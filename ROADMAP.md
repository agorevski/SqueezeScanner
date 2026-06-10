# Squeeze Scanner Implementation Status

This document records the roadmap items that have been implemented in the local FastAPI/SQLite scanner. The scanner remains informational only and is not financial advice.

## Shipped status

| Area | Status | Implementation |
| --- | --- | --- |
| Historical score platform | Shipped | `scan_score_history` stores derived scores, model components, rationales, metrics, confidence, risk flags, warnings, model version, scan run id, and raw snapshot references. |
| Score deltas | Shipped | Scan responses include `previous_scan_delta`, `delta_24h`, baseline scores/timestamps, and a `score_delta_status`. `/api/scans/{symbol}/deltas` returns structured explainable deltas. |
| Ranking modes | Shipped | Scan responses support top score, selected model score, model confidence, 1h/24h score increases, relative volume, short interest, smallest float, hybrid-only, and gamma-only rankings. |
| Saved screens | Shipped | `saved_screens` persists structured filter/ranking JSON and is exposed through `/api/screens`. |
| Watchlists | Shipped | `watchlists` and `watchlist_symbols` persist local symbol lists, support symbol add/remove, and can be scanned through `/api/watchlists/{watchlist_id}/scan`. |
| Scheduled scans | Shipped | `scheduled_scans` and `scheduled_scan_runs` support in-process local schedules for saved screens, watchlists, Yahoo most-shorted, and explicit symbol lists. |
| Alerts | Shipped | `alerts` and `alert_events` support score/model/delta/relative-volume/short-interest/float/gamma rules with deduplication until a condition clears. |
| Data feed seams | Shipped | Domain provider protocols now cover quote, short interest, borrow, options, corporate actions, and filings. Yahoo remains the default provider. |
| Source provenance and quality | Shipped | Snapshots carry `field_sources`, `field_quality`, `source_quality`, `source_fetched_at`, and source warnings. Cached legacy JSON still loads. |
| Model confidence | Shipped | Each scan returns `model_confidence` and `confidence_rationales` per scoring model. Missing premium data reduces confidence instead of creating bullish signals. |
| Risk and liquidity guardrails | Shipped | Results include configurable `risk_flags` for price, dollar volume, average volume, market cap, missing data, reverse splits, source warnings, and low data quality. |
| Backtesting and calibration | Shipped | `price_history` and `scan_outcomes` support delayed outcome computation, no-lookahead guards, score-bucket calibration reports, and outcome metrics. |
| Historical reports | Shipped | Analytics queries and HTTP report endpoints cover top new high setups, biggest 1h/24h increases, repeated high setups, deterioration, and calibration buckets. |
| Browser UI | Shipped | The UI exposes ranking, saved screens, watchlists, deltas, confidence badges, risk badges, and roadmap panels for alerts, scheduler, reports, and history. |

## Current data model

| Table | Purpose |
| --- | --- |
| `market_data_cache` | Latest raw market snapshot per provider/symbol. |
| `market_data_history` | Refreshed raw snapshots retained over time. |
| `scan_score_history` | Point-in-time derived scores, model details, confidence, risk, metrics, warnings, raw references, and scoring version. |
| `saved_screens` | Local saved filter/ranking definitions as JSON. |
| `watchlists` | Local watchlist metadata. |
| `watchlist_symbols` | Watchlist symbol membership. |
| `scheduled_scans` | In-process schedule definitions. |
| `scheduled_scan_runs` | Scheduled run status, timing, symbols, errors, and response payloads. |
| `alerts` | Local alert rule definitions. |
| `alert_events` | Deduplicated alert events and acknowledgement/clear state. |
| `price_history` | Historical OHLCV bars used for backtesting. |
| `scan_outcomes` | Forward return and excursion outcomes by scan row, model, version, and horizon. |

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/scans/recent` | Return recent cached scans with ranking and delta fields. |
| `GET` | `/api/scans/history` | Query historical score rows across symbols. |
| `GET` | `/api/scans/{symbol}/history` | Query historical score rows for one symbol. |
| `GET` | `/api/scans/{symbol}/deltas` | Return structured delta explanations for a symbol. |
| `POST` | `/api/scans/recompute` | Recompute historical raw snapshots with the current scoring model. |
| `POST` | `/api/scan` | Scan requested symbols with optional ranking options. |
| `POST` | `/api/scan/most-shorted` | Scan Yahoo's most-shorted universe with optional ranking options. |
| `GET/POST/PUT/DELETE` | `/api/screens` and `/api/screens/{screen_id}` | Manage saved screens. |
| `GET/POST/PUT/DELETE` | `/api/watchlists` and `/api/watchlists/{watchlist_id}` | Manage watchlists. |
| `GET/POST/DELETE` | `/api/watchlists/{watchlist_id}/symbols` | Manage watchlist symbols. |
| `POST` | `/api/watchlists/{watchlist_id}/scan` | Scan a saved watchlist. |
| `GET/POST/PATCH/DELETE` | `/api/scheduled-scans` and `/api/scheduled-scans/{schedule_id}` | Manage scheduled scans. |
| `POST` | `/api/scheduled-scans/{schedule_id}/run` | Run a schedule immediately. |
| `GET` | `/api/scheduled-scan-runs` | List scheduled scan runs. |
| `GET/POST/PATCH/DELETE` | `/api/alerts` and `/api/alerts/{alert_id}` | Manage alert rules. |
| `GET` | `/api/alert-events` | List alert events. |
| `POST` | `/api/alert-events/{event_id}/ack` | Acknowledge an alert event. |
| `GET` | `/api/reports` | List available historical reports. |
| `GET` | `/api/reports/top-new-high-setups` | Query new high setup report rows. |
| `GET` | `/api/reports/biggest-1h-increases` | Query biggest 1-hour score increases. |
| `GET` | `/api/reports/biggest-24h-increases` | Query biggest 24-hour score increases. |
| `GET` | `/api/reports/repeated-high-setups` | Query repeated high setup rows. |
| `GET` | `/api/reports/deterioration` | Query score deterioration rows. |
| `GET` | `/api/reports/calibration` | Query calibration buckets by model and horizon. |

## Gamma/options implementation note

The scanner now has provider seams, source-quality metadata, options confidence, options risk handling, Yahoo option-chain aggregation, and a dealer-gamma exposure proxy. Full expiry-aware greeks require a configured options provider that supplies greeks, implied volatility, strike/expiry concentration, and open-interest changes. Until that provider is added, Gamma Candidate remains a confidence-aware public-data model rather than a true dealer-positioning model.

## Validation coverage

Unit tests cover:

- symbol normalization and scoring metadata
- raw cache TTL/history behavior and legacy cache compatibility
- score-history writes, recompute behavior, version retention, confidence/risk persistence, and deltas
- saved screen and watchlist CRUD plus ranking semantics
- scheduled scan persistence, run success/failure logging, and alert dedupe/reset behavior
- model confidence, source provenance, guardrail risk flags, and Yahoo-only compatibility
- backtesting outcome delay/no-lookahead behavior, calibration buckets, deterministic delta drivers, historical reports, CSV export, and report/delta API route behavior

## Intentional local-deployment constraints

- Storage is local SQLite and single-user by design.
- The scheduler is in-process and intended for local deployments.
- Alert delivery is in-app only; email, webhook, Slack/Discord, and SMS are not configured.
- Yahoo Finance remains the built-in data provider. Premium borrow, filings, halt, corporate-action, quote, and options-greeks feeds can be added through the provider protocols without committing credentials.
