# Squeeze Scanner Documentation

This directory is the documentation-suite entry point for Squeeze Scanner. It is intended to help maintainers, operators, contributors, and AI coding agents quickly understand what the repository does, how its main systems fit together, and where to look before making changes.

Squeeze Scanner is a local-first Python/FastAPI web application for screening ticker symbols with Yahoo Finance market data, SQLite persistence, explainable scoring models, browser-visible results, automation, alerts, and historical analytics. The documentation in this directory describes the engineering behavior of the repository; it is not a trading guide.

## Audience

| Reader | What this index helps you do |
| --- | --- |
| Human maintainers | Navigate the codebase, understand shipped features, and identify the right subsystem before editing. |
| AI coding agents | Build a compact mental model of repository boundaries, invariants, and data flow before generating code. |
| Operators | Find setup, runtime, health, cache, scheduler, and troubleshooting guidance. |
| API and UI contributors | Understand which Python contracts drive HTTP responses, frontend labels, tooltips, filters, and result cards. |
| Provider-adapter authors | Locate the seams for premium quote, borrow, short-interest, option-chain, corporate-action, filing, and event data. |

## Quick reading paths

| If you need to... | Start here | Then read |
| --- | --- | --- |
| Get oriented in five minutes | This page | [`architecture-and-data.md`](architecture-and-data.md) |
| Change API routes or request/response shapes | [`api-automation-and-alerts.md`](api-automation-and-alerts.md) | [`model-context.md`](model-context.md) |
| Modify scoring behavior or model metadata | [`scoring-options-and-providers.md`](scoring-options-and-providers.md) | [`architecture-and-data.md`](architecture-and-data.md) |
| Add or replace market-data providers | [`scoring-options-and-providers.md`](scoring-options-and-providers.md) | [`operations-and-testing.md`](operations-and-testing.md) |
| Operate the service locally | [`operations-and-testing.md`](operations-and-testing.md) | [`api-automation-and-alerts.md`](api-automation-and-alerts.md) |
| Prepare an AI agent for implementation work | [`model-context.md`](model-context.md) | This page, then the document most relevant to the target subsystem |

## Documentation map

| Document | Purpose |
| --- | --- |
| [`architecture-and-data.md`](architecture-and-data.md) | Deep dive into request flow, module boundaries, SQLite stores, raw snapshot history, score history, and data lifecycle. |
| [`scoring-options-and-providers.md`](scoring-options-and-providers.md) | Reference for the `squeeze-v3` scoring model, signal weights, guardrails, confidence, option-chain normalization, true/proxy gamma handling, and provider seams. |
| [`api-automation-and-alerts.md`](api-automation-and-alerts.md) | Guide to FastAPI routes, scan workflows, screens, watchlists, scheduled scans, alerts, delivery attempts, reports, and owner metadata. |
| [`operations-and-testing.md`](operations-and-testing.md) | Operator guide for configuration, startup, health checks, cache maintenance, scheduler checks, troubleshooting, and test commands. |
| [`model-context.md`](model-context.md) | Compact context pack for AI models: repository purpose, key files, invariants, safe-change rules, and common implementation patterns. |

Top-level repository documents remain useful source material: `readme.md` explains setup and API basics, `ARCHITECTURE.md` shows current request/data flow, `ROADMAP.md` records shipped implementation status and remaining integration work, and `runbook.md` provides local operations commands.

## Repository at a glance

| Area | Current behavior |
| --- | --- |
| Runtime | Python 3.11+ package managed with `uv`; console script is `squeeze-scanner`. |
| Web framework | FastAPI serves the browser UI, static assets, model metadata, scan APIs, automation APIs, report APIs, and health/status endpoints. |
| Default data source | Yahoo Finance through `yfinance`, wrapped by provider interfaces and a local cache. |
| Persistence | SQLite stores raw market snapshots, raw snapshot history, derived score history, saved screens, watchlists, schedules, alert rules/events/delivery attempts, price history, and scan outcomes. |
| Scoring | `squeeze-v3` produces four independent 0-100 scores: Classical Short Squeeze, Float Compression, Gamma Candidate, and Hybrid. |
| Automation | Saved screens, watchlists, in-process scheduled scans, alert rules, in-app alert events, and optional noop/webhook delivery are implemented. |
| Analytics | Historical score reports, calibration buckets, delta explanations, model-version comparisons, gamma-threshold review, and CSV export are available through API endpoints. |
| Deployment posture | Local-first and SQLite-backed by default, with owner metadata and provider seams that support future hosted or multi-user integration. |

## Key concepts

### Raw snapshots vs. derived scores

`TickerSnapshot` is the normalized raw market-data contract. It includes quote, volume, short-interest, float, borrow, corporate-action, filing/event, options, gamma, source, freshness, and warning fields. The raw cache stores this provider-facing data so scans can avoid unnecessary network fetches.

`ScanResult` is derived from a snapshot. It contains model scores, components, metrics, confidence, risk flags, rationales, and UI-ready fields. Derived scores are recomputed from raw snapshots whenever scan responses are built so model changes can be reflected without requiring fresh market-data fetches.

### Scoring model metadata is an API contract

The browser does not hard-code the scoring model guide. Signal labels, weights, definitions, calculations, favorability colors, confidence, and guardrail metadata come from Python scoring definitions exposed by `/api/model` and included in scan responses. Changes in `src/squeeze_scanner/scoring.py` therefore affect both backend scoring and frontend explanation text.

### Provider seams preserve Yahoo-only behavior

Yahoo Finance is the built-in quote/market-data path. Optional provider settings exist for premium borrow, short-interest, corporate-action, filing, event, and option-chain data. If a premium provider is selected without an implemented adapter or credentials, the system reports explicit provider status/warnings instead of silently inventing data.

### True gamma and Yahoo proxy gamma are separated

Provider-backed option-chain records with greeks can populate true gamma exposure metrics. Yahoo/public option data can populate proxy fields. Proxy exposure is labeled and kept separate from true GEX fields so scoring, confidence, history, and reports can reason about data quality correctly.

### Automation is persisted but local-first

Schedules, schedule runs, alert rules, alert events, and alert delivery attempts are stored in SQLite. The default scheduler is in-process and appropriate for local deployments. Owner metadata can tag screens, watchlists, schedules, and alerts, but local mode is not the same as authenticated multi-user hosting.

## Repository guarantees and invariants

| Invariant | Why it matters |
| --- | --- |
| Market-data cache rows store raw snapshots, not final scores or rendered UI state. | Scores can be recomputed with the current model, and cached provider data remains reusable. |
| Fresh cached raw data is reused until the configured TTL expires; stale or missing symbols trigger provider fetches. | Scans are faster and less dependent on repeated Yahoo Finance calls. |
| `market_data_history` retains refreshed raw snapshots over time. | Historical scoring, deltas, outcomes, and calibration can be computed from retained source data. |
| Score history stores point-in-time derived results with model version, raw references, confidence, risk, warnings, and metrics. | Reports can compare model versions and explain score changes without losing provenance. |
| Missing premium data lowers confidence or yields warnings; it must not create bullish score credit by itself. | Optional feeds improve signal quality only when real data is present. |
| Provider status endpoints must not expose credentials. | Operational visibility should not leak secrets. |
| Browser, static, and API responses use no-store cache behavior. | Users should see current assets and freshly recomputed response data. |
| `owner_id` is metadata/filtering support, not complete authentication. | Shared/hosted deployments still need real auth and durable multi-user infrastructure. |
| Runtime configuration belongs in `.env` or environment variables, not source code. | Local settings and secrets stay outside version control. |
| Tests are run with `uv run pytest`. | Validation should use the repository's existing toolchain. |

## Current-state summary

The core local scanner is implemented. The application can scan explicit symbols or Yahoo's predefined most-shorted universe, cache raw market snapshots, recompute four-model scores, show explainable browser cards, rank/filter results, delete cached symbols, persist saved screens and watchlists, run scheduled scans, evaluate alerts, record alert delivery attempts, and expose historical analytics/reporting endpoints.

The architecture is intentionally modular: domain dataclasses and protocols define provider and result contracts; cache/history/screen/automation/analytics stores isolate SQLite persistence; service code normalizes symbols, orchestrates scans, ranks results, and records history; scoring code owns model definitions and rationales; web code wires FastAPI routes and application services.

The main remaining work is integration-oriented rather than core-platform work: implement real vendor adapters for premium borrow, short-interest, filings/corporate actions, events, and true options greeks; collect enough provider-backed history to calibrate thresholds; and add hosted-deployment pieces such as authentication, a multi-user database, external workers/queues, centralized observability, and additional native notification channels if needed.
