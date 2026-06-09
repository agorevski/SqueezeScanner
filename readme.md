# Squeeze Scanner

A Python/FastAPI website for screening potential squeeze setups using live Yahoo Finance data through `yfinance`.

This is informational only and is not financial advice.

## Features

- Browser UI for scanning comma-, space-, or semicolon-separated ticker symbols.
- One-click Yahoo most-shorted loader that screens Yahoo's predefined most-shorted universe.
- Default prefill list: `INHD, MSFT, ZM, GME, AMC, CVNA, BYND, RILY`.
- Recent screened stocks load automatically from the local cache when the page opens.
- Search results append to the screened-stock list instead of replacing it.
- Screened stocks can be filtered in the browser by top model, setup level, score, short interest, relative volume, and float size.
- Trash-can controls remove a screened ticker from both the page and the local SQLite cache.
- Signal tiles and rationale bullets are color-coded by squeeze favorability: red, orange, yellow, green.
- Model cards include hover/tap tooltips plus a bottom-of-page model guide explaining each category and calculation.
- Signal labels, weights, descriptions, calculations, and the color legend come from the Python scoring model API, not hard-coded HTML/JS.
- Each ticker receives four independent 0-100 scores: Classical Short Squeeze, Float Compression, Gamma Candidate, and Hybrid.
- Raw market data is cached in SQLite for 1 hour; refreshed snapshots are appended to a history table and model scores are always recomputed.
- `Cache-Control: no-store` is sent for the page, static files, and API responses to avoid stale browser assets.
- Development server can run with Uvicorn auto-reload for `src/squeeze_scanner`.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- Network access to Yahoo Finance
- Optional: Tailscale if exposing the site to a tailnet

## Setup

```bash
uv sync
cp .env.example .env
```

Edit `.env` for your machine. The checked-in `.env.example` lists all supported runtime variables:

| Variable | Purpose |
| --- | --- |
| `SQUEEZE_SCANNER_HOST` | Host/IP for Uvicorn to bind, such as `127.0.0.1`, `0.0.0.0`, or a Tailscale IP. |
| `SQUEEZE_SCANNER_PORT` | Port for the website. |
| `SQUEEZE_SCANNER_RELOAD` | Set to `true` to enable Uvicorn reload through the console script. |
| `SQUEEZE_SCANNER_CACHE_DB` | Repository-relative or absolute path to the SQLite raw market data cache. |
| `SQUEEZE_SCANNER_CACHE_TTL_SECONDS` | Raw market data freshness window before a ticker is refreshed. |

## Run

```bash
uv run squeeze-scanner
```

The app automatically loads `.env` from the repository root. Visit:

```text
http://<SQUEEZE_SCANNER_HOST>:<SQUEEZE_SCANNER_PORT>/
```

For local development with source auto-reload, set this in `.env`:

```dotenv
SQUEEZE_SCANNER_RELOAD=true
```

Then run the same command:

```bash
uv run squeeze-scanner
```

If you prefer invoking Uvicorn directly:

```bash
uv run uvicorn squeeze_scanner.web:app \
  --app-dir src \
  --host "${SQUEEZE_SCANNER_HOST:-127.0.0.1}" \
  --port "${SQUEEZE_SCANNER_PORT:-7890}" \
  --reload \
  --reload-dir src/squeeze_scanner
```

## Project layout

```text
src/squeeze_scanner/
  cache.py              SQLite raw market data cache
  config.py             .env loading and runtime settings
  domain.py             shared dataclasses, protocols, and errors
  providers/yahoo.py    yfinance/Yahoo Finance adapter
  scoring.py            four-model metadata and scoring logic
  service.py            ticker normalization, scanning, and response shaping
  web.py                FastAPI app, routes, static assets, and templates
  server.py             console-script entrypoint
```

The top-level `app/` package is retained only as a thin compatibility shim for older `app.main:app` or `app.scanner` imports.

## Market data cache

The cache stores only raw financial-service data from `TickerSnapshot` records, such as price, volume, short interest, days to cover, float shares, market cap, split history signals, and option-chain aggregates.

`market_data_cache` keeps the latest raw snapshot for fast current scans. `market_data_history` keeps every refreshed raw snapshot, including records older than the 1-hour cache TTL, so historical score trends can be computed later from prior raw data.

Each row also stores timestamps:

- `fetched_at`: when raw market data was last refreshed from Yahoo Finance.
- `scanned_at`: when the ticker was last screened in this app.

It does **not** cache:

- model score
- risk level
- component scores
- rationale
- rendered results

Defaults are configured in `.env.example`:

- database: `data/market_data_cache.sqlite3`
- latest table: `market_data_cache`
- history table: `market_data_history`
- refresh interval: `3600` seconds

## Scoring models

The scanner uses `squeeze-v3`, which returns four independent 0-100 model scores per ticker. The top score is used only for sorting and the setup badge; the four category scores remain separate in the API and website.

| Model | Signals | Definition |
| --- | --- | --- |
| Classical Short Squeeze | Short interest, borrow fee, days to cover | Names with a large short base, expensive borrow when a borrow-fee feed is available, and crowded short exits. |
| Float Compression | Tiny float, recent reverse split, rapid volume increase | Tiny-float names where recent share-count compression can combine with rising demand. |
| Gamma Candidate | Heavy call buying, dealer gamma exposure | Options-driven names where call activity and public-options exposure may force dealer hedging flows. |
| Hybrid | Tiny float, short interest, borrow fee, options activity | Rare names combining compressed supply, high short pressure, costly borrow, and elevated options activity. |

Yahoo Finance does not provide borrow fees or true dealer positioning. Borrow-fee fields score zero until an external securities-lending feed populates `borrow_fee_pct`; dealer gamma uses a public option-chain exposure proxy when options data is available.

The frontend reads this metadata from `GET /api/model` and from the `model` block included in scan responses. To change the UX legend/tooltips, update `SCORING_MODELS` in `src/squeeze_scanner/scoring.py`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Website front-end |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/model` | Current scoring model metadata used by the UI legend and tooltips |
| `GET` | `/api/scans/recent` | Scores tickers screened within the last hour from latest cached raw snapshots |
| `DELETE` | `/api/scans/{symbol}` | Deletes one ticker from the local cache so it disappears from the current UX |
| `POST` | `/api/scan` | Scans requested tickers, using cached raw data unless stale |
| `POST` | `/api/scan/most-shorted?count=100` | Loads Yahoo's predefined most-shorted universe and analyzes those tickers |

Example:

```bash
curl -X POST "http://${SQUEEZE_SCANNER_HOST:-127.0.0.1}:${SQUEEZE_SCANNER_PORT:-7890}/api/scan" \
  -H 'Content-Type: application/json' \
  -d '{"symbols":"INHD, BYND"}'
```

Yahoo most-shorted example:

```bash
curl -X POST "http://${SQUEEZE_SCANNER_HOST:-127.0.0.1}:${SQUEEZE_SCANNER_PORT:-7890}/api/scan/most-shorted?count=100"
```

## Test

```bash
uv run pytest
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the component diagram and data flow.
