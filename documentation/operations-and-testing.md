# Operations and Testing

This document explains how to set up, run, inspect, test, and safely operate the
Squeeze Scanner repository. It is written for both human maintainers and AI
agents that need to understand the operational surface quickly.

> **Important:** Squeeze Scanner is informational only and is **not financial
> advice**. Scores, alerts, reports, and historical analytics are screening aids,
> not trade recommendations.

## Repository role and runtime shape

Squeeze Scanner is a local-first Python/FastAPI application that ranks equities
across independent squeeze setup models using Yahoo Finance data through
`yfinance`. The browser UI, API routes, scheduler, saved screens, watchlists,
alerts, cache, score history, and analytics reports all run from the same local
process by default.

Key implementation entry points:

| Area | File(s) | Operational meaning |
| --- | --- | --- |
| CLI entrypoint | `src/squeeze_scanner/server.py` | Runs Uvicorn with settings loaded from `.env`. |
| FastAPI routes | `src/squeeze_scanner/web.py` | Defines health/status, scan, history, reports, schedules, alerts, screens, and watchlist APIs. |
| Configuration | `src/squeeze_scanner/config.py`, `.env.example` | Loads runtime settings from `.env` in the current working directory. |
| Market data cache | `src/squeeze_scanner/cache.py` | Stores raw provider snapshots in SQLite and tracks provider/cache status. |
| Scanner service | `src/squeeze_scanner/service.py` | Normalizes symbols, scans in parallel, records score history, ranks responses. |
| Scheduler and alerts | `src/squeeze_scanner/automation.py`, `src/squeeze_scanner/alert_delivery.py` | Persists schedules, runs, alert rules, alert events, and delivery attempts. |
| Optional providers | `src/squeeze_scanner/providers/premium.py` | Exposes premium-provider seams and explicit unconfigured status. |
| Tests | `tests/` | Covers scoring, cache/history, provider status, scheduler/alerts, analytics, screens, and deployment status. |

## Dependencies and installation

Runtime dependencies are declared in `pyproject.toml`:

- Python `>=3.11`
- `fastapi`
- `jinja2`
- `pandas`
- `python-dotenv`
- `uvicorn[standard]`
- `yfinance`

Development dependencies:

- `pytest`

Recommended setup:

```bash
uv sync
cp .env.example .env
```

If tests are missing `pytest` in a freshly provisioned environment, sync dev
dependencies explicitly:

```bash
uv sync --dev
```

## Configuration variables

The checked-in `.env.example` is the authoritative template. Copy it to `.env`
and edit `.env` only. Do not commit real credentials, webhook secrets, vendor
tokens, or private URLs.

| Variable | Default/example | Purpose |
| --- | --- | --- |
| `SQUEEZE_SCANNER_HOST` | `.env.example`: `100.126.90.82`; code default: `0.0.0.0` | Host/IP Uvicorn binds to. Use `127.0.0.1` for local-only, `0.0.0.0` or a Tailscale IP only when intentional. |
| `SQUEEZE_SCANNER_PORT` | `7890` | HTTP port. |
| `SQUEEZE_SCANNER_RELOAD` | `false` | Enables Uvicorn auto-reload when true-like (`1`, `true`, `yes`). |
| `SQUEEZE_SCANNER_CACHE_DB` | `data/market_data_cache.sqlite3` | SQLite file for raw cache, score history, screens, watchlists, schedules, alerts, and analytics tables. Relative paths resolve from the process working directory. |
| `SQUEEZE_SCANNER_CACHE_TTL_SECONDS` | `3600` | Raw market data freshness window. Scores are recomputed from raw data on each response. |
| `SQUEEZE_SCANNER_SCHEDULER_ENABLED` | `true` | Starts the in-process scheduler at API startup. Set false only when another worker polls due schedules. |
| `SQUEEZE_SCANNER_SCHEDULER_POLL_SECONDS` | `30` | Scheduler polling interval. Minimum effective value is 1 second. |
| `SQUEEZE_SCANNER_DEFAULT_OWNER_ID` | blank | Optional metadata tag for newly created screens, watchlists, schedules, and alerts. This is not authentication. |
| `SQUEEZE_SCANNER_QUOTE_PROVIDER` | `yahoo` | Quote provider. Yahoo is the only built-in implementation. |
| `SQUEEZE_SCANNER_BORROW_PROVIDER` | `disabled` | Optional premium borrow feed name. No real adapter exists unless separately implemented and configured. |
| `SQUEEZE_SCANNER_SHORT_INTEREST_PROVIDER` | `disabled` | Optional premium short-interest feed name. |
| `SQUEEZE_SCANNER_CORPORATE_ACTIONS_PROVIDER` | `disabled` | Optional corporate action feed name. |
| `SQUEEZE_SCANNER_FILINGS_PROVIDER` | `disabled` | Optional filings/float/dilution feed name. |
| `SQUEEZE_SCANNER_EVENT_PROVIDER` | `disabled` | Optional halt/news/event feed name. |
| `SQUEEZE_SCANNER_ALERT_DELIVERY_CHANNELS` | blank | Comma-separated default delivery channels. Supported channels are `noop` and `webhook`. Blank means in-app only. |
| `SQUEEZE_SCANNER_ALERT_WEBHOOK_URL` | blank | Webhook endpoint for `webhook` delivery. Keep this in `.env` or a secrets manager. |
| `SQUEEZE_SCANNER_ALERT_WEBHOOK_TIMEOUT_SECONDS` | `5` | Webhook request timeout. |
| `SQUEEZE_SCANNER_PUBLIC_BASE_URL` | blank | Optional browser URL included in alert payload links. |

### Configuration pitfalls

- Start commands from the repository root. `config.py` uses `Path.cwd()` for
  `.env` loading and relative database paths.
- `.env.example` is safe documentation. `.env` is where local secrets and
  machine-specific values belong.
- Selecting a premium provider name does not magically enable premium data.
  Current optional providers report `unconfigured` unless a real adapter and
  credentials are implemented.
- `SQUEEZE_SCANNER_DEFAULT_OWNER_ID` only tags records. The app currently reports
  `auth_required=false`; do not treat owner filters as access control.

## Running locally

Start from the repository root:

```bash
uv run squeeze-scanner
```

Build a shell base URL from `.env` for `curl` checks:

```bash
set -a
source .env
set +a
BASE_URL="http://${SQUEEZE_SCANNER_HOST:-127.0.0.1}:${SQUEEZE_SCANNER_PORT:-7890}"
```

Open the browser at:

```text
http://<SQUEEZE_SCANNER_HOST>:<SQUEEZE_SCANNER_PORT>/
```

Or confirm with curl:

```bash
curl -fsS "${BASE_URL}/" | grep 'Squeeze Scanner'
```

## Development reload

Preferred local reload path:

```dotenv
SQUEEZE_SCANNER_RELOAD=true
```

Then restart:

```bash
uv run squeeze-scanner
```

Direct Uvicorn invocation is also supported:

```bash
uv run uvicorn squeeze_scanner.web:app \
  --app-dir src \
  --host "${SQUEEZE_SCANNER_HOST:-127.0.0.1}" \
  --port "${SQUEEZE_SCANNER_PORT:-7890}" \
  --reload \
  --reload-dir src/squeeze_scanner
```

The app sends `Cache-Control: no-store` for `/`, `/static/*`, and `/api/*`, so
stale browser assets should be uncommon. If a phone or browser still shows old
assets, force-close the tab or clear site data.

## Health, status, and operational checks

Health and status are intentionally structured for operators and AI agents.

```bash
curl -fsS "${BASE_URL}/api/health" | python3 -m json.tool
curl -fsS "${BASE_URL}/api/status" | python3 -m json.tool
```

Expected top-level shape:

```json
{
  "status": "ok",
  "app": {
    "name": "squeeze-scanner",
    "auth_required": false,
    "owner_scoping_available": true
  },
  "storage": {
    "backend": "sqlite",
    "accessible": true
  },
  "cache": {},
  "provider": {},
  "scheduler": {},
  "automation": {}
}
```

Additional useful checks:

```bash
curl -fsS "${BASE_URL}/api/scheduler/status" | python3 -m json.tool
curl -fsS "${BASE_URL}/api/providers" | python3 -m json.tool
curl -fsS "${BASE_URL}/api/model" | python3 -m json.tool
curl -I "${BASE_URL}/"
```

Use the listener check when debugging bind/port issues:

```bash
ss -ltnp | grep ":${SQUEEZE_SCANNER_PORT}"
```

Stop a local instance with the numeric PID from `ss`:

```bash
kill <PID>
```

## Scanning and API smoke tests

Scan explicit symbols:

```bash
curl -fsS -X POST "${BASE_URL}/api/scan" \
  -H 'Content-Type: application/json' \
  -d '{"symbols":"INHD, BYND"}' | python3 -m json.tool
```

Scan Yahoo's most-shorted universe:

```bash
curl -fsS -X POST "${BASE_URL}/api/scan/most-shorted?count=100" | python3 -m json.tool
```

Load recent cached scans:

```bash
curl -fsS "${BASE_URL}/api/scans/recent" | python3 -m json.tool
```

Inspect score history and deltas:

```bash
curl -fsS "${BASE_URL}/api/scans/history?limit=25" | python3 -m json.tool
curl -fsS "${BASE_URL}/api/scans/BYND/deltas" | python3 -m json.tool
```

Delete one latest-cache entry so the next scan refetches that ticker:

```bash
curl -fsS -X DELETE "${BASE_URL}/api/scans/BYND" | python3 -m json.tool
```

## Scheduler operations

The scheduler is local and in-process. At FastAPI startup, `web.py` starts
`AutomationScheduler` when `SQUEEZE_SCANNER_SCHEDULER_ENABLED=true`; at shutdown
it stops the scheduler thread.

Scheduler target types:

- `symbols`
- `yahoo_most_shorted`
- `watchlist`
- `saved_screen`

Inspect state:

```bash
curl -fsS "${BASE_URL}/api/scheduler/status" | python3 -m json.tool
curl -fsS "${BASE_URL}/api/scheduled-scans" | python3 -m json.tool
curl -fsS "${BASE_URL}/api/scheduled-scan-runs?limit=25" | python3 -m json.tool
```

Create and run a simple symbol schedule:

```bash
curl -fsS -X POST "${BASE_URL}/api/scheduled-scans" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Hourly BYND","target_type":"symbols","target":{"symbols":["BYND"]},"interval_seconds":3600}' \
  | python3 -m json.tool

curl -fsS -X POST "${BASE_URL}/api/scheduled-scans/1/run" | python3 -m json.tool
```

Operational boundaries:

- Scheduler state and run history persist in SQLite.
- The polling loop itself does not run while the web process is stopped.
- For hosted or highly reliable scheduled scanning, use an external worker or
  queue and set `SQUEEZE_SCANNER_SCHEDULER_ENABLED=false` for the web process.

## Provider status and premium data boundaries

The default provider is Yahoo Finance. It supplies public quote/liquidity fields,
public short-interest fields when available, split signals, and options-derived
proxy values. It does not supply true securities-lending borrow fees, true dealer
positioning, or complete premium event/filing datasets.

Check provider status:

```bash
curl -fsS "${BASE_URL}/api/providers" | python3 -m json.tool
```

Important behavior:

- `SQUEEZE_SCANNER_QUOTE_PROVIDER` supports only `yahoo`/`yahoo_finance` today.
- Optional provider settings for borrow, short interest, corporate actions,
  filings, and events are seams. Without a real adapter and credentials, selected
  provider names return `unconfigured`.
- Missing premium data lowers confidence or creates warnings; it should not
  create bullish scores.
- Yahoo options are a proxy. True GEX fields require a provider-backed options
  adapter with greeks.

## Alert delivery configuration

Alerts are persisted locally. Delivery is in-app only unless channels are
configured.

Dry-run delivery:

```dotenv
SQUEEZE_SCANNER_ALERT_DELIVERY_CHANNELS=noop
```

Webhook delivery:

```dotenv
SQUEEZE_SCANNER_ALERT_DELIVERY_CHANNELS=webhook
SQUEEZE_SCANNER_ALERT_WEBHOOK_URL=https://example.invalid/scanner-alerts
SQUEEZE_SCANNER_ALERT_WEBHOOK_TIMEOUT_SECONDS=5
SQUEEZE_SCANNER_PUBLIC_BASE_URL=http://127.0.0.1:7890
```

Create an alert:

```bash
curl -fsS -X POST "${BASE_URL}/api/alerts" \
  -H 'Content-Type: application/json' \
  -d '{"name":"High score","rule":{"type":"score_threshold","threshold":70},"delivery_channels":["noop"]}' \
  | python3 -m json.tool
```

Inspect events and delivery attempts:

```bash
curl -fsS "${BASE_URL}/api/alert-events?limit=25" | python3 -m json.tool
curl -fsS "${BASE_URL}/api/alert-delivery-attempts?limit=25" | python3 -m json.tool
```

Retry a failed delivery attempt:

```bash
curl -fsS -X POST "${BASE_URL}/api/alert-delivery-attempts/1/retry" | python3 -m json.tool
```

Alert behavior to know:

- Alert events are deduplicated while the condition remains active.
- Failed webhook/noop attempts are persisted in `alert_delivery_attempts`.
- Webhook destination labels are sanitized in status rows, but webhook URLs must
  still be protected in `.env`.
- Native Slack, Discord, email, and SMS channels are not built in.

## SQLite and cache operations

By default, all local persistence uses:

```text
data/market_data_cache.sqlite3
```

The database includes raw cache, raw history, derived score history, saved
screens, watchlists, schedules, alert events, delivery attempts, price history,
and scan outcomes.

Core tables from the current implementation:

| Table | Purpose |
| --- | --- |
| `market_data_cache` | Latest raw provider snapshot per provider/symbol. |
| `market_data_history` | Refreshed raw snapshots over time. |
| `scan_score_history` | Derived score rows, model details, confidence, risk flags, and raw references. |
| `saved_screens` | Saved filter/ranking definitions. |
| `watchlists` / `watchlist_symbols` | Local watchlist metadata and symbols. |
| `scheduled_scans` / `scheduled_scan_runs` | Schedule definitions and run history. |
| `alerts` / `alert_events` / `alert_delivery_attempts` | Alert rules, deduplicated events, and external delivery status. |
| `price_history` / `scan_outcomes` | Backtesting/calibration inputs and computed outcomes. |

Set the DB path for shell commands:

```bash
DB_PATH="${SQUEEZE_SCANNER_CACHE_DB:-data/market_data_cache.sqlite3}"
```

List tables:

```bash
sqlite3 "${DB_PATH}" ".tables"
```

Inspect cache freshness:

```bash
sqlite3 "${DB_PATH}" \
  "SELECT provider, symbol, datetime(fetched_at, 'unixepoch') AS fetched_at_utc, datetime(scanned_at, 'unixepoch') AS scanned_at_utc FROM market_data_cache ORDER BY scanned_at DESC LIMIT 50;"
```

Inspect history volume:

```bash
sqlite3 "${DB_PATH}" \
  "SELECT provider, COUNT(*) AS rows, datetime(MAX(fetched_at), 'unixepoch') AS latest_fetched_at_utc FROM market_data_history GROUP BY provider;"
```

Inspect latest score rows:

```bash
sqlite3 "${DB_PATH}" \
  "SELECT symbol, score, primary_model, risk_level, scoring_model_version, created_at FROM scan_score_history ORDER BY created_at DESC LIMIT 25;"
```

Force one ticker to refetch on the next scan:

```bash
sqlite3 "${DB_PATH}" "DELETE FROM market_data_cache WHERE symbol = 'BYND';"
```

Force all current tickers to refetch:

```bash
sqlite3 "${DB_PATH}" "DELETE FROM market_data_cache;"
```

Safe SQLite practices:

- Back up the database before manual deletes or schema experiments.
- Delete from `market_data_cache` when forcing refresh; avoid deleting
  `market_data_history` or `scan_score_history` unless intentionally discarding
  analytics history.
- SQLite is suitable for local single-user operation. It is not the right
  persistence layer for high-concurrency hosted multi-user workloads.
- If using backup tooling, include any SQLite sidecar files created by the local
  SQLite mode, such as `*.sqlite3-wal` and `*.sqlite3-shm`.

## Validation commands

Run the full repository test suite:

```bash
uv run pytest
```

Run targeted operational tests:

```bash
uv run pytest tests/test_deployment_status.py tests/test_automation.py tests/test_premium_providers.py
```

Run cache/history/scanner tests:

```bash
uv run pytest tests/test_scanner.py tests/test_history.py tests/test_analytics.py
```

Run screen/watchlist and options-confidence tests:

```bash
uv run pytest tests/test_screens.py tests/test_options.py tests/test_confidence_risk.py
```

After starting the service, run API smoke checks:

```bash
curl -fsS "${BASE_URL}/api/health" | python3 -m json.tool
curl -fsS "${BASE_URL}/api/providers" | python3 -m json.tool
curl -fsS -X POST "${BASE_URL}/api/scan" \
  -H 'Content-Type: application/json' \
  -d '{"symbols":"BYND"}' | python3 -m json.tool
```

## Troubleshooting

| Symptom | Likely cause | Check or fix |
| --- | --- | --- |
| `uv` is not found | `uv` is not installed or not on `PATH` | Install `uv`, then rerun `uv sync`. |
| App ignores `.env` | Process started from a different directory | Start from the repository root because `.env` is loaded from `Path.cwd()`. |
| Port is already in use | Another service owns the configured port | `ss -ltnp | grep ":${SQUEEZE_SCANNER_PORT}"`, then stop the intended PID or change the port. |
| Browser cannot connect | Host/IP binding is unreachable | Use `127.0.0.1` for local-only; verify firewall/Tailscale routing for non-local hosts. |
| `/api/health` is degraded | SQLite or automation schema/status failed | Inspect `storage`, `cache`, and `automation.error` fields in `/api/status`. |
| First scan is slow | Cache miss requires live Yahoo fetch | Retry after cache warms; inspect `cache.fresh_rows` in `/api/status`. |
| Market data is missing | Yahoo/network/rate/data issue | Try a liquid ticker such as `AAPL` or `BYND`; inspect `errors` in scan response. |
| Premium provider shows `unconfigured` | Provider name selected but no adapter/credentials exist | Set provider back to `disabled` or implement and configure the adapter. |
| Webhook delivery fails | Bad URL, timeout, or remote non-2xx response | Check `/api/alert-delivery-attempts`, fix `.env`, then retry the attempt. |
| Scheduler does not run due scans | Scheduler disabled or web process not running | Check `/api/scheduler/status`; ensure `SQUEEZE_SCANNER_SCHEDULER_ENABLED=true`. |
| SQLite locked errors | Too many concurrent writes or external DB inspection | Stop long-running manual SQL sessions; consider external storage for hosted scale. |
| UI appears stale | Browser cache/site data despite no-store headers | Check `curl -I "${BASE_URL}/"` and clear browser site data. |

## Safe operational boundaries

- Treat every score and alert as informational only, not financial advice.
- Keep credentials and private endpoints in `.env` or a secrets manager only.
- Do not expose the service to untrusted networks without adding real
  authentication, authorization, and transport/security controls.
- Do not assume `owner_id` is security. It is metadata and filter support only.
- Do not assume premium-provider data exists unless `/api/providers` reports a
  configured implementation and scans show source provenance for those fields.
- Do not assume Yahoo-derived options values are true dealer GEX.
- Do not run the in-process scheduler as the only reliability layer for hosted
  production workflows. Use an external worker/queue if scans must survive web
  process restarts.
- Do not use local SQLite for high-concurrency, multi-user production traffic.
- Avoid manual SQL changes to historical tables unless you are deliberately
  changing analytics/backtesting history.

## AI-agent checklist

When asked to operate, test, or modify this area:

1. Read `.env.example`, `pyproject.toml`, `readme.md`, `runbook.md`,
   `ROADMAP.md`, `src/squeeze_scanner/config.py`, and
   `src/squeeze_scanner/web.py`.
2. For cache/status issues, inspect `src/squeeze_scanner/cache.py`,
   `src/squeeze_scanner/history.py`, and `src/squeeze_scanner/analytics.py`.
3. For scheduler or alert issues, inspect `src/squeeze_scanner/automation.py`
   and `src/squeeze_scanner/alert_delivery.py`.
4. For provider issues, inspect `src/squeeze_scanner/providers/premium.py` and
   `src/squeeze_scanner/providers/yahoo.py`.
5. Validate with the narrowest relevant `uv run pytest ...` command, then use
   `curl` smoke checks against a running local server if behavior is API-facing.
