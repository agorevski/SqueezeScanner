# Model Context for SqueezeScanner

This guide is for humans and AI coding agents who need to understand or safely extend this repository. The application is informational software for screening market-data patterns; do not present scanner output as financial advice.

## Repository purpose

SqueezeScanner is a Python 3.11 FastAPI web application that screens ticker symbols for potential squeeze setup characteristics using public Yahoo Finance data through `yfinance`. It serves a browser UI, JSON APIs, local SQLite persistence, scheduled scans, alerts, and historical reporting. The default deployment is local/zero-config: copy `.env.example` to `.env`, run with `uv`, and store app data in a local SQLite database under `data/`.

The scanner computes four independent model scores for every ticker:

- Classical Short Squeeze
- Float Compression
- Gamma Candidate
- Hybrid

Scores and risk labels are model outputs for ranking and explanation only. The app keeps raw market data and derived score history separate so scores can be recomputed when model logic changes.

## High-level request flow

1. Browser or API client calls `/api/scan`, `/api/scan/most-shorted`, a watchlist scan, or a scheduled scan.
2. `ScannerService` normalizes symbols, fetches snapshots concurrently, scores each snapshot, persists score history when possible, computes deltas, and shapes the response.
3. `CachedMarketDataProvider` returns fresh raw snapshots from SQLite when available; otherwise it calls the composite provider and stores the refreshed raw snapshot.
4. `CompositeMarketDataProvider` starts with Yahoo Finance data and overlays optional premium-provider patches only when those providers are explicitly enabled and implemented.
5. `score_snapshot()` recomputes all model scores from the raw `TickerSnapshot` and returns a `ScanResult` with metrics, components, rationale, confidence, warnings, and risk flags.
6. The frontend renders the response and applies browser-side filtering/ranking without deleting cache rows unless the user explicitly calls the delete endpoint.

## Core file map

| Path | Responsibility |
| --- | --- |
| `pyproject.toml` | Project metadata, dependencies, console script, pytest configuration. Use `uv`. |
| `.env.example` | Documented runtime variables. Copy to `.env` for local configuration; do not commit secrets. |
| `readme.md`, `ARCHITECTURE.md`, `runbook.md`, `ROADMAP.md` | Human-facing setup, architecture, operations, and roadmap documentation. |
| `app/` | Thin compatibility shims for older imports such as `app.main:app`, `app.scanner`, `app.cache`, and `app.server`. Do not add core logic here. |
| `src/squeeze_scanner/config.py` | Loads `.env` from the current project root and resolves settings such as host, port, cache DB, providers, scheduler, owner tag, and alert delivery. |
| `src/squeeze_scanner/domain.py` | Shared dataclasses, protocols, and errors: `TickerSnapshot`, option-chain records, provider protocols, guardrails, `ScanResult`, and provider health metadata. |
| `src/squeeze_scanner/providers/yahoo.py` | Built-in `yfinance` quote/options/screener adapter and Yahoo most-shorted screener. Yahoo is the only implemented quote provider. |
| `src/squeeze_scanner/providers/premium.py` | Provider extension seam for borrow, short interest, corporate actions, filings, and events. Also merges premium patches into base Yahoo snapshots. |
| `src/squeeze_scanner/cache.py` | SQLite raw-market-data cache. Stores latest raw snapshots and refreshed raw snapshot history; never stores rendered scan results. |
| `src/squeeze_scanner/options.py` | Option-chain normalization plus true gamma exposure aggregation from contract-level greeks and open interest. |
| `src/squeeze_scanner/scoring.py` | Source of truth for model metadata, scoring weights, score calculations, confidence, guardrails, risk flags, rationale, and gamma/proxy logic. |
| `src/squeeze_scanner/service.py` | Symbol normalization, scan orchestration, response shaping, ranking modes, guardrail filtering, score-history recording, deltas, and recompute support. |
| `src/squeeze_scanner/history.py` | SQLite `scan_score_history` persistence and score-delta queries. Derived scores live here, not in the raw cache tables. |
| `src/squeeze_scanner/analytics.py` | SQLite analytics, calibration, historical reports, CSV export, delta explanations, and outcome logic. It reads stored score/price history rather than live providers. |
| `src/squeeze_scanner/screens.py` | SQLite saved screens and watchlists, including optional `owner_id` filters and watchlist scan helper. |
| `src/squeeze_scanner/automation.py` | SQLite scheduled scans, alert rules/events, delivery attempts, and the in-process polling scheduler. |
| `src/squeeze_scanner/alert_delivery.py` | Alert delivery channels. Built-ins are `noop` dry-run and optional webhook POST delivery. |
| `src/squeeze_scanner/web.py` | FastAPI app factory, Pydantic request models, route wiring, static/template mounting, health/status payloads, and no-cache middleware. |
| `src/squeeze_scanner/server.py` | `squeeze-scanner` console-script entrypoint for Uvicorn. |
| `src/squeeze_scanner/static/`, `templates/` | Browser UI assets. The frontend reads model definitions from `/api/model` and scan response `model` blocks. |
| `tests/` | Unit/integration tests for scoring, caching, providers, options, screens/watchlists, automation, analytics, deployment status, and risk/confidence behavior. |

## Important data objects

- `TickerSnapshot` is the normalized raw-market-data record passed from providers to scoring. Add new provider fields here with safe defaults (`None`, `default_factory`) so old cached JSON remains readable.
- `OptionChainRecord` and `OptionChainSnapshot` are normalized option-chain inputs. Provider adapters may pass mappings or dataclass instances; `options.py` handles common aliases.
- `ScanResult` is the derived scored output. It contains `score`, `primary_model`, per-model scores/components/rationales, metrics, warnings, source provenance, model confidence, and risk flags.
- `ProviderCapability` and `ProviderHealth` describe provider status without exposing credentials.
- `GuardrailConfig` defines default risk/liquidity thresholds. Guardrails create flags and confidence adjustments; they should not silently rewrite raw data.

## Scoring and data invariants

- The current scoring model version is `squeeze-v3` (`SCORING_MODEL_VERSION`). If score semantics materially change, update tests and consider versioning impacts on persisted history and reports.
- Every scan computes all four models independently on a 0-100 scale. `primary_model` is simply the model with the highest score; it must not erase the other model scores.
- Each model's signal weights sum to 100. Tests assert this, and the frontend assumes `/api/model` is the source of truth for labels, weights, tooltips, calculations, and color legend.
- Missing or unavailable data scores as zero for that signal and reduces confidence. Never fabricate bullish fallback values for missing premium data.
- Yahoo does not provide borrow fees, filing-derived dilution risk, halt/event feeds, or true dealer gamma positioning. Those fields remain missing/zero unless a provider supplies them with provenance.
- `data_quality` is based on core fields such as price, volume, average volume, short interest, days to cover, float/market cap, and momentum. Low score and low confidence are separate concepts.
- Risk flags expose guardrail concerns such as low liquidity, missing data, reverse split, dilution/offering risk, halt/event risk, source warnings, and low data quality. High-risk names are flagged, not automatically removed, unless a caller passes guardrail filters.
- Scan responses include `model` metadata and `ranking` details. Keep API and frontend ranking names aligned when adding ranking modes.
- Analytics/outcome code must use persisted point-in-time score rows and price bars. Do not call live providers while computing historical outcomes; that risks lookahead bias.

## Storage conventions

The default database is `data/market_data_cache.sqlite3`, configurable through `SQUEEZE_SCANNER_CACHE_DB`. The `data/` directory and SQLite files are gitignored.

- `market_data_cache` stores the latest raw `TickerSnapshot` JSON per `(provider, symbol)`, with `fetched_at` and `scanned_at` timestamps.
- `market_data_history` appends refreshed raw snapshots and is keyed by `(provider, symbol, fetched_at)`.
- The raw cache must not store model score, risk level, components, rationale, or rendered UI state.
- Cache freshness is controlled by `SQUEEZE_SCANNER_CACHE_TTL_SECONDS` and defaults to one hour. Fresh cache hits update `scanned_at` but do not create new raw history rows.
- `scan_score_history` stores derived score rows separately, deduped by provider, symbol, raw fetch time, scoring model version, and scan run id.
- Screens, watchlists, schedules, alerts, delivery attempts, price history, analytics outcomes, and reports share the same SQLite file but are managed by their own store/service classes.
- Schema migrations are lightweight and local: `_ensure_schema()` methods create tables/indexes and add missing columns. When adding columns, preserve existing databases and tests that simulate older schemas.

## Provider extension rules

Use `domain.py` protocols and `providers/premium.py` seams rather than coupling vendor logic into scoring or web routes.

- Keep Yahoo as the built-in zero-config provider unless intentionally adding a new quote-provider implementation in `build_market_data_provider()`.
- Premium feed names are normalized from environment variables. Disabled/unimplemented providers must fail explicitly, add source warnings/provenance, and not create bullish scores.
- A provider patch should return a `TickerSnapshot` with only fields it can support plus `field_sources`, `field_quality`, `source_quality`, `source_warnings`, and `source_fetched_at` metadata.
- `merge_ticker_snapshots()` overlays non-`None` patch fields onto the base snapshot, merges metadata, and keeps warnings unique. It does not merge arbitrary non-snapshot metadata.
- Event providers are risk-flag providers. They currently create halt/news/event flags and do not directly affect model scores.
- If adding a credentialed adapter, keep credentials in environment variables or a secrets manager. Do not expose them through `/api/providers`, logs, tests, docs examples, or source control.
- Add tests for provider status, disabled/unconfigured behavior, merge behavior, provenance, and scoring impact. Existing patterns live in `tests/test_premium_providers.py`.

## Gamma and proxy separation rules

This repository deliberately separates public-options proxy data from provider-backed true gamma exposure.

- Yahoo option-chain data populates call/put volume, call/put open interest, and `dealer_gamma_exposure_proxy`. The source is `yahoo_finance_options_proxy` and capabilities do not include true gamma exposure.
- Yahoo proxy exposure is a near-money open-interest proxy (`openInterest * 100 * price` within a strike window). It is not true dealer gamma exposure and must never be copied into true-GEX fields.
- True gamma fields include `call_gamma_exposure`, `put_gamma_exposure`, `net_gamma_exposure`, `absolute_gamma_exposure`, `gamma_exposure_pct_market_cap`, gamma flip, walls, and concentration fields.
- True gamma aggregation requires contract-level gamma, open interest, side, strike, expiration, and spot price. Calls are signed positive, puts negative, and absolute exposure keeps total magnitude.
- `snapshot_with_true_gamma_metrics()` may derive true gamma metrics from provider option-chain records with greeks. It intentionally avoids promoting Yahoo proxy-only data to true GEX.
- Scoring uses true gamma when provider-backed fields/capabilities are present. Proxy snapshots may receive weaker fallback credit for exposure, call/put skew, and concentration, but receive no gamma-flip credit.
- API metrics include source type flags such as `gamma_exposure_source_type`, `gamma_exposure_is_true`, and `gamma_exposure_is_proxy`. Preserve these when changing gamma behavior.
- Tests covering this separation live in `tests/test_options.py` and `tests/test_confidence_risk.py`.

## API conventions

- `web.py` owns route registration through `create_app()`, and module-level `app = create_app()` supports Uvicorn and compatibility imports.
- Request bodies use Pydantic models in `web.py`. Blocking provider, SQLite, and scanning work is run through `asyncio.to_thread()` from async routes.
- `/api/health`, `/api/status`, and `/api/scheduler/status` expose local readiness and scheduler/provider/cache state.
- `/api/model` exposes scoring metadata. The frontend should not duplicate model labels, weights, calculations, or legends.
- `/api/scans/recent` recomputes scores from recent raw cached snapshots. `/api/scans/recompute` recomputes historical raw snapshots with the current model.
- `/api/scans/{symbol}` delete removes a ticker from the latest raw cache so it disappears from the current UI. It does not purge all historical analysis rows.
- Screens, watchlists, schedules, and alerts accept optional `owner_id` metadata. Treat it as an integration hook for future auth, not as security by itself.
- Browser, static, and API responses use `Cache-Control: no-store` to reduce stale UI/API behavior.

## Configuration and secrets

- Runtime configuration is loaded from `.env` in the repository working directory. Commands should be run from the repository root so relative paths resolve as expected.
- `.env.example` is the public schema for supported variables. Update it when adding a new runtime variable.
- `.env`, `.env.*`, local databases, runtime data, logs, and virtual environments are gitignored. Keep webhook URLs, provider credentials, and other secrets out of committed files.
- Alert delivery defaults to no external delivery. `SQUEEZE_SCANNER_ALERT_DELIVERY_CHANNELS=noop` is dry-run; `webhook` requires `SQUEEZE_SCANNER_ALERT_WEBHOOK_URL`.
- `/api/providers` should expose capability/status metadata only, never credentials or secret-derived values.

## Tests to run for common changes

Install dependencies first:

```bash
uv sync
```

Run the full suite before handing off broad changes:

```bash
uv run pytest
```

Targeted test commands:

| Change area | Suggested tests |
| --- | --- |
| Scoring model, score metadata, ranking, scanner response | `uv run pytest tests/test_scanner.py tests/test_confidence_risk.py tests/test_screens.py` |
| Gamma/options/provider-backed greeks | `uv run pytest tests/test_options.py tests/test_confidence_risk.py` |
| Premium provider seams and provenance | `uv run pytest tests/test_premium_providers.py tests/test_confidence_risk.py` |
| Raw cache and score history | `uv run pytest tests/test_scanner.py tests/test_history.py tests/test_analytics.py` |
| Analytics reports, deltas, CSV | `uv run pytest tests/test_analytics.py` |
| Saved screens and watchlists | `uv run pytest tests/test_screens.py` |
| Schedules, alerts, delivery attempts | `uv run pytest tests/test_automation.py` |
| App status/config/deployment readiness | `uv run pytest tests/test_deployment_status.py` |

The project does not define a separate lint command in `pyproject.toml`; do not add one unless the task explicitly asks for tooling changes.

## Safe-edit guidance for future AI agents

- Check `git status` before editing. This repository may contain uncommitted work from other agents or users; do not overwrite unrelated changes.
- Keep core logic under `src/squeeze_scanner/`. Only adjust `app/` shims when intentionally preserving or changing backwards-compatible imports.
- Add dataclass fields with defaults and update JSON read/write paths so old cache rows and old SQLite schemas still work.
- Keep model metadata in `scoring.py`; update frontend code only to render metadata, not to redefine the model.
- Use parameterized SQLite queries. If dynamic SQL is required for optional filters, build only trusted clauses and pass values separately.
- Avoid network-dependent unit tests. Use fake providers, fake screeners, in-memory/temp test databases, and deterministic timestamps as existing tests do.
- Preserve raw/provenance separation: providers produce raw fields plus metadata; scoring produces derived scores; history stores derived rows; analytics reads persisted history.
- Do not convert warnings or missing provider data into positive score contributions. Missing data should either score zero, reduce confidence, or create risk/source warnings.
- When adding public API fields, consider frontend rendering, score history serialization, analytics CSV/report output, and backward compatibility for existing cached JSON.
- Keep all user-facing language informational. Avoid recommendations to buy, sell, short, hold, or otherwise trade securities.

## Common pitfalls

- The repository documentation suite lives in `documentation/`; keep internal links and top-level references aligned if files move.
- `PROJECT_ROOT` is based on the current working directory, so running the app outside the repository root can move relative database paths.
- Yahoo public option data is not true gamma exposure. Preserve `dealer_gamma_exposure_proxy` and true-GEX fields separately.
- Borrow fees, dilution risk, halt risk, and filing/event signals are absent unless a provider supplies them. Do not infer them from unrelated Yahoo fields.
- `owner_id` filtering is not authentication. Do not treat it as an access-control boundary without adding real auth.
- Frontend filters are mostly client-side visibility controls. They should not delete SQLite rows unless wired to explicit delete endpoints.
- Cache hits are fast because raw snapshots are reused; scores are still recomputed. Do not cache score objects in `market_data_cache` to optimize response time.
