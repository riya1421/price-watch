"""
Price Watch: scrape a product catalog, store snapshots, report what changed.

Marketing teams ask "did competitors change prices / launch / go out of stock this
week?" This script answers that on a schedule:

    scrape → normalise → SQLite snapshot → diff vs previous run → CSV + Markdown report
                                                           └→ optional AI summary (Claude)

Demo target: books.toscrape.com, a sandbox site built for scraping practice.
Swap the PARSER section to point it at any catalog you have permission to scrape.

Usage:
    python price_watch.py --pages 3
    python price_watch.py --pages 3 --simulate-changes   # demo: fake a "next week" run
    python price_watch.py --pages 3 --summary            # needs ANTHROPIC_API_KEY
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import sqlite3
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

BASE = "https://books.toscrape.com/"
HEADERS = {"User-Agent": "price-watch-demo/1.0 (+portfolio project)"}
DELAY_S = 1.0  # be polite: one request per second
OUT = Path(__file__).parent / "output"
DB = OUT / "snapshots.db"
RATING = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


@dataclass
class Product:
    sku: str          # stable key: the product URL slug
    title: str
    price: float
    currency: str
    rating: int
    in_stock: bool
    url: str


# ---------------- PARSER (site-specific) ----------------
def parse_listing(html: str, page_url: str) -> list[Product]:
    soup = BeautifulSoup(html, "html.parser")
    items = []
    for card in soup.select("article.product_pod"):
        a = card.select_one("h3 a")
        url = urljoin(page_url, a["href"])
        price_text = card.select_one(".price_color").get_text(strip=True)
        rating_cls = [c for c in card.select_one(".star-rating")["class"] if c != "star-rating"]
        items.append(Product(
            sku=url.rstrip("/").split("/")[-2] if url.endswith("index.html") else url,
            title=a["title"].strip(),
            price=float("".join(ch for ch in price_text if ch.isdigit() or ch == ".")),
            currency="GBP",
            rating=RATING.get(rating_cls[0] if rating_cls else "", 0),
            in_stock="in stock" in card.select_one(".availability").get_text(strip=True).lower(),
            url=url,
        ))
    return items


def next_page(html: str, page_url: str) -> str | None:
    nxt = BeautifulSoup(html, "html.parser").select_one("li.next a")
    return urljoin(page_url, nxt["href"]) if nxt else None
# --------------------------------------------------------


def fetch(session: requests.Session, url: str, tries: int = 3) -> str:
    for i in range(1, tries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            if i == tries:
                raise
            print(f"  retry {i} for {url}: {e}")
            time.sleep(2 ** i)
    raise RuntimeError("unreachable")


def scrape(max_pages: int) -> list[Product]:
    robots = RobotFileParser(urljoin(BASE, "robots.txt"))
    try:
        robots.read()
    except Exception:
        pass  # no robots.txt reachable: treat as allowed, but keep the delay
    session = requests.Session()
    url, products, page = BASE, [], 0
    while url and page < max_pages:
        if robots.default_entry or robots.entries:
            if not robots.can_fetch(HEADERS["User-Agent"], url):
                print(f"robots.txt disallows {url}; stopping.")
                break
        page += 1
        print(f"page {page}: {url}")
        html = fetch(session, url)
        products += parse_listing(html, url)
        url = next_page(html, url)
        time.sleep(DELAY_S)
    # de-duplicate on sku (catalogs often repeat items across pages)
    return list({p.sku: p for p in products}.values())


def save_snapshot(con: sqlite3.Connection, products: list[Product]) -> int:
    con.executescript("""
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, scraped_at TEXT NOT NULL, n INTEGER);
        CREATE TABLE IF NOT EXISTS snapshots (
            run_id INTEGER REFERENCES runs(id), sku TEXT, title TEXT, price REAL, currency TEXT,
            rating INTEGER, in_stock INTEGER, url TEXT, PRIMARY KEY (run_id, sku));
    """)
    cur = con.execute("INSERT INTO runs (scraped_at, n) VALUES (?, ?)",
                      (datetime.now(timezone.utc).isoformat(timespec="seconds"), len(products)))
    run_id = cur.lastrowid
    con.executemany(
        "INSERT INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
        [(run_id, p.sku, p.title, p.price, p.currency, p.rating, int(p.in_stock), p.url) for p in products],
    )
    con.commit()
    return run_id


def diff_runs(con: sqlite3.Connection, run_id: int) -> dict:
    prev = con.execute("SELECT id FROM runs WHERE id < ? ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    if not prev:
        return {"first_run": True}
    q = "SELECT sku, title, price, in_stock, url FROM snapshots WHERE run_id = ?"
    old = {r[0]: r for r in con.execute(q, (prev[0],))}
    new = {r[0]: r for r in con.execute(q, (run_id,))}
    price_changes = [
        {"title": new[s][1], "old": old[s][2], "new": new[s][2],
         "pct": round((new[s][2] - old[s][2]) / old[s][2] * 100, 1), "url": new[s][4]}
        for s in new.keys() & old.keys() if abs(new[s][2] - old[s][2]) > 0.009
    ]
    return {
        "first_run": False,
        "price_changes": sorted(price_changes, key=lambda c: c["pct"]),
        "new_products": [new[s][1] for s in new.keys() - old.keys()],
        "removed_products": [old[s][1] for s in old.keys() - new.keys()],
        "went_out_of_stock": [new[s][1] for s in new.keys() & old.keys() if old[s][3] and not new[s][3]],
        "back_in_stock": [new[s][1] for s in new.keys() & old.keys() if not old[s][3] and new[s][3]],
    }


def simulate_changes(products: list[Product]) -> list[Product]:
    """Demo only: pretend a week passed (price moves, a delisting, a stock-out)."""
    rnd = random.Random(42)
    out = [Product(**asdict(p)) for p in products]
    for p in rnd.sample(out, k=min(5, len(out))):
        p.price = round(p.price * rnd.choice([0.8, 0.85, 1.1, 1.2]), 2)
    if len(out) > 3:
        out[2].in_stock = False
        out.pop(0)
    out.append(Product("new-launch-demo", "A Brand New Launch (demo)", 19.99, "GBP", 5, True, BASE))
    return out


def write_reports(products: list[Product], diff: dict, run_id: int, ai_summary: str | None) -> Path:
    OUT.mkdir(exist_ok=True)
    with open(OUT / f"catalog_run{run_id}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(products[0]).keys()))
        w.writeheader()
        w.writerows(asdict(p) for p in products)

    lines = [f"# Price Watch report: run {run_id}", "",
             f"_{datetime.now().strftime('%d %b %Y, %H:%M')} · {len(products)} products tracked_", ""]
    if ai_summary:
        lines += ["## Summary", "", ai_summary, ""]
    if diff.get("first_run"):
        lines += ["First run: baseline saved. Changes will be reported from the next run."]
    else:
        pc = diff["price_changes"]
        lines += [f"## Price changes ({len(pc)})", ""]
        if pc:
            lines += ["| Product | Old | New | Change |", "|---|---:|---:|---:|"]
            lines += [f"| [{c['title'][:60]}]({c['url']}) | £{c['old']:.2f} | £{c['new']:.2f} | {c['pct']:+.1f}% |" for c in pc]
        for key, label in [("new_products", "New products"), ("removed_products", "Removed"),
                           ("went_out_of_stock", "Went out of stock"), ("back_in_stock", "Back in stock")]:
            lines += ["", f"## {label} ({len(diff[key])})", ""] + [f"- {t}" for t in diff[key]]
    path = OUT / f"report_run{run_id}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def ai_summary(diff: dict) -> str | None:
    """Optional: a 3-bullet summary for a non-technical marketing lead."""
    if diff.get("first_run") or not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic  # imported lazily so the scraper works without the SDK

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-opus-5-5"),
        max_tokens=2000,
        output_config={"effort": "low"},
        system="You write short competitor-monitoring notes for a marketing lead. "
               "Use only the data given. Max 3 bullets, plain language, mention the biggest numbers.",
        messages=[{"role": "user", "content": f"Changes since last week's scrape:\n{diff}"}],
    )
    if resp.stop_reason == "refusal":
        return None
    return next((b.text for b in resp.content if b.type == "text"), None)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pages", type=int, default=2, help="catalog pages to scrape (20 items each)")
    ap.add_argument("--simulate-changes", action="store_true", help="demo: fake next week's data")
    ap.add_argument("--summary", action="store_true", help="add an AI summary (needs ANTHROPIC_API_KEY)")
    args = ap.parse_args()

    products = scrape(args.pages)
    if not products:
        raise SystemExit("No products parsed: the site layout may have changed. Check the PARSER section.")
    if args.simulate_changes:
        products = simulate_changes(products)

    OUT.mkdir(exist_ok=True)
    with sqlite3.connect(DB) as con:
        run_id = save_snapshot(con, products)
        diff = diff_runs(con, run_id)
    summary = ai_summary(diff) if args.summary else None
    report = write_reports(products, diff, run_id, summary)
    print(f"\n{len(products)} products · run {run_id} · report: {report}")


if __name__ == "__main__":
    main()
