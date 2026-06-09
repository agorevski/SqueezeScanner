# Short Squeeze Scanner

A Python/FastAPI website for screening potential short squeeze setups using live Yahoo Finance data through `yfinance`.

The app is currently designed to run on the Tailscale address:

```text
http://100.126.90.82:7890/
```

This is informational only and is not financial advice.

## Features

- Browser UI for scanning comma-, space-, or semicolon-separated ticker symbols.
- Default prefill list: `INHD, GME, AMC, CVNA, BYND, RILY`.
- Recent screened stocks load automatically from the local cache when the page opens.
- Search results append to the screened-stock list instead of replacing it.
- Signal tiles and rationale bullets are color-coded by squeeze favorability: red, orange, yellow, green.
- Model cards include hover/tap tooltips plus a bottom-of-page signal guide explaining each calculation.
- Signal labels, weights, descriptions, calculations, and the color legend come from the Python scoring model API, not hard-coded HTML/JS.
- Raw market data is cached in SQLite for 1 hour; model scores are always recomputed.
- `Cache-Control: no-store` is sent for the page, static files, and API responses to avoid stale browser assets.
- Development server can run with Uvicorn auto-reload for `app/`, `templates/`, and `static/`.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- Network access to Yahoo Finance
- Tailscale access to `100.126.90.82`

## Setup

```bash
uv sync
```

## Run

Standard launch:

```bash
uv run squeeze-scanner
```

Defaults:

- host: `0.0.0.0`
- port: `7890`

Host-specific Tailscale launch:

```bash
SQUEEZE_SCANNER_HOST=100.126.90.82 SQUEEZE_SCANNER_PORT=7890 uv run squeeze-scanner
```

Reload-enabled launch used during development:

```bash
uv run uvicorn app.main:app \
  --app-dir /home/algore/GIT/agorevski/SqueezeScanner \
  --host 100.126.90.82 \
  --port 7890 \
  --reload \
  --reload-dir /home/algore/GIT/agorevski/SqueezeScanner/app \
  --reload-dir /home/algore/GIT/agorevski/SqueezeScanner/templates \
  --reload-dir /home/algore/GIT/agorevski/SqueezeScanner/static
```

## Market data cache

The cache stores only raw financial-service data from `TickerSnapshot` records, such as price, volume, short interest, days to cover, float shares, and market cap.

Each row also stores timestamps:

- `fetched_at`: when raw market data was last refreshed from Yahoo Finance.
- `scanned_at`: when the ticker was last screened in this app.

It does **not** cache:

- model score
- risk level
- component scores
- rationale
- rendered results

Defaults:

- database: `data/market_data_cache.sqlite3`
- table: `market_data_cache`
- refresh interval: `3600` seconds

Configuration:

```bash
SQUEEZE_SCANNER_CACHE_DB=/path/to/market_data_cache.sqlite3 \
SQUEEZE_SCANNER_CACHE_TTL_SECONDS=3600 \
uv run squeeze-scanner
```

## Scoring model

The scanner uses `squeeze-v2`, a 100-point model weighted toward the characteristics that create squeeze pressure:

| Characteristic | Weight | Why it matters |
| --- | ---: | --- |
| Short interest % of float | 35 | Core squeeze fuel; the model gives no points below 10%, ramps materially above 20%, and treats 40%+ as extreme. |
| Days to cover | 20 | Estimates how hard it may be for shorts to exit based on average trading volume; starts contributing above 2 days. |
| Float pressure | 15 | Smaller tradable floats can move faster when demand spikes; market cap is used only as a weaker fallback. |
| Momentum | 15 | Positive 1-day, 5-day, and 20-day price action can indicate that covering pressure is starting. |
| Relative volume | 10 | Elevated volume confirms active demand/liquidity and helps distinguish dormant setups from active squeezes. |
| Short-interest trend | 5 | Rising short interest adds pressure, but it is weighted lightly because reported short data is delayed. |

The model does not currently include borrow fees, utilization, fails-to-deliver, options gamma, or news catalysts because those are not reliably available from the current Yahoo Finance data source.

The frontend reads this metadata from `GET /api/model` and from the `model` block included in scan responses. To change the UX legend/tooltips, update `SCORING_SIGNALS` in `app/scanner.py`.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Website front-end |
| `GET` | `/api/health` | Health check |
| `GET` | `/api/model` | Current scoring model metadata used by the UI legend and tooltips |
| `GET` | `/api/scans/recent` | Scores tickers screened within the last hour from cached raw snapshots |
| `POST` | `/api/scan` | Scans requested tickers, using cached raw data unless stale |

Example:

```bash
curl -X POST http://100.126.90.82:7890/api/scan \
  -H 'Content-Type: application/json' \
  -d '{"symbols":"INHD, BYND"}'
```

## Test

```bash
uv run pytest
```

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the component diagram and data flow.
