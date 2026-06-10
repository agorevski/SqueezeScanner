# Scoring, Options, and Provider Data Flow

This document explains how this repository turns raw market/provider data into informational squeeze-scanner scores. It is written for both human maintainers and future AI agents. The scanner output is **not financial advice** and should not be treated as a recommendation to buy, sell, short, hedge, or trade any security.

## Executive summary

`score_snapshot()` receives a normalized `TickerSnapshot`, derives true gamma metrics when provider-backed option-chain greeks are available, scores all four models, chooses the highest model as the primary score, and returns a `ScanResult` with components, rationales, confidence, metrics, provenance, warnings, and risk flags.

The most important implementation boundary is this:

| Data category | Meaning in this codebase | Must not be confused with |
| --- | --- | --- |
| Provider-backed true gamma / GEX | Contract-level option records with greek `gamma` and `open_interest`, plus source/capability metadata that does **not** mark the chain as Yahoo proxy-only. These can populate `call_gamma_exposure`, `put_gamma_exposure`, `net_gamma_exposure`, `absolute_gamma_exposure`, `gamma_exposure_pct_market_cap`, gamma flip, walls, and concentration. | Yahoo's estimated `dealer_gamma_exposure_proxy`. |
| Yahoo/public options proxy | Public Yahoo option-chain data used for call/put volume, open interest, IV, and a near-money exposure proxy. It is labeled `yahoo_finance_options_proxy` and `dealer_gamma_exposure_proxy` is marked `estimated`. | True dealer gamma exposure, true GEX, or provider-backed greeks. |
| Score | A 0-100 heuristic setup score for one of four informational models. | Trade quality, expected return, or financial advice. |
| Confidence | A separate 0-100 completeness/quality estimate for each model. A low score can still have high confidence if data is complete. | Bullishness or risk level. |
| Risk flags | Guardrail/warning metadata for liquidity, missing data, stale/source errors, corporate actions, dilution, halts, and events. | Automatic removal; filtering is an API/UI choice. |

## Source map

| Area | Primary files | Relevant implementation |
| --- | --- | --- |
| Scoring model metadata and scoring orchestration | `src/squeeze_scanner/scoring.py` | `SCORING_MODELS` and weights (`:19-259`), `score_snapshot()` (`:262-387`), model component wiring (`:390-442`), scoring curves (`:445-764`). |
| Risk guardrails, confidence, rationales, metrics | `src/squeeze_scanner/scoring.py` | Guardrail flags (`:777-1041`), model confidence (`:1067-1449`), data quality/missing core fields (`:1478-1503`), rationales (`:1506-1670`), gamma detail metrics (`:1728-2184`). |
| Domain contracts | `src/squeeze_scanner/domain.py` | `OptionProviderCapabilities`, `OptionChainRecord`, `OptionChainSnapshot`, provider protocols (`:33-131`), `TickerSnapshot` (`:174-241`), `GuardrailConfig` and `ScanResult` (`:244-280`). |
| Option-chain normalization and true GEX aggregation | `src/squeeze_scanner/options.py` | `GammaExposureAggregation` (`:80-108`), record/snapshot normalization (`:111-287`), true GEX aggregation and snapshot enrichment (`:290-450`), Yahoo-proxy guard (`:486-494`). |
| Built-in Yahoo provider | `src/squeeze_scanner/providers/yahoo.py` | Yahoo option capabilities (`:12-23`), quote/options snapshot fetch (`:25-138`), public option aggregation (`:279-347`), near-money proxy formula (`:384-399`). |
| Optional premium seams | `src/squeeze_scanner/providers/premium.py` | Provider capabilities (`:22-108`), provider set/status (`:113-204`), composite merge (`:205-236`), factories/status payload (`:239-298`), merge/error provenance (`:301-358`). |
| Raw cache/history compatibility | `src/squeeze_scanner/cache.py` | Raw snapshot cache (`:31-85`), recent/historical reads (`:87-222`), writes/schema (`:323-458`), legacy JSON and option-record rehydration (`:487-531`). |
| Regression coverage | `tests/test_options.py`, `tests/test_confidence_risk.py`, `tests/test_premium_providers.py` | Tests cover option normalization, true/proxy GEX separation, confidence/risk behavior, cache compatibility, gamma scoring inputs, and premium-provider status/error seams. |
| Product status | `ROADMAP.md` | Shipped status for options data contract, true GEX aggregation, gamma scoring, source provenance, model confidence, guardrails, and premium provider foundations (`:17-23`, `:83-110`, `:159-170`). |

## End-to-end data flow

1. **Provider fetch** creates a normalized `TickerSnapshot`.
   - Built-in mode uses `YahooFinanceProvider.fetch()`.
   - Optional premium feeds can patch specific fields onto the Yahoo snapshot through `CompositeMarketDataProvider`.
2. **Raw caching** stores the snapshot JSON in `market_data_cache` and `market_data_history` without derived scores.
3. **True gamma enrichment** runs at scoring time through `snapshot_with_true_gamma_metrics()`.
   - It normalizes `option_chain_records`.
   - It refuses to promote Yahoo proxy data to true GEX.
   - It fills true gamma fields only when records contain usable greeks/open interest.
4. **Model scoring** computes all four model component dictionaries and sums them to `model_scores`.
5. **Primary result selection** chooses the model with the highest score as `primary_model`; `score` is that model's score.
6. **Explainability** adds metrics, per-model rationales, summary rationale, warnings, source provenance, model confidence, and risk flags.

## Domain objects and snapshot fields

### `TickerSnapshot`

`TickerSnapshot` is the repository's normalized raw data envelope. It contains quote/liquidity fields, short-interest fields, borrow fields, corporate-action and event-risk fields, options activity, true/proxy gamma metrics, momentum, and provenance.

| Field group | Examples | Notes |
| --- | --- | --- |
| Quote/liquidity | `price`, `previous_close`, `volume`, `avg_volume_20d`, `avg_volume_90d`, `market_cap`, `float_shares` | Yahoo-backed by default. Used by scores, guardrails, confidence, and data quality. |
| Short interest | `short_percent_float`, `short_ratio`, `shares_short`, settlement/report/revision metadata | Yahoo supplies public values; premium short-interest providers can improve freshness and metadata. |
| Borrow | `borrow_fee_pct`, `borrow_available_shares`, `borrow_utilization_pct`, `borrow_rebate_rate_pct`, `borrow_fetched_at` | Yahoo does not provide borrow fee; missing borrow scores zero and lowers confidence. |
| Corporate/action risk | `recent_reverse_split`, `days_since_reverse_split`, `reverse_split_ratio`, dilution/offering/warrant/ATM flags | Reverse split contributes to Float Compression scoring and can also create risk flags. |
| Events | `active_trading_halt`, `halt_risk`, `material_news_event`, `event_risk` | Optional event providers create guardrail flags; tests assert these do not change model scores. |
| Options activity | `call_volume`, `put_volume`, `call_open_interest`, `put_open_interest` | Yahoo can populate these from public chains. Premium/true options providers can also supply them. |
| Yahoo proxy gamma | `dealer_gamma_exposure_proxy` | Estimated public proxy only. Never true GEX. |
| True gamma metrics | `call_gamma_exposure`, `put_gamma_exposure`, `net_gamma_exposure`, `absolute_gamma_exposure`, `gamma_exposure_pct_market_cap`, `gamma_flip_price`, walls, concentration | Only provider-backed greeks/open interest should populate these. |
| Option chain metadata | `option_chain_source`, `option_chain_provider`, `option_chain_fetched_at`, freshness/staleness, `option_chain_capabilities`, `option_chain_records` | Drives source-aware scoring and confidence. |
| Provenance | `source_fetched_at`, `field_sources`, `field_quality`, `source_quality`, `source_warnings` | Used directly by confidence and guardrail logic. |

### `ScanResult`

`score_snapshot()` returns `ScanResult` with:

| Field | Meaning |
| --- | --- |
| `score` | Highest score among the four models. |
| `primary_model` | Key of the highest-scoring model. |
| `model_scores` | Sum of components for every model. |
| `model_components` | Per-model component contributions, rounded to one decimal. |
| `model_rationales` | Per-model natural-language explanations for all signals. |
| `components` | Components for the primary model only. |
| `rationale` | Summary rationale for the primary model. |
| `metrics` | Normalized raw and derived metrics consumed by UI/API/reporting. |
| `model_confidence` | Per-model confidence score, separate from model score. |
| `confidence_rationales` | Why each confidence score was increased or reduced. |
| `risk_flags` | Structured guardrail flags with key, severity, message, and optional field/value/limit. |
| `field_sources`, `field_quality`, `source_quality`, `warnings` | Provenance and source warnings carried from the snapshot. |

## The four scoring models

All scores are 0-100. Each model has fixed component weights that sum to 100. Missing numeric inputs score zero for their component; they do not create bullish signals.

| Model key | Human label | Purpose | Components and weights | Key data dependencies |
| --- | --- | --- | --- | --- |
| `classical_short_squeeze` | Classical Short Squeeze | Finds names with a large short base, expensive borrow if available, and crowded short exits. | Short interest 40, borrow fee 25, days to cover 35. | `short_percent_float`, `borrow_fee_pct`, `short_ratio`. |
| `float_compression` | Float Compression | Finds small-float names where recent reverse splits/supply compression combine with rising volume. | Tiny float 45, recent reverse split 25, relative volume 30. | `float_shares` or `market_cap` fallback, `recent_reverse_split`, `days_since_reverse_split`, `volume`, `avg_volume_20d`. |
| `gamma_candidate` | Gamma Candidate | Finds options-driven names with heavy call demand, true/proxy exposure, near gamma flips, walls/concentration, and OI growth. | Call buying 30, true/proxy gamma exposure 35, gamma flip proximity 10, call/put gamma skew 10, gamma walls/concentration 10, open-interest change 5. | Options volume/OI, true gamma fields if available, Yahoo proxy if no true greeks, option-chain records, freshness and capabilities. |
| `hybrid` | Hybrid | Finds rare names combining small float, high short interest, borrow pressure, and options activity. | Tiny float 25, short interest 25, borrow fee 25, options activity 25. | Float/market cap, short interest, borrow, call volume/open interest. |

### Model score mechanics

| Component | Implementation behavior |
| --- | --- |
| Piecewise curves | Most numeric components use `_piecewise_score()` so points ramp between documented thresholds instead of jumping abruptly. |
| Missing inputs | Missing values return zero for the affected component. They also appear in confidence rationales and may create missing-data guardrails. |
| Primary score | The highest value in `model_scores` becomes `score`; the other model scores remain available for ranking/filtering. |
| Rationales | `_signal_rationale()` emits explanations per signal, including whether gamma is true-greeks backed or proxy-only. |
| Components | `model_components` exposes each signal's score contribution, making score changes auditable. |

## Model confidence

Confidence answers: **how complete and trustworthy was the data for this model?** It is not a bullish/bearish score.

| Confidence input | Effect |
| --- | --- |
| Field presence | Required model fields add confidence; missing required fields add rationales such as `short interest missing`. |
| Field quality | `present` contributes full weight; `estimated` contributes less; `stale` contributes less; `missing` and `provider-error` contribute zero. |
| Source quality | Source quality below 75/100 applies a medium multiplier; below 50/100 applies a lower multiplier. |
| Snapshot freshness | Missing, unparsable, older than 6h, older than 24h, or older than 48h source timestamps reduce confidence. |
| Source warnings | Provider warnings reduce confidence and create a `source_warnings` risk flag. |
| Liquidity/missing-data guardrails | Low liquidity and excessive missing data reduce confidence. |
| Gamma data type | Provider-backed true greeks can earn additional confidence for true GEX, gamma flip, walls/concentration, near-expiry GEX, and OI-change details. Yahoo proxy gamma receives lower support and an explicit rationale that it is not true GEX. |
| Hybrid confidence | Blends classical 35%, float 35%, gamma 30%, then caps confidence if one required domain is weak. |

## Risk guardrails

`GuardrailConfig` defines default thresholds. Guardrails create structured flags; they do not automatically delete a result unless a caller asks to filter, such as `exclude_high_risk` behavior covered by tests.

| Guardrail setting | Default | Primary flags / meaning |
| --- | ---: | --- |
| `min_price` | 1.00 | `price_below_min` high severity. |
| `max_price` | 250.00 | `price_above_max` warning. |
| `min_dollar_volume` | 1,000,000 | `low_dollar_volume` and aggregated `low_liquidity`. |
| `min_avg_volume_20d` | 250,000 shares | `low_average_volume`. |
| `min_avg_dollar_volume_20d` | 1,000,000 | `low_average_dollar_volume` and `low_liquidity`. |
| `min_market_cap` | 25,000,000 | `low_market_cap` high severity. |
| `max_squeeze_market_cap` | 10,000,000,000 | `large_market_cap` informational flag. |
| `max_missing_core_fields` | 4 | `missing_data` or `excessive_missing_data`. |
| `recent_reverse_split_days` | 180 | `recent_reverse_split` high severity when a reverse split is recent or undated. |

Additional risk flags come from missing price/volume/market cap, unknown corporate-action history, dilution/offering/warrant/ATM signals, active halts, halt risk, material news/events, source warnings, and low data quality.

## Source provenance and quality

Provenance travels with every snapshot and scan result.

| Provenance field | Expected content | Used by |
| --- | --- | --- |
| `field_sources` | Mapping of field name to source name, for example `yahoo_finance`, `yahoo_finance_options_proxy`, or `premiumborrowco_borrow`. | Confidence source-quality multipliers, UI/API explanations. |
| `field_quality` | Mapping of field name to quality label: `present`, `estimated`, `stale`, `missing`, or `provider-error`. | Confidence field multipliers and missing/provider-error handling. |
| `source_quality` | Mapping of source name to numeric quality score. Yahoo defaults are 70 for quotes and 55 for Yahoo options proxy. | Confidence multipliers. |
| `source_fetched_at` | Snapshot-level source timestamp. | Staleness confidence penalties. |
| `source_warnings` | Provider warnings and fallback/errors. | Warnings list, `source_warnings` risk flag, confidence penalties. |

Important AI-agent invariant: do **not** erase provenance when merging provider patches. `merge_ticker_snapshots()` intentionally merges source maps and warnings, and `snapshot_with_provider_error()` marks missing fields as `provider-error` rather than inventing values.

## Yahoo Finance provider behavior and limitations

Yahoo is the built-in provider. It supplies useful public data but it is not a premium borrow, true-greeks, corporate-action, filing, or event-risk provider.

| Yahoo-backed field type | Current behavior | Limitation |
| --- | --- | --- |
| Quote/volume/history | Pulls `price`, previous close, current volume, average volumes, market cap, float, short interest fields, momentum, and reverse split history where available. | Availability varies by symbol; public data may be delayed/incomplete. |
| Borrow fee | Always `None` in the Yahoo provider. | Borrow scoring requires an optional securities-lending feed. Missing borrow scores zero. |
| Options volume/OI/IV | Reads up to the first two expirations and sums call/put volume and open interest. | Yahoo capabilities do not include greeks, OI change, or true gamma exposure. |
| `dealer_gamma_exposure_proxy` | Sums near-money open interest exposure proxy for contracts within ±15% of spot: `open_interest * 100 * price`. | This is estimated public-data proxy exposure, not true GEX. It is marked `estimated` and source `yahoo_finance_options_proxy`. |
| Option-chain freshness | Yahoo options proxy includes fetched timestamp, freshness `0`, stale-after `3600` seconds, and capability metadata. | Freshness metadata helps confidence but does not turn proxy data into true greeks. |

Yahoo option capabilities currently include expiration listing, strike listing, bid/ask, last price, volume, open interest, and implied volatility. They do **not** include `delta`, `gamma`, `open_interest_change`, or `true_gamma_exposure`.

## Option-chain records and snapshots

### `OptionChainRecord`

Each record is one option contract row.

| Field | Meaning |
| --- | --- |
| `symbol` | Underlying ticker. Required. |
| `expiration` | Expiration date. Required. |
| `strike` | Strike price. Required. |
| `side` | `call` or `put`. Required. |
| `contract_symbol` | Vendor/OCC contract identifier when available. |
| `days_to_expiration`, `days_to_expiry` | DTE aliases kept for compatibility. |
| `bid`, `ask`, `last_price`, `volume`, `open_interest`, `open_interest_change`, `implied_volatility`, `delta`, `gamma` | Normalized contract metrics. |
| `timestamp`, `provider`, `source` | Record-level provenance. |

`normalize_option_chain_record()` accepts common provider aliases such as `underlyingSymbol`, `expirationDate`, `strikePrice`, `openInterest`, `impliedVolatility`, `dte`, and OCC-style contract symbols. Records without symbol/expiration/side/strike are skipped.

### `OptionChainSnapshot`

`OptionChainSnapshot` wraps records with chain-level metadata: `symbol`, `provider`, `source`, `fetched_at`, `freshness_seconds`, `stale_after_seconds`, boolean `capabilities`, and `warnings`. The cache rehydrates embedded JSON records through `normalize_option_chain_records()` so legacy raw snapshots stay readable.

## True/proxy gamma separation

The repository intentionally preserves true and proxy gamma as different concepts.

| Code path | True provider-backed behavior | Yahoo proxy behavior |
| --- | --- | --- |
| Snapshot fields | True GEX fields are `call_gamma_exposure`, `put_gamma_exposure`, `net_gamma_exposure`, `absolute_gamma_exposure`, `gamma_exposure_pct_market_cap`, `gamma_flip_price`, walls, and concentration. | Proxy field is only `dealer_gamma_exposure_proxy`. |
| `snapshot_with_true_gamma_metrics()` | Aggregates true GEX when option records contain greeks/open interest and snapshot is not Yahoo proxy-only. Sets `open_interest`, `gamma`, and `true_gamma_exposure` capabilities. | If `option_chain_source == yahoo_finance_options_proxy` and true capability is not present, records are normalized but true GEX fields are not populated. |
| `score_snapshot().metrics` | Reports `gamma_exposure_source_type = true_greeks`, `gamma_exposure_is_true = true`, and true GEX percent of market cap when available. | Reports `gamma_exposure_source_type = proxy`, `gamma_exposure_is_proxy = true`, `dealer_gamma_exposure_pct_market_cap`, and leaves true `gamma_exposure_pct_market_cap` as `None`. |
| Gamma Candidate score | Can use true exposure, gamma flip, call/put gamma skew, walls, concentration, near-expiry GEX, and OI-change data. | Can still score call buying, proxy exposure, call-open-interest skew, and weaker concentration fallbacks, but confidence is lower and rationales state it is not true GEX. |

Regression tests assert this separation: Yahoo proxy exposure is not promoted into true GEX, and proxy gamma has lower confidence than provider-backed true greeks.

## True GEX formula and aggregation

For provider-backed records with usable `gamma`, `open_interest`, `side`, `strike`, and `expiration`, true gamma exposure uses:

```text
exposure_magnitude = abs(gamma) * open_interest * 100 * spot^2 * 0.01
call exposure      = +exposure_magnitude
put exposure       = -exposure_magnitude
```

Where:

| Term | Source / meaning |
| --- | --- |
| `gamma` | Contract greek supplied by a true options provider. |
| `open_interest` | Contract open interest. Negative OI is treated as invalid/skipped. |
| `100` | Standard equity option contract multiplier. |
| `spot^2` | Underlying spot-price scaling. |
| `0.01` | One-percent move sensitivity (`GAMMA_EXPOSURE_PERCENT_MOVE`). |

Aggregated outputs:

| Metric | Calculation / meaning |
| --- | --- |
| `call_gamma_exposure` | Sum of signed positive call exposures. |
| `put_gamma_exposure` | Sum of signed negative put exposures. |
| `net_gamma_exposure` | Calls plus puts. |
| `absolute_gamma_exposure` | Calls plus absolute puts. |
| `gamma_exposure_pct_market_cap` | Absolute GEX divided by market cap, percent. |
| `gamma_flip_price` | Interpolated strike where cumulative net-by-strike exposure changes sign. |
| `gamma_flip_distance_pct` | Gamma flip distance from spot. |
| `max_gamma_strike` | Strike with largest absolute exposure. |
| `call_wall_strike`, `put_wall_strike` | Largest call-side and put-side exposure strikes. |
| `largest_gamma_expiration` | Expiration with largest absolute exposure. |
| `gamma_strike_concentration_pct`, `gamma_expiration_concentration_pct` | Largest strike/expiration exposure share of absolute exposure. |
| `valid_contract_count`, `skipped_contract_count`, `missing_fields`, `warnings` | Aggregation diagnostics. |

Near-expiration GEX detail uses the same true-gamma formula for contracts with DTE from 0 through 14 days and produces `near_gamma_exposure`, `near_gamma_exposure_pct_market_cap`, and contract-count metadata.

## Gamma Candidate scoring inputs

Gamma Candidate scoring is the most source-aware model. It blends true greeks when available with weaker public-data fallbacks when they are not.

| Component | Weight | True-greeks behavior | Proxy/fallback behavior |
| --- | ---: | --- | --- |
| `call_buying` | 30 | Scores call volume and call/put volume ratio. | Same Yahoo-compatible behavior. |
| `dealer_gamma_exposure` | 35 | Scores true GEX percent of market cap with thresholds around 1%, 3%, 7%, and 12%; can add near-expiration true GEX credit. | Scores `dealer_gamma_exposure_proxy` or proxy percent of market cap, but does not set true GEX metrics. |
| `gamma_flip_proximity` | 10 | Scores true gamma flip distance, strongest within roughly 1% of spot and fading through wider distances. | No credit. Gamma flip requires true greeks. |
| `call_put_gamma_skew` | 10 | Scores call-side share of true gamma exposure. | Uses call open-interest share as a weaker fallback. |
| `gamma_concentration_walls` | 10 | Scores strike/expiration concentration, call wall distance, put wall distance, max gamma strike distance, and near-dated largest gamma expiration. | Uses call open interest and call-OI share as weaker concentration fallback. |
| `open_interest_change` | 5 | Aggregates provider `open_interest_change`; scores positive call OI growth by absolute and percent change, with a small net-positive bonus. | No credit when OI-change data is missing. |

AI-agent invariant: never combine `dealer_gamma_exposure_proxy` and `absolute_gamma_exposure` into a single unnamed GEX metric. Keep source type, source name, and true/proxy flags attached to any downstream explanation.

## Optional premium provider seams

The premium provider foundation is intentionally adapter-ready but mostly unimplemented for real vendors. It preserves Yahoo-only behavior unless a provider is explicitly selected.

| Feed | Fields | Score impact | Risk impact | Current state |
| --- | --- | --- | --- | --- |
| `borrow` | Borrow fee, availability, utilization, rebate rate, fetched timestamp | Classical and Hybrid scores/confidence | No direct risk flags by default | Disabled by default; named provider is unconfigured until adapter exists. |
| `short_interest` | Short % float, short ratio, shares short, settlement/report/revision metadata | Classical and Hybrid scores/confidence | No direct risk flags by default | Disabled by default; adapter seam exists. |
| `corporate_actions` | Reverse split metrics, dilution/offering/warrant/ATM flags | Float score/confidence | Creates risk flags | Disabled by default; adapter seam exists. |
| `filings` | Float shares and dilution/overhang/ATM flags | Float/Hybrid data and confidence | Creates risk flags | Disabled by default; adapter seam exists. |
| `events` | Halt/news/event-risk fields | Does not change scores in current tests | Creates halt/news/event risk flags | Disabled by default; adapter seam exists. |
| True options greeks | Option-chain records with gamma, OI, OI change, IV, delta, timestamps | Gamma Candidate score/confidence | Source warnings/freshness; no dedicated premium factory yet | Domain contracts and normalizers exist; external vendor adapter still needs wiring. |

### Error and merge behavior

- Disabled providers report `status="disabled"` and do not fetch.
- A named but unimplemented provider reports `status="unconfigured"` and raises `PremiumProviderNotConfigured` if fetched directly.
- `CompositeMarketDataProvider` catches provider errors, preserves the Yahoo snapshot, marks relevant missing fields as `provider-error`, records source quality, and appends a source warning.
- Event fields can add risk flags without changing scores.

## Extension workflow for new providers

Use this workflow when adding a real vendor adapter:

1. **Choose the feed and protocol.** Use the protocols in `domain.py` (`BorrowProvider`, `ShortInterestProvider`, `OptionChainProvider`, `CorporateActionsProvider`, `FilingsProvider`, `EventProvider`) and the provider capability tables in `premium.py`.
2. **Keep credentials out of source control.** Add settings/environment variables or a secrets-manager integration; do not commit keys, tokens, or sample secrets.
3. **Return normalized `TickerSnapshot` patches.** Populate only fields the vendor actually supplies. Include `field_sources`, `field_quality`, `source_quality`, `source_fetched_at`, and `source_warnings`.
4. **For options vendors, normalize contract rows.** Use `normalize_option_chain_snapshot()` / `normalize_option_chain_records()` and populate `option_chain_records`, `option_chain_provider`, `option_chain_source`, `option_chain_fetched_at`, freshness/stale metadata, and `option_chain_capabilities`.
5. **Declare capabilities honestly.** Set `gamma=True`, `open_interest=True`, `open_interest_change=True`, `delta=True`, and `true_gamma_exposure=True` only when the vendor provides those inputs with usable quality. Do not use `yahoo_finance_options_proxy` for a true-greeks provider.
6. **Wire the adapter.** Extend the relevant factory/composition path so named providers instantiate the real adapter instead of `PremiumDataFeedProvider`. For true options greeks, wire the option-chain provider into the snapshot assembly before scoring so `snapshot_with_true_gamma_metrics()` can aggregate true GEX.
7. **Preserve fallback behavior.** Provider outages should degrade provenance/confidence and add warnings, not silently invent data or remove Yahoo-backed fields.
8. **Add regression tests.** Cover provider status, merge/error provenance, cache round-trip, true/proxy gamma separation, and score/confidence effects.
9. **Calibrate with history.** After provider-backed scans accumulate, use reports/calibration described in `ROADMAP.md` to tune thresholds using observed outcomes rather than assumptions.

## Testing anchors for future changes

| Test file | What it protects |
| --- | --- |
| `tests/test_options.py` | Provider alias normalization, true GEX math/signs/concentration, true GEX derivation from records, and Yahoo proxy non-promotion. |
| `tests/test_confidence_risk.py` | Missing data reducing confidence, low-liquidity/risk guardrails, low score vs low confidence distinction, legacy cache compatibility, option-chain cache round-trip, true/proxy metric separation, Gamma Candidate component blend, and proxy-vs-true confidence gap. |
| `tests/test_premium_providers.py` | Provider settings defaults, unconfigured provider failure/status, Yahoo preservation with provider-error provenance, event risk flags not changing scores, and `/api/providers` status shape. |
| `ROADMAP.md` validation coverage | Confirms the implemented areas and remaining external integration work. |

## Practical guidance for humans and AI agents

- Treat all scanner output as informational analysis metadata, not investment advice.
- Keep `model_scores` and `model_confidence` separate in explanations.
- Mention whether Gamma Candidate evidence came from `true_greeks`, `proxy`, or `missing` source type.
- Prefer field-level provenance over assumptions about a provider.
- Missing premium data should lower confidence or score zero; it should not create positive score credit.
- Risk flags are important context even when a score is high.
- When documenting or changing gamma behavior, cite both `options.py` and `scoring.py` because true GEX is aggregated in one file and consumed/explained in the other.
