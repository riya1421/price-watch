# Price Watch: competitor catalog monitoring

Marketing teams keep asking: *did competitors change prices, launch something or go out of stock this week?* Usually someone checks websites by hand. This script **scrapes a product catalog, saves a snapshot to SQLite, compares it with the last run** and writes a CSV plus a readable Markdown report. An optional AI summary is written for a non-technical reader.

```
scrape (polite: robots.txt, 1 req/s, retries)
  → normalise (price → number, rating → 1–5, stock → bool, stable SKU)
  → SQLite snapshot (runs + snapshots tables)
  → diff vs previous run (price changes %, new, removed, stock-outs)
  → output/catalog_runN.csv + output/report_runN.md (+ optional Claude summary)
```

## Problem → decision → result

| | |
|---|---|
| **Problem** | Manual competitor checks are slow, inconsistent and leave no history. |
| **Build / buy / skip** | Paid tools (Prisync and similar) make sense for hundreds of SKUs across many sites. For a handful of competitors, ~250 lines of Python plus a weekly cron job is enough and free, so **build**. If the site has an API or a product feed, **use that instead of scraping**. |
| **Key design choices** | Site-specific parsing is isolated in one section, so adding a new site means editing about 30 lines. Snapshots are append-only, so you get price history for free. The AI step is optional and runs *after* the deterministic diff. The numbers always come from code, never from the model. |
| **Result** | A weekly report that answers "what changed?" in 10 seconds of reading. See [sample-report.md](sample-report.md). |

## Run it

```bash
pip install -r requirements.txt
python price_watch.py --pages 2                      # first run = baseline
python price_watch.py --pages 2 --simulate-changes   # demo a "next week" run with changes
python price_watch.py --pages 2 --summary            # add AI summary (needs ANTHROPIC_API_KEY)
```

To run it every Monday at 9am: cron `0 9 * * 1 python price_watch.py --pages 5 --summary`, or Windows Task Scheduler, or a GitHub Actions schedule.

## Responsible scraping

The demo targets books.toscrape.com, a site built for scraping practice. For real sites: check the terms of service and robots.txt, keep the rate low, scrape only public data and prefer official APIs or feeds.

## What I'd do next

- Post the report to Slack, or write it to a Google Sheet for the marketing team.
- Alert only when a change crosses a threshold (for example a price drop of more than 10%).
- Store snapshots in Supabase so a small dashboard can chart price history.
