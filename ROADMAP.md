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
| Alerts | Shipped | `alerts`, `alert_events`, and `alert_delivery_attempts` support score/model/delta/relative-volume/short-interest/float/gamma rules, in-app event deduplication until a condition clears, and optional noop/webhook external delivery. |
| Data feed seams | Shipped | Domain provider protocols now cover quote, short interest, borrow, options, corporate actions, and filings. Yahoo remains the default provider. |
| Options data contract | Shipped | Normalized option-chain records and snapshots support expiration, DTE, strike, side, bid/ask, last price, volume, open interest, OI change, IV, greeks, timestamps, provider/source metadata, capabilities, and warnings. |
| True GEX aggregation | Shipped | Provider-backed contract greeks can be aggregated into call/put/net/absolute GEX, GEX percent of market cap, gamma flip, walls, largest strike/expiry, concentration, and OI-change metrics while preserving Yahoo proxy fields separately. |
| Gamma scoring and UI explainability | Shipped | Gamma Candidate scoring now blends true/proxy exposure, near-dated GEX, flip proximity, call/put skew, walls/concentration, OI change, and confidence; browser cards expose source-aware Gamma details. |
| Premium provider foundation | Shipped | Optional borrow, short-interest, corporate-action, filing, and halt/news/event provider settings, unconfigured adapters, source-error provenance, and `/api/providers` status metadata are available without changing Yahoo-only behavior. |
| Source provenance and quality | Shipped | Snapshots carry `field_sources`, `field_quality`, `source_quality`, `source_fetched_at`, and source warnings. Cached legacy JSON still loads. |
| Model confidence | Shipped | Each scan returns `model_confidence` and `confidence_rationales` per scoring model. Missing premium data reduces confidence instead of creating bullish signals. |
| Risk and liquidity guardrails | Shipped | Results include configurable `risk_flags` for price, dollar volume, average volume, market cap, missing data, reverse splits, source warnings, and low data quality. |
| Backtesting and calibration | Shipped | `price_history` and `scan_outcomes` support delayed outcome computation, no-lookahead guards, score-bucket calibration reports, and outcome metrics. |
| Historical reports | Shipped | Analytics queries and HTTP report endpoints cover top new high setups, biggest 1h/24h increases, repeated high setups, deterioration, calibration/source-quality buckets, model-version comparisons, gamma threshold review, and CSV exports. |
| Deployment hardening foundations | Shipped | Local zero-config mode now includes store/service boundaries, SQLite migrations, owner metadata/filtering, default-owner config, scheduler/provider/cache status, and health payloads that expose auth/storage/scheduler/automation readiness. |
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
| `alert_delivery_attempts` | Per-event external alert delivery status, retry count, errors, and last-attempt timing. |
| `price_history` | Historical OHLCV bars used for backtesting. |
| `scan_outcomes` | Forward return and excursion outcomes by scan row, model, version, and horizon. |

Option-chain contracts are embedded in raw snapshot JSON for cache/history compatibility. Derived true/proxy gamma aggregates and source-quality metadata are persisted in `scan_score_history.metrics_json` for reports and calibration.

## API surface

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/providers` | Return default and optional provider capability/status metadata. |
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
| `GET` | `/api/alert-delivery-attempts` | List external alert delivery attempts. |
| `POST` | `/api/alert-delivery-attempts/{attempt_id}/retry` | Retry a persisted alert delivery attempt. |
| `GET` | `/api/reports` | List available historical reports. |
| `GET` | `/api/reports/top-new-high-setups` | Query new high setup report rows. |
| `GET` | `/api/reports/biggest-1h-increases` | Query biggest 1-hour score increases. |
| `GET` | `/api/reports/biggest-24h-increases` | Query biggest 24-hour score increases. |
| `GET` | `/api/reports/repeated-high-setups` | Query repeated high setup rows. |
| `GET` | `/api/reports/deterioration` | Query score deterioration rows. |
| `GET` | `/api/reports/calibration` | Query calibration buckets by model and horizon. |
| `GET` | `/api/reports/model-version-comparison` | Compare paired outcomes for two scoring model versions. |
| `GET` | `/api/reports/gamma-threshold-review` | Review outcome buckets for gamma metric thresholds. |

## Gamma/options implementation status

The scanner/platform pieces of the options plan are shipped:

- `OptionChainRecord` and `OptionChainSnapshot` normalize provider rows with symbol, expiration, DTE, strike, side, bid/ask, last price, volume, open interest, OI change, IV, delta, gamma, timestamps, provider/source metadata, capabilities, and warnings.
- Yahoo options remain a public-data proxy. `dealer_gamma_exposure_proxy` is marked estimated and is never promoted into true GEX fields.
- Provider-backed records with greeks can populate true GEX fields using `abs(gamma) * open_interest * 100 * spot^2 * 0.01`, with calls signed positive and puts negative.
- Aggregation covers call/put/net/absolute GEX, GEX percent of market cap, gamma flip distance, max gamma strike, call/put walls, largest gamma expiration, concentration, and OI-change metrics.
- Gamma Candidate scoring and confidence now distinguish true greeks from proxy data and blend exposure, near-dated GEX, flip proximity, call/put skew, walls/concentration, OI change, and option-chain freshness.
- Browser result cards expose Gamma detail chips for source, GEX percent of market cap, net/near GEX, flip distance, call-gamma share, walls, largest expiry, and OI change when those metrics exist.
- Historical raw snapshots and score history retain option metrics plus field/source quality for later calibration.

**Remaining external integration work:** select and credential an options data vendor that supplies contract greeks, IV surfaces, OI changes, and timestamps; implement the adapter against the existing option-chain contract; then collect provider-backed history and use the calibration reports to tune thresholds against observed outcomes.

## Premium data-feed integrations

**Goal:** Improve confidence and reduce missing-data penalties by adding optional providers for data Yahoo Finance does not supply.

| Feed area | Why it matters | External adapter work remaining |
| --- | --- | --- |
| Borrow and securities lending | Borrow fee is a major Classical Short Squeeze and Hybrid input, but Yahoo does not provide it. | Implement `BorrowProvider` adapters for borrow fee, availability, utilization, and rebate-rate fields. Mark stale borrow data explicitly and keep missing borrow data from creating bullish signals. |
| Short interest | Exchange-reported short interest can be delayed, and availability varies by symbol. | Add provider-specific short-interest timestamps, settlement dates, and revision metadata. Prefer fresher premium data over Yahoo when configured. |
| Corporate actions and filings | Reverse splits, dilution, offerings, warrants, and ATM programs can materially change float-compression risk. | Add filing/corporate-action provider adapters and convert dilution/reverse-split findings into explicit risk flags and confidence adjustments. |
| Halt/news/events | Halt risk, offering announcements, and major event catalysts affect squeeze behavior and tradability. | Add optional event providers that create warnings/risk flags without changing core scores unless calibrated. |

**Implementation status:** The optional provider foundation is shipped: `.env` settings select disabled or named providers, placeholder adapters fail explicitly when selected without an implementation/credentials, scan warnings preserve Yahoo fallback behavior, source provenance identifies provider-populated fields, and `/api/providers` exposes capability/status metadata without leaking credentials.

**Remaining external integration work:** implement actual vendor adapters for borrow/securities lending, fresher short-interest feeds, corporate-action/filing feeds, halt/news/event feeds, and true options-greeks feeds. Credentials should stay in `.env` or a secrets manager, not source control.

## Alert delivery beyond in-app events — shipped

**Goal:** Turn existing persisted alert events into actionable notifications outside the browser.

| Phase | Work | Acceptance criteria |
| --- | --- | --- |
| 1. Notification abstraction | Shipped: `AlertDeliveryService` separates alert evaluation from noop/webhook delivery channels without hard-coding credentials. | Alert evaluation remains separate from alert delivery, and each delivery attempt is logged. |
| 2. Delivery configuration | Shipped: `.env` settings configure default channels/webhook details, and alert API payloads expose `delivery_channels` for per-alert routing. | A user can choose in-app only or one or more external channels per alert. |
| 3. Retry and dedupe | Shipped: `alert_delivery_attempts` persists status, failures, retry count, and last-attempt time; retries are exposed through the API and condition dedupe prevents notification spam. | Failed delivery is visible and retryable; active alert conditions do not repeatedly send duplicate notifications. |
| 4. Message templates | Shipped: external payloads include symbol, score, trigger message, current value, threshold, confidence, risk flags, and a browser link. | External notifications provide enough context to decide whether to open the scanner. |

## Deployment, persistence, and multi-user hardening — shipped foundation

**Goal:** Preserve the simple local deployment path while making the app safer for longer-running or shared deployments.

| Area | Implemented now | Remaining hosted/shared integration |
| --- | --- | --- |
| Database portability | SQLite remains the default; cache, history, screens, automation, and analytics are behind store/service APIs with schema migrations and status reporting. | Add a PostgreSQL adapter and migration path if concurrent hosted users require it. |
| Auth and users | `owner_id` metadata and filters exist for screens, watchlists, schedules, and alerts; `SQUEEZE_SCANNER_DEFAULT_OWNER_ID` can tag new resources; health reports `auth_required=false` and `owner_scoping_available=true`. | Add real authentication/session enforcement and user provisioning before running a shared multi-user service. |
| Scheduler reliability | Schedules and run history persist in SQLite; the local scheduler reports mode, poll counts, due schedules, failures, and last success. | Add an external worker/queue/cron integration if scans must survive web-process restarts in hosted mode. |
| Secrets and configuration | Runtime settings are documented in `.env.example`; provider status payloads avoid credentials; webhook URLs stay in `.env`. | Use a production secrets manager and vendor-specific credential docs for hosted deployments. |
| Observability | `/api/health`, `/api/status`, `/api/scheduler/status`, and `/api/providers` expose storage/cache/provider/scheduler/automation/delivery health. | Add centralized logs, metrics, tracing, and alerting if operating as a production service. |

## Reporting and calibration upgrades — shipped

**Goal:** Use the historical platform to measure whether each new data source or model change improves signal quality.

| Work | Acceptance criteria |
| --- | --- |
| Add model-version comparison reports so `squeeze-v3` and alternate gamma-enabled versions can be compared on the same historical outcomes. | Reports show old score, new score, bucket, forward return, forward excursion, and deterioration by model version. |
| Add source-quality slices to calibration reports. | Performance can be compared for Yahoo-only, premium borrow, true options greeks, stale data, and missing-data groups. |
| Add threshold-review reports for gamma-specific metrics such as net GEX percent of market cap, gamma flip distance, call-wall concentration, and OI change. | Gamma thresholds are tuned from observed scanner history rather than manually guessed. |
| Add exportable CSV/JSON report outputs for external analysis. | Calibration and report data can be reviewed in notebooks or spreadsheets without querying SQLite directly. |

**Implementation status:** Shipped: analytics reports now pair scan outcomes by model version, optionally slice calibration buckets by source-quality groups, review gamma threshold buckets from persisted metrics JSON, and expose JSON by default with `format=csv` for report-row exports.

## Remaining integration plan

The core local scanner implementation is complete. Remaining work is external/vendor or hosted-deployment integration:

1. Select options/borrow/short-interest/corporate-action/filing/event data vendors and map their credentials through environment variables or a secrets manager.
2. Implement vendor adapters against the existing provider protocols and option-chain contract.
3. Run provider-backed scans long enough to populate `scan_score_history`, `price_history`, and `scan_outcomes`.
4. Use calibration, model-version comparison, and gamma-threshold reports to tune thresholds from observed outcomes instead of assumptions.
5. For shared/hosted deployment, add external auth, PostgreSQL or another durable multi-user store, a worker/queue scheduler, and centralized observability.
6. Add native delivery providers such as Slack, Discord, email, or SMS only if webhook/noop delivery is insufficient.

## Validation coverage

Unit tests cover:

- symbol normalization and scoring metadata
- option-chain normalization, provider alias parsing, true-GEX math/signs/concentration, true/proxy gamma separation, and Yahoo-proxy non-promotion
- raw cache TTL/history behavior and legacy cache compatibility
- score-history writes, recompute behavior, version retention, confidence/risk persistence, and deltas
- saved screen and watchlist CRUD plus ranking semantics
- scheduled scan persistence, owner metadata/filtering, schema migrations, scheduler status, run success/failure logging, alert dedupe/reset behavior, and external alert delivery status/failure persistence
- premium-provider defaults/status, explicit unconfigured-provider failures, source provenance, model confidence, guardrail risk flags, and Yahoo-only compatibility
- backtesting outcome delay/no-lookahead behavior, calibration/source-quality buckets, model-version comparisons, gamma threshold review, deterministic delta drivers, historical reports, CSV export, and report/delta API route behavior
- deployment status payloads for storage, cache, scheduler, automation, auth-required state, and owner-scoping availability

There is no dedicated browser-automation test suite; frontend validation is covered indirectly through the API/model contracts it consumes.

## Intentional local-deployment constraints

- Storage is local SQLite and single-user by design.
- The scheduler is in-process and intended for local deployments.
- Alert delivery is disabled by default and can be enabled locally with noop dry-run or webhook channels through `.env`; email, Slack/Discord-native, and SMS providers remain extension points.
- Yahoo Finance remains the built-in data provider. Premium borrow, short-interest, filings, halt/news/event, corporate-action, quote, and options-greeks feeds can be added through the provider protocols and optional provider factories without committing credentials.
