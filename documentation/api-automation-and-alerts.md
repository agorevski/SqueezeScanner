# API, Automation, and Alerts

This document describes the HTTP API, saved screen/watchlist storage, scheduled scan engine, alert lifecycle, delivery retry model, report exports, and operational status endpoints in Squeeze Scanner. It is written for human maintainers and AI agents that need to understand the repository before changing routes or data contracts.

> Squeeze Scanner is an informational screening tool. API responses, alerts, scores, reports, and examples in this document are not financial advice and are not trade recommendations.

## Implementation map

| Area | Primary files | Responsibility |
| --- | --- | --- |
| FastAPI routes | `src/squeeze_scanner/web.py` | Creates the app, wires services, defines request models, route handlers, no-store headers, owner defaults, and HTTP error mappings. |
| Scanning and ranking | `src/squeeze_scanner/service.py` | Normalizes symbols, fetches/scans concurrently, records score history, computes deltas, ranks results, and shapes scan responses. |
| Saved screens/watchlists | `src/squeeze_scanner/screens.py` | Persists saved screen JSON and watchlist symbols in SQLite with optional owner metadata. |
| Automation | `src/squeeze_scanner/automation.py` | Persists schedules, runs, alerts, alert events, delivery attempts, dedupe state, and in-process scheduler status. |
| Alert delivery | `src/squeeze_scanner/alert_delivery.py` | Builds delivery payloads and sends through `noop` or webhook channels. |
| Analytics and reports | `src/squeeze_scanner/analytics.py` | Reads score history/outcomes for deltas, reports, calibration buckets, model comparison, gamma review, and CSV export. |
| Validation coverage | `tests/test_automation.py`, `tests/test_analytics.py` | Covers schedule persistence/status, owner filtering, alert dedupe/delivery/retry, report routes, calibration, gamma review, deltas, and CSV. |
| Product docs | `readme.md`, `ROADMAP.md` | List the shipped API surface, runtime settings, implementation status, and local-first constraints. |

## Cross-cutting HTTP conventions

### Transport and response format

- API paths are under `/api/...`; the browser UI is served from `/`.
- Most endpoints return JSON.
- Report endpoints return JSON by default and return `text/csv` when `format=csv`.
- The app adds `Cache-Control: no-store` to `/`, `/static/...`, and `/api/...` responses so browsers and clients do not reuse stale assets or stale scan data.
- There is no built-in authentication in the current FastAPI app. Status payloads report `auth_required: false`.

### Symbol handling

Scanner-facing endpoints normalize symbols through `normalize_symbols()`:

- Strings can be comma-, semicolon-, or whitespace-separated.
- Symbols are uppercased and deduplicated while preserving first appearance.
- Valid symbols match a compact ticker-like pattern: first character alphanumeric, followed by alphanumeric, dot, or hyphen characters.
- Manual scans default to a 25-symbol maximum; scheduled scans and Yahoo most-shorted flows allow up to 250; watchlist symbol operations allow up to 500.

Invalid or excessive symbol lists are reported as HTTP `400` for route-level validation. Per-symbol provider failures during a scan are returned in the scan response `errors` array while successful symbols still appear in `results`.

### Time handling

- Query parameters named `from` are represented internally as `from_time` because `from` is a Python keyword.
- History and report timestamps accept the formats supported by `parse_history_timestamp()` and analytics datetime coercion: ISO-8601 strings and numeric epoch-like values.
- History filters compare against raw snapshot fetch time.
- Report windows default to the last 24 hours when `from` or `to` are omitted.

### `owner_id` behavior

`owner_id` is metadata and filtering support, not authentication.

- New saved screens, watchlists, scheduled scans, and alerts accept an optional `owner_id`.
- If request `owner_id` is omitted during create and `SQUEEZE_SCANNER_DEFAULT_OWNER_ID` is set, the default owner is applied.
- Blank strings are normalized to `null`.
- List/read/delete endpoints that accept `owner_id` filter by exact normalized owner when provided.
- Omitting `owner_id` returns or acts on all local resources for that endpoint.
- Updates only change ownership when the request body explicitly includes `owner_id`; passing `null` or blank clears it.
- Shared/hosted deployments still need real authentication and authorization before relying on owner metadata.

### Error handling conventions

| Condition | HTTP behavior |
| --- | --- |
| Invalid symbols, invalid ranking mode, invalid sort direction | `400` with `{"detail": "..."}`. |
| Invalid screen/watchlist/schedule/alert body | Usually `400` with `{"detail": "..."}`; FastAPI/Pydantic range/type errors may return `422`. |
| Missing saved screen or watchlist | `404` for route handlers that require the resource. |
| Missing schedule, alert, alert event, or delivery attempt | `404` when surfaced through HTTP. |
| Yahoo most-shorted screener failure | `502`. |
| Invalid report window, bucket size, horizon, or format | `400`. |
| Provider errors for individual symbols during scan | HTTP `200` scan response with entries in `errors`. |
| Scheduled scan runtime failure | Persisted as a run with `status: "failure"`; immediate-run endpoint still returns the run record. |

## Scans, model metadata, and history

### Endpoint table

| Method | Path | Request input | Response concept |
| --- | --- | --- | --- |
| `GET` | `/api/model` | None | Scoring model metadata: version, models, weights, signals, guardrails, and favorability scale. |
| `GET` | `/api/scans/recent` | Query: `ranking_mode`, `selected_model`, `sort_direction` | Re-scores recent cached raw snapshots and returns a standard scan response. |
| `GET` | `/api/scans/history` | Query: `symbol`, `from`, `to`, `limit`, `primary_model` or `model`, `min_score`, `max_score`, `risk_level`, `scoring_model_version` | `{count, results}` for persisted `scan_score_history` rows across symbols. |
| `GET` | `/api/scans/{symbol}/history` | Same history filters except `symbol` is in the path | `{symbol, count, results}` for one normalized symbol. |
| `GET` | `/api/scans/{symbol}/deltas` | Path symbol | Structured explanation of previous-scan, 1h/24h/7d style score changes from analytics history. |
| `POST` | `/api/scans/recompute` | JSON body with optional `symbols`, `from`, `to`, `limit` | Recomputes historical raw snapshots with the current scoring model and returns written history rows. |
| `DELETE` | `/api/scans/{symbol}` | Path symbol | Deletes latest cached raw snapshot for the symbol: `{symbol, deleted}`. |
| `POST` | `/api/scan` | JSON `ScanRequest` | Fetches or reuses raw snapshots, scores them, persists score history, computes deltas, and returns a standard scan response. |
| `POST` | `/api/scan/most-shorted` | Query: `count`, plus optional ranking fields | Loads Yahoo's predefined most-shorted symbols and returns a standard scan response. |

### Standard scan request

```json
{
  "symbols": "GME, AMC; CVNA",
  "ranking_mode": "selected_model_score",
  "selected_model": "hybrid",
  "sort_direction": "desc"
}
```

`symbols` may also be an array:

```json
{
  "symbols": ["GME", "AMC"],
  "ranking_mode": "score_increase_24h"
}
```

### Standard scan response

`POST /api/scan`, `GET /api/scans/recent`, `POST /api/scan/most-shorted`, and watchlist scans use the same response shape:

| Field | Meaning |
| --- | --- |
| `model` | Current scoring metadata from `scoring_model_metadata()`. |
| `ranking` | Normalized ranking options: `mode`, `selected_model`, `sort_direction`. |
| `generated_at` | UTC response-generation timestamp. |
| `count` | Number of returned, ranked results after optional guardrail filtering. |
| `results` | Array of scored `ScanResult` payloads, augmented with scan timestamp and delta fields. |
| `errors` | Per-symbol provider/scanner errors that did not prevent other symbols from scoring. |

Each `results[]` object is derived from `ScanResult.to_dict()` and normally includes:

- identity: `symbol`, `company_name`
- primary scoring: `score`, `risk_level`, `data_quality`, `primary_model`
- model detail: `model_scores`, `model_components`, `model_rationales`, `model_confidence`, `confidence_rationales`
- UI/rationale detail: `metrics`, `components`, `rationale`, `warnings`, `risk_flags`
- provenance: `field_sources`, `field_quality`, `source_quality`
- scan recency: `scanned_at`, `minutes_since_scan`
- score-change fields: `previous_scan_delta`, `delta_24h`, `score_delta_status`, and related history fields when available

### Ranking fields

| `ranking_mode` | Sort value | Notes |
| --- | --- | --- |
| `top_score` | Overall top score | Default mode. Aliases include `score` and `top`. |
| `selected_model_score` | A selected model score | Uses `selected_model` when provided; otherwise uses the result's `primary_model` score and can fall back to overall score. |
| `highest_model_confidence` | Highest model confidence value | Reads `model_confidence`-style fields. |
| `score_increase_1h` | 1-hour score delta | Uses mapped persisted delta fields when available, then result/metric delta fields. |
| `score_increase_24h` | 24-hour score delta | Uses `delta_24h` and equivalent score-delta fields. |
| `relative_volume` | `relative_volume` metric | Higher is first unless `sort_direction=asc`. |
| `short_interest` | `short_percent_float` / short-interest metric | Higher is first by default. |
| `smallest_float` | `float_shares` / float metric | Defaults to ascending so smaller floats appear first. |
| `hybrid_only` | `model_scores.hybrid` | Alias examples: `hybrid`, `hybrid_score`. |
| `gamma_candidate_only` | `model_scores.gamma_candidate` | Alias examples: `gamma`, `gamma_score`. |

`selected_model` aliases include `classical`, `classical_short_squeeze`, `short_squeeze`, `float`, `float_compression`, `gamma`, `gamma_candidate`, and `hybrid`.

`sort_direction` accepts ascending aliases such as `asc`, `ascending`, `low_to_high`, `smallest_first` and descending aliases such as `desc`, `descending`, `high_to_low`, `largest_first`. Null ranking values sort after populated values. Ties use higher overall score, higher data quality, then symbol.

### History and recomputation concepts

Score history rows are derived scores linked back to raw snapshot history. A history row includes identifiers and provenance (`id`, `provider`, `raw_history_id`, `raw_fetched_at`, `scan_run_id`, `scoring_model_version`) plus the same score/model/metric/risk fields needed for analytics.

Example recompute request:

```json
{
  "symbols": ["GME", "AMC"],
  "from": "2026-06-01T00:00:00Z",
  "to": "2026-06-10T00:00:00Z",
  "limit": 250
}
```

Recompute reads retained raw snapshots, scores them with the current scoring code, writes `scan_score_history`, and returns `{model, scan_run_id, count, results}`.

## Screens and watchlists

Saved screens store UI/filter/ranking JSON. Watchlists store named symbol lists. Both are local SQLite resources with optional owner metadata.

### Endpoint table

| Method | Path | Request input | Response concept |
| --- | --- | --- | --- |
| `GET` | `/api/screens` | Query: optional `owner_id` | `{screens: [...]}` sorted by recent update. |
| `POST` | `/api/screens` | `{name, filters or filters_json, owner_id?}` | Created saved screen with both `filters` and `filters_json` in response. |
| `PUT` | `/api/screens/{screen_id}` | Partial `{name?, filters?, filters_json?, owner_id?}` | Updated screen or `404`. |
| `DELETE` | `/api/screens/{screen_id}` | Query: optional `owner_id` | `{deleted: true}` or `404`. |
| `GET` | `/api/watchlists` | Query: optional `owner_id` | `{watchlists: [...]}` with symbols. |
| `POST` | `/api/watchlists` | `{name, symbols?, owner_id?}` | Created watchlist. |
| `PUT` | `/api/watchlists/{watchlist_id}` | Partial `{name?, owner_id?}` | Updated watchlist or `404`. |
| `DELETE` | `/api/watchlists/{watchlist_id}` | Query: optional `owner_id` | `{deleted: true}` or `404`. |
| `GET` | `/api/watchlists/{watchlist_id}/symbols` | Query: optional `owner_id` | `{watchlist_id, symbols}`. |
| `POST` | `/api/watchlists/{watchlist_id}/symbols` | Body `{symbols}`; query optional `owner_id` | Watchlist after adding normalized symbols. |
| `DELETE` | `/api/watchlists/{watchlist_id}/symbols/{symbol}` | Query: optional `owner_id` | `{symbol, removed}`; `removed` may be false if symbol was not present. |
| `POST` | `/api/watchlists/{watchlist_id}/scan` | Body optional ranking request; query optional `owner_id` | Standard scan response plus `watchlist_id` and `symbols`. |

### Saved screen example

```json
{
  "name": "High score hybrid candidates",
  "filters": {
    "min_score": 70,
    "primary_model": "hybrid",
    "ranking_mode": "relative_volume"
  },
  "owner_id": "alice"
}
```

The store validates that filters are a JSON object and are JSON-serializable. The API accepts either `filters` or `filters_json`; the response includes both names for compatibility.

### Watchlist examples

Create a watchlist:

```json
{
  "name": "Focus list",
  "symbols": "GME, AMC; CVNA",
  "owner_id": "alice"
}
```

Add symbols:

```json
{
  "symbols": ["BYND", "RILY"]
}
```

Scan a watchlist with a ranking preference:

```json
{
  "ranking_mode": "smallest_float",
  "sort_direction": "asc"
}
```

An empty watchlist scan returns a valid standard scan response with `count: 0`, no results, and the watchlist metadata.

## Scheduler and scheduled scans

Schedules are persisted in SQLite and can be run by the in-process scheduler or manually through the API. The scheduler starts on FastAPI startup only when `SQUEEZE_SCANNER_SCHEDULER_ENABLED` is true.

### Endpoint table

| Method | Path | Request input | Response concept |
| --- | --- | --- | --- |
| `GET` | `/api/scheduler/status` | None | In-process scheduler state plus automation service status. |
| `GET` | `/api/scheduled-scans` | Query: optional `owner_id` | List schedule definitions. |
| `POST` | `/api/scheduled-scans` | `{name, target_type, target, interval_seconds, enabled?, next_run_at?, owner_id?}` | Created schedule. |
| `GET` | `/api/scheduled-scans/{schedule_id}` | Query: optional `owner_id` | One schedule or `404`. |
| `PATCH` | `/api/scheduled-scans/{schedule_id}` | Partial schedule fields | Updated schedule or `404`. |
| `DELETE` | `/api/scheduled-scans/{schedule_id}` | Query: optional `owner_id` | `{id, deleted}`. |
| `POST` | `/api/scheduled-scans/{schedule_id}/run` | None | Runs immediately and returns the persisted run record with `alert_events`. |
| `GET` | `/api/scheduled-scan-runs` | Query: optional `schedule_id`, `limit` | Recent run records. |

### Schedule request fields

| Field | Meaning |
| --- | --- |
| `name` | Required display name. |
| `target_type` | One of `symbols`, `yahoo_most_shorted`, `watchlist`, `saved_screen` with several aliases normalized by the service. |
| `target` | JSON object interpreted according to `target_type`. |
| `interval_seconds` | Positive integer interval between completed run time and next run time. |
| `enabled` | Defaults to true. Disabled schedules have `next_run_at: null`. |
| `next_run_at` | Optional initial run timestamp. If omitted and enabled, next run is now plus interval. |
| `owner_id` | Optional metadata/filtering tag. |

### Target payloads

| `target_type` | Example `target` | Behavior |
| --- | --- | --- |
| `symbols` | `{"symbols": ["GME", "AMC"], "max_symbols": 50}` | Scans explicit normalized symbols. |
| `yahoo_most_shorted` | `{"count": 100}` | Calls the Yahoo most-shorted screener; `count` must be 1-250. |
| `watchlist` | `{"watchlist_id": 1}` or `{"name": "Focus list"}` | Resolves symbols from `watchlist_symbols`; explicit `symbols` in the target override lookup. |
| `saved_screen` | `{"saved_screen_id": 2}` or `{"name": "High RVOL"}` | Uses explicit symbols or symbols embedded in saved-screen filters. Full saved-screen filtering is not implemented in the scheduler yet. |

Create schedule example:

```json
{
  "name": "Hourly focus list",
  "target_type": "watchlist",
  "target": {
    "watchlist_id": 1
  },
  "interval_seconds": 3600,
  "enabled": true,
  "owner_id": "alice"
}
```

### Run lifecycle

When a scheduled scan runs:

1. A `scheduled_scan_runs` row is created with `status: "running"`.
2. The target resolves to symbols.
3. `ScannerService.scan()` runs and returns a standard scan response.
4. Enabled alerts are evaluated against returned results. If the schedule has an `owner_id`, only alerts with that owner are listed; if the schedule owner is null, owner filtering is omitted and all enabled local alerts are eligible.
5. The run is finished with:
   - `success` when results exist and no errors were returned
   - `partial_success` when results and errors both exist
   - `failure` when no results are produced and errors occurred, or when target/scanner execution raises
6. `last_run_at` is set and `next_run_at` becomes `finished_at + interval_seconds` for enabled schedules.

The run record includes `symbols_scanned`, `errors`, `result_count`, `response`, and `error_message`. Immediate API runs also attach `alert_events` created by that run.

## Alerts and alert events

Alerts are persisted rules evaluated against scheduled scan results. Current manual `/api/scan` calls do not create alert events; alert evaluation happens through `AutomationService.process_alerts()`, which scheduled scans call after scanning.

### Endpoint table

| Method | Path | Request input | Response concept |
| --- | --- | --- | --- |
| `GET` | `/api/alerts` | Query: `enabled_only`, optional `owner_id` | List alert definitions. |
| `POST` | `/api/alerts` | `{name, rule, enabled?, delivery_channels?, owner_id?}` | Created alert definition. |
| `PATCH` | `/api/alerts/{alert_id}` | Partial alert fields | Updated alert or `404`. |
| `DELETE` | `/api/alerts/{alert_id}` | Query: optional `owner_id` | `{id, deleted}`. |
| `GET` | `/api/alert-events` | Query: `alert_id`, `symbol`, `active_only`, `limit`, optional `owner_id` | Alert events with attached delivery attempts. |
| `POST` | `/api/alert-events/{event_id}/ack` | None | Marks an event acknowledged without clearing it. |

### Alert request fields

| Field | Meaning |
| --- | --- |
| `name` | Required alert name. |
| `rule` | Required JSON object normalized and validated by alert-rule logic. |
| `enabled` | Defaults to true. Disabled alerts are not evaluated. |
| `delivery_channels` | Optional channel list. `null` uses configured default channels; `[]` means in-app event only. |
| `owner_id` | Optional metadata/filtering tag. Scheduled scans pass their owner into alert filtering; a non-null schedule owner limits evaluation to matching alerts, while a null schedule owner evaluates all enabled local alerts. |

### Alert rule types

All rule types support `direction: "above"` or `"below"`; default is `above`. The trigger is active when the value is `>= threshold` for `above` or `<= threshold` for `below`.

| Normalized type | Aliases | Required fields | Value source |
| --- | --- | --- | --- |
| `score_threshold` | `score`, `score_crosses_threshold` | `threshold` or `value` | Result `score`. |
| `model_threshold` | `model_crosses_threshold`, `selected_model_threshold` | `model`/`model_key`/`selected_model`, plus `threshold` | `model_scores[model]` or `{model}_score`. |
| `score_increase` | `score_increase_1h`, `score_increase_24h` | `delta` or `threshold`; optional `window` of `1h` or `24h` | Delta fields such as `previous_scan_delta`, `delta_24h`, `score_delta_1h`, or nested delta maps. |
| `relative_volume_threshold` | `relative_volume` | `threshold` | `relative_volume` metric. |
| `short_interest_threshold` | `short_interest` | `threshold` | `short_percent_float` metric. |
| `float_compression_threshold` | `float_compression`, `float_compression_score` | `threshold` | `model_scores.float_compression`. |
| `gamma_score_threshold` | `gamma`, `gamma_score` | `threshold` | `model_scores.gamma_candidate`. |

Example alert:

```json
{
  "name": "Hybrid model score above 80",
  "rule": {
    "type": "model_threshold",
    "model": "hybrid",
    "threshold": 80,
    "direction": "above"
  },
  "enabled": true,
  "delivery_channels": ["webhook"],
  "owner_id": "alice"
}
```

### Alert event shape

Alert events include:

- `id`, `alert_id`, `symbol`
- `scan_run_id` and optional `scan_score_history_id`
- `condition_key`, `rule_type`, `message`
- `value`, `threshold`, `previous_value`
- `result`, which is the scan result snapshot that triggered the event
- `created_at`, `acknowledged_at`, `cleared_at`
- `active`, derived from whether `cleared_at` is null
- `delivery_attempts`, attached when listing events

### Dedupe and clearing

Alert dedupe is implemented with a unique open-condition index on `(alert_id, symbol, condition_key)` where `cleared_at IS NULL`.

- When a condition first becomes active, a new event is inserted.
- If the same alert/symbol/condition remains active, the insert is rejected and no duplicate event or duplicate external notification is created.
- When a later scan evaluates the condition as inactive, the open event is marked with `cleared_at`.
- If the condition becomes active again after clearing, a new event is created.
- Acknowledging an event sets `acknowledged_at`; it does not clear or dedupe-reset the event.

## Delivery attempts and retries

Delivery attempts are external-notification records attached to alert events. They are separate from in-app alert events so failed delivery can be inspected and retried without re-triggering the alert condition.

### Endpoint table

| Method | Path | Request input | Response concept |
| --- | --- | --- | --- |
| `GET` | `/api/alert-delivery-attempts` | Query: `alert_event_id`, `alert_id`, `status`, `limit`, optional `owner_id` | List delivery attempts ordered by last attempt time. |
| `POST` | `/api/alert-delivery-attempts/{attempt_id}/retry` | Query: optional `owner_id` | Re-sends one attempt's channel and returns the updated attempt. |

### Channel configuration

Built-in channels:

| Channel | Behavior |
| --- | --- |
| `noop` | Dry-run delivery; logs the alert and records success with `{"dry_run": true}`. |
| `webhook` | Sends an HTTP `POST` JSON payload to `SQUEEZE_SCANNER_ALERT_WEBHOOK_URL`. |

Default channels for new alerts come from `SQUEEZE_SCANNER_ALERT_DELIVERY_CHANNELS`. If defaults are blank and an alert does not specify `delivery_channels`, alert events remain in-app only and no attempt rows are created.

`webhook` failures are captured as attempt rows with `status: "failure"`, `error_message`, and any response metadata. Webhook destination labels intentionally include only scheme and host, not full secret-bearing URLs.

### Delivery payload

External messages include:

- `text`, `symbol`, `score`, `model_confidence`, `risk_flags`
- `link` built from `SQUEEZE_SCANNER_PUBLIC_BASE_URL` when configured
- `alert`: `id`, `name`, `rule`
- `trigger`: event id, rule type, condition key, message, value, threshold, previous value, and creation time

### Attempt and retry behavior

Each event/channel pair has one persisted attempt row. On initial delivery the row is inserted with `retry_count: 0`. Retrying:

- reloads the original attempt, event, and alert
- re-sends only that attempt's `channel`
- updates `destination`, `status`, `last_attempted_at`, `error_message`, and `response`
- increments `retry_count`

Example retry call:

```text
POST /api/alert-delivery-attempts/42/retry?owner_id=alice
```

## Reports and calibration

Report endpoints read persisted score history, price history, and scan outcomes. They are for analysis and calibration of the software model; they are not predictive guarantees.

### Common report parameters

| Parameter | Used by | Meaning |
| --- | --- | --- |
| `from`, `to` | Historical reports | Window bounds. Default is last 24 hours ending now. |
| `model` | Most reports | Score/model key to analyze; defaults vary by report. |
| `limit`, `offset` | Paged reports | Limit visible rows and assign `rank` starting at `offset + 1`. |
| `format` | All report endpoints except catalog | `json` by default; `csv` exports visible rows only. |

`format=csv` returns `text/csv` with a `Content-Disposition` filename derived from the report name. CSV columns are derived from row keys in visible-row order. Nested dict/list values are JSON-encoded; booleans become `true`/`false`; null becomes an empty cell.

### Endpoint table

| Method | Path | Important parameters | Response concept |
| --- | --- | --- | --- |
| `GET` | `/api/reports` | None | Catalog of available report endpoints. |
| `GET` | `/api/reports/top-new-high-setups` | `from`, `to`, `model`, `min_score`, `limit`, `offset`, `format` | Symbols first reaching at least `min_score` in the window, excluding prior high symbols. |
| `GET` | `/api/reports/biggest-1h-increases` | `from`, `to`, `model`, `min_delta`, `limit`, `offset`, `format` | Largest score increases versus a 1-hour baseline. |
| `GET` | `/api/reports/biggest-24h-increases` | Same as above | Largest score increases versus a 24-hour baseline. |
| `GET` | `/api/reports/repeated-high-setups` | `from`, `to`, `model`, `min_score`, `min_count`, `limit`, `offset`, `format` | Symbols with repeated high setup scores in the window. |
| `GET` | `/api/reports/deterioration` | `from`, `to`, `window`, `model`, `min_drop`, `limit`, `offset`, `format` | Largest score drops versus a prior baseline. |
| `GET` | `/api/reports/calibration` | `model`, `horizon`, `bucket_size`, `scoring_model_version`, `slice_by_source_quality`, `source_quality_slice`, `format` | Outcome statistics grouped by score bucket, optionally by source-quality group. |
| `GET` | `/api/reports/model-version-comparison` | `model`, `horizon`, `base_version`, `compare_version`, `bucket_size`, `deterioration_threshold_pct`, `limit`, `offset`, `format` | Paired outcomes for two scoring model versions on the same symbol/time/model/horizon rows. |
| `GET` | `/api/reports/gamma-threshold-review` | `model`, `horizon`, `metrics`, `bucket_size`, `scoring_model_version`, `include_missing`, `limit`, `offset`, `format` | Outcome buckets for gamma-related metrics. |

### JSON report response

Windowed reports return:

```json
{
  "report": "top_new_high_setups",
  "from": "2026-06-09T16:00:00+00:00",
  "to": "2026-06-10T16:00:00+00:00",
  "count": 1,
  "rows": [
    {
      "rank": 1,
      "score_history_id": 123,
      "symbol": "GME",
      "company_name": "Example Corp",
      "created_at": "2026-06-10T15:30:00+00:00",
      "score": 82.0,
      "primary_model": "hybrid",
      "risk_level": "High setup",
      "scoring_model_version": "squeeze-v3"
    }
  ]
}
```

Calibration-style responses include report-specific metadata such as `model`, `horizon`, selected versions, and `rows`.

### CSV examples

```text
GET /api/reports/top-new-high-setups?from=2026-06-09T00:00:00Z&to=2026-06-10T00:00:00Z&min_score=70&format=csv
```

```text
GET /api/reports/gamma-threshold-review?model=gamma_candidate&horizon=1d&metrics=net_gamma_exposure_pct_market_cap,gamma_flip_distance_pct&bucket_size=5&format=csv
```

### Calibration and gamma concepts

- Supported default horizons are `1h`, `4h`, `1d`, `3d`, and `5d`; numeric second-based horizons and simple `h`/`d`/`s` windows are also normalized by analytics helpers.
- Calibration buckets group outcome rows by scan score and compute count, average forward return, win rate, average favorable/adverse excursion, and worst adverse excursion.
- Source-quality slicing can distinguish groups such as true options greeks, premium borrow, Yahoo-only, stale data, or missing-data contexts when persisted source metadata exists.
- Gamma threshold review defaults to metrics such as net/absolute GEX percent of market cap, gamma flip distance, call/put wall distance, gamma concentration, expiration concentration, and open-interest change.

## Providers and status

Operational status endpoints expose cache, provider, scheduler, automation, and application readiness. They should not expose credentials.

### Endpoint table

| Method | Path | Response concept |
| --- | --- | --- |
| `GET` | `/api/health` | Structured health payload for app, storage, cache, provider, scheduler, and automation. |
| `GET` | `/api/status` | Same detailed status payload as health. |
| `GET` | `/api/scheduler/status` | Scheduler runtime state plus `service` automation status. |
| `GET` | `/api/providers` | Default quote provider and optional premium provider capability/status metadata. |
| `GET` | `/api/model` | Scoring model metadata consumed by the browser and scan responses. |

### Health/status payload

`/api/health` and `/api/status` return:

- top-level `status`: `ok` when cache and automation status are ok, otherwise `degraded`
- `generated_at`
- `app`: name, version, `auth_required`, `owner_scoping_available`, `default_owner_configured`
- `storage`: SQLite accessibility metadata from the cache provider
- `cache`: cache health and freshness details
- `provider`: active market-data provider metadata
- `scheduler`: in-process scheduler state plus whether scheduler is enabled by settings
- `automation`: schedule/run/alert/delivery counts and recent failure status

### Scheduler status payload

The scheduler reports:

- `mode: "in_process"`
- `running`
- `poll_interval_seconds`
- `started_at`, `stopped_at`, `last_poll_at`, `last_success_at`, `last_error_at`, `last_error`
- counters: `total_polls`, `total_errors`, `total_runs_started`
- `enabled` from runtime settings and a nested automation `service` status

### Provider status payload

`/api/providers` returns:

- `default_provider`: quote provider health. Yahoo Finance is the built-in available provider.
- `premium_providers`: one status object for each optional feed area: borrow, short interest, corporate actions, filings, and events.

Each provider status includes:

| Field | Meaning |
| --- | --- |
| `feed` | Feed area such as `quote`, `borrow`, or `events`. |
| `provider` | Normalized selected provider name or `disabled`. |
| `enabled` | Whether the feed was selected. |
| `configured` | Whether the selected provider is actually usable. |
| `status` | Examples: `available`, `disabled`, `unconfigured`, `unsupported`. |
| `message` | Human-readable operational explanation. |
| `capability` | Feed fields, whether the feed can affect scores, whether it can create risk flags, and description. |

Optional premium provider selections that lack an adapter or credentials fail explicitly and report `unconfigured`; they should not silently create bullish score inputs. Secrets belong in `.env` or a secrets manager, never in source code or provider status responses.

## AI-agent change notes

- Treat scan response fields as a frontend/API contract. The browser uses model metadata, model scores, confidence, risk flags, deltas, and ranking metadata.
- Preserve the distinction between raw snapshots, derived score history, alert events, and delivery attempts.
- Do not convert `owner_id` metadata into an assumed auth mechanism; add explicit auth before enforcing multi-user security.
- Preserve alert dedupe semantics so active conditions do not spam event or webhook delivery.
- Keep report `format=csv` exporting only visible rows from the JSON report rows.
- Avoid adding language that implies trading recommendations; document software behavior and data contracts only.
