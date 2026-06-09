# Short Squeeze Scanner Runbook

## Service address

The expected Tailscale URL for this host is:

```text
http://100.126.90.82:7890/
```

## Start the service

Standard start:

```bash
uv sync
SQUEEZE_SCANNER_HOST=100.126.90.82 SQUEEZE_SCANNER_PORT=7890 uv run squeeze-scanner
```

Development start with source auto-reload:

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

## Confirm it is running

Check the listener:

```bash
ss -ltnp | grep ':7890'
```

Check health from the server:

```bash
curl http://100.126.90.82:7890/api/health
```

Expected response:

```json
{"status":"ok"}
```

Check the homepage:

```bash
curl -fsS http://100.126.90.82:7890/ | grep 'Short Squeeze Scanner'
```

## Stop or restart the service

Find the process:

```bash
ss -ltnp | grep ':7890'
```

Stop it with the numeric PID shown by `ss`:

```bash
kill <PID>
```

Then restart with either the standard or reload-enabled command above.

## Validate scanner behavior

Run tests:

```bash
uv run pytest
```

Run a live scan:

```bash
curl -fsS -X POST http://100.126.90.82:7890/api/scan \
  -H 'Content-Type: application/json' \
  -d '{"symbols":"BYND"}' | python3 -m json.tool
```

Load recent cached scans:

```bash
curl -fsS http://100.126.90.82:7890/api/scans/recent | python3 -m json.tool
```

Inspect the scoring model metadata used by the frontend legend and tooltips:

```bash
curl -fsS http://100.126.90.82:7890/api/model | python3 -m json.tool
```

## Cache operations

The cache stores raw market snapshots only. Scores are recomputed on every API response.

Inspect cached raw market data:

```bash
sqlite3 data/market_data_cache.sqlite3 \
  "SELECT provider, symbol, datetime(fetched_at, 'unixepoch') AS fetched_at_utc, datetime(scanned_at, 'unixepoch') AS scanned_at_utc FROM market_data_cache ORDER BY scanned_at DESC;"
```

Inspect one cached payload:

```bash
sqlite3 data/market_data_cache.sqlite3 \
  "SELECT payload_json FROM market_data_cache WHERE symbol = 'BYND';"
```

Force the next scan to refresh all market data:

```bash
sqlite3 data/market_data_cache.sqlite3 "DELETE FROM market_data_cache;"
```

Force one ticker to refresh:

```bash
sqlite3 data/market_data_cache.sqlite3 "DELETE FROM market_data_cache WHERE symbol = 'BYND';"
```

Use a different cache database or TTL:

```bash
SQUEEZE_SCANNER_CACHE_DB=/tmp/squeeze-cache.sqlite3 \
SQUEEZE_SCANNER_CACHE_TTL_SECONDS=3600 \
SQUEEZE_SCANNER_HOST=100.126.90.82 \
SQUEEZE_SCANNER_PORT=7890 \
uv run squeeze-scanner
```

## Browser refresh behavior

The app sends:

```text
Cache-Control: no-store
```

for `/`, `/static/*`, and `/api/*`. If a phone still shows stale assets, force-close the browser tab or clear the site data, then reload `http://100.126.90.82:7890/`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Phone cannot load the site | Confirm Tailscale is connected on the phone and server, then check `ss -ltnp | grep ':7890'`. |
| Health works but page looks stale | Confirm reload server is running and check `curl -I http://100.126.90.82:7890/` for `Cache-Control: no-store`. |
| Scan is slow | New or stale tickers require a Yahoo Finance fetch; cached tickers under 1 hour should return faster. |
| Market data is missing | Confirm outbound internet access and try a liquid ticker such as `AAPL` or `BYND`. |
| Cache appears wrong | Delete the ticker row from `market_data_cache`; the next scan will refresh raw data. |
