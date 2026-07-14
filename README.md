# pg-slide-harvester

`pg-slide-harvester` is a lightweight command-line tool for discovering,
downloading, and organizing PostgreSQL conference slide decks.

It helps you keep a local archive of public PostgreSQL event materials such as
PDF, PPT, PPTX, and ODP files. The project is designed for practical personal
and community use: discover events, follow conference pages, download available
slides, classify them by topic, and revisit sessions whose materials are
published later.

## Features

- Discover PostgreSQL events from `postgresql.org`.
- Download public slide assets from supported event platforms.
- Track sessions whose slides are not available yet.
- Re-run crawls safely; already downloaded files are skipped.
- Organize files by event and topic.
- Generate local HTML and CSV indexes.
- Use a small local SQLite database for state.

## Supported Sources

Current adapters include:

- `pgevents.ca` events, such as PGConf.dev.
- Indico-based events, such as CERN PGDay.
- WordPress-based conference websites.
- Generic fallback scanning for independent conference websites.

More dedicated adapters can be added incrementally as new conference platforms
are encountered.

## Quick Start

```bash
python3 pgppt.py init
python3 pgppt.py scan-official
python3 pgppt.py list events
python3 pgppt.py download-event "CERN PGDay 2026"
python3 pgppt.py report
```

Generated local artifacts:

- Event archive: `archive/by_event/`
- Topic index: `archive/by_topic/`
- SQLite state: `data/pgppt.sqlite`
- HTML report: `reports/index.html`
- CSV report: `reports/index.csv`

These local artifacts are intentionally ignored by git.

## Commands

```bash
# Download one PDF/PPT/ODP asset.
python3 pgppt.py ingest <asset-url>

# Scan one page for PDF/PPT/ODP links.
python3 pgppt.py ingest <page-url>

# Discover events from postgresql.org events/archive.
python3 pgppt.py scan-official

# Download by event name after running list events.
python3 pgppt.py download-event "CERN PGDay 2026"

# Classify local events by adapter type.
python3 pgppt.py analyze-events

# Resolve official event pages and classify external websites.
python3 pgppt.py analyze-events --resolve --limit 30

# Crawl a pgevents.ca sessions page.
python3 pgppt.py crawl-pgevents https://www.pgevents.ca/events/pgconfdev2026/sessions/ --event "PGConf.dev 2026"

# Crawl a generic conference website.
python3 pgppt.py crawl-generic https://example-conference.org/ --event "Conference Name"

# Process sessions that are due for re-checking.
python3 pgppt.py tick

# Regenerate reports.
python3 pgppt.py report

# Inspect local state.
python3 pgppt.py list assets
python3 pgppt.py list sessions
python3 pgppt.py list events
```

## Delayed Slide Publication

Many conferences publish slide decks days or weeks after the event. The tool
tracks each session with a status:

- `missing`：当前页面还没找到资料
- `found`：发现资料链接
- `downloaded`：已下载
- `failed`：下载失败
- `login_required`：需要登录，暂时跳过

Each checked session receives a `next_check_at` timestamp so future runs can
revisit pages where slides were not available yet.

For recurring usage, run:

```bash
python3 pgppt.py tick
python3 pgppt.py report
```

## Design Principles

- Be polite to conference websites.
- Download only publicly available materials.
- Do not bypass authentication or access restrictions.
- Keep local archives and generated reports out of version control.
- Prefer small adapters over a fragile one-size-fits-all crawler.

## Roadmap

- Dedicated PGConf.EU/PostgreSQL Europe adapter.
- More event-platform adapters.
- Optional Playwright-based login cookie reuse for sites that require login.
- Better topic classification.
- Optional recurring job setup for macOS/Linux.

## License

MIT
