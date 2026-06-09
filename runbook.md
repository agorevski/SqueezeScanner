# Squeeze Scanner Runbook

## Configuration

Runtime configuration lives in `.env` and is documented in `.env.example`.

```bash
cp .env.example .env
```

Use repository-relative paths where possible, for example:

```dotenv
SQUEEZE_SCANNER_CACHE_DB=data/market_data_cache.sqlite3
```

## Start the service

```bash
uv sync
uv run squeeze-scanner
```

The console script loads `.env` automatically.

For development reload, set:

```dotenv
SQUEEZE_SCANNER_RELOAD=true
```

Then restart:

```bash
uv run squeeze-scanner
```

## Confirm it is running

Build the base URL from `.env`:

```bash
source .env
BASE_URL="http://${SQUEEZE_SCANNER_HOST}:${SQUEEZE_SCANNER_PORT}"
```

Check the listener:

```bash
ss -ltnp | grep ":${SQUEEZE_SCANNER_PORT}"
```

Check health:

```bash
curl "${BASE_URL}/api/health"
```

Expected response:

```json
{"status":"ok"}
```

Check the homepage:

```bash
curl -fsS "${BASE_URL}/" | grep 'Squeeze Scanner'
```

## Stop or restart the service

Find the process:

```bash
ss -ltnp | grep ":${SQUEEZE_SCANNER_PORT}"
```

Stop it with the numeric PID shown by `ss`:

```bash
kill <PID>
```

Then restart with:

```bash
uv run squeeze-scanner
```

## Validate scanner behavior

Run tests:

```bash
uv run pytest
```

Run a live scan:

```bash
curl -fsS -X POST "${BASE_URL}/api/scan" \
  -H 'Content-Type: application/json' \
  -d '{"symbols":"BYND"}' | python3 -m json.tool
```

Load and analyze Yahoo's most-shorted universe:

```bash
curl -fsS -X POST "${BASE_URL}/api/scan/most-shorted?count=100" | python3 -m json.tool
```

Load recent cached scans:

```bash
curl -fsS "${BASE_URL}/api/scans/recent" | python3 -m json.tool
```

Inspect the scoring model metadata used by the frontend legend and tooltips:

```bash
curl -fsS "${BASE_URL}/api/model" | python3 -m json.tool
```

Delete one ticker from the scanner page and local cache:

```bash
curl -fsS -X DELETE "${BASE_URL}/api/scans/BYND" | python3 -m json.tool
```

## Cache operations

The cache stores raw market snapshots only. Scores are recomputed on every API response.

Inspect cached raw market data:

```bash
sqlite3 "${SQUEEZE_SCANNER_CACHE_DB:-data/market_data_cache.sqlite3}" \
  "SELECT provider, symbol, datetime(fetched_at, 'unixepoch') AS fetched_at_utc, datetime(scanned_at, 'unixepoch') AS scanned_at_utc FROM market_data_cache ORDER BY scanned_at DESC;"
```

Inspect one cached payload:

```bash
sqlite3 "${SQUEEZE_SCANNER_CACHE_DB:-data/market_data_cache.sqlite3}" \
  "SELECT payload_json FROM market_data_cache WHERE symbol = 'BYND';"
```

Force the next scan to refresh all market data:

```bash
sqlite3 "${SQUEEZE_SCANNER_CACHE_DB:-data/market_data_cache.sqlite3}" "DELETE FROM market_data_cache;"
```

Force one ticker to refresh manually:

```bash
sqlite3 "${SQUEEZE_SCANNER_CACHE_DB:-data/market_data_cache.sqlite3}" "DELETE FROM market_data_cache WHERE symbol = 'BYND';"
```

## Browser refresh behavior

The app sends:

```text
Cache-Control: no-store
```

for `/`, `/static/*`, and `/api/*`. If a phone still shows stale assets, force-close the browser tab or clear the site data, then reload the URL from `BASE_URL`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Browser cannot load the site | Confirm the configured host/IP is reachable and check `ss -ltnp | grep ":${SQUEEZE_SCANNER_PORT}"`. |
| Health works but page looks stale | Confirm reload is enabled if developing and check `curl -I "${BASE_URL}/"` for `Cache-Control: no-store`. |
| Scan is slow | New or stale tickers require a Yahoo Finance fetch; cached tickers under 1 hour should return faster. |
| Market data is missing | Confirm outbound internet access and try a liquid ticker such as `AAPL` or `BYND`. |
| Cache appears wrong | Delete the ticker row from `market_data_cache`; the next scan will refresh raw data. |
