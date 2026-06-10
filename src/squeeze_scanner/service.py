from __future__ import annotations

import concurrent.futures
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .domain import DataProviderError, InvalidRankingModeError, InvalidSymbolError, MarketDataProvider, ScanResult
from .history import ScoreHistoryStore, ScoreHistoryWrite
from .scoring import score_snapshot, scoring_model_metadata

logger = logging.getLogger(__name__)

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
RANKING_MODE_TOP_SCORE = "top_score"
RANKING_MODE_SELECTED_MODEL_SCORE = "selected_model_score"
RANKING_MODE_HIGHEST_MODEL_CONFIDENCE = "highest_model_confidence"
RANKING_MODE_SCORE_INCREASE_1H = "score_increase_1h"
RANKING_MODE_SCORE_INCREASE_24H = "score_increase_24h"
RANKING_MODE_RELATIVE_VOLUME = "relative_volume"
RANKING_MODE_SHORT_INTEREST = "short_interest"
RANKING_MODE_SMALLEST_FLOAT = "smallest_float"
RANKING_MODE_HYBRID_ONLY = "hybrid_only"
RANKING_MODE_GAMMA_CANDIDATE_ONLY = "gamma_candidate_only"
RANKING_MODES = {
    RANKING_MODE_TOP_SCORE,
    RANKING_MODE_SELECTED_MODEL_SCORE,
    RANKING_MODE_HIGHEST_MODEL_CONFIDENCE,
    RANKING_MODE_SCORE_INCREASE_1H,
    RANKING_MODE_SCORE_INCREASE_24H,
    RANKING_MODE_RELATIVE_VOLUME,
    RANKING_MODE_SHORT_INTEREST,
    RANKING_MODE_SMALLEST_FLOAT,
    RANKING_MODE_HYBRID_ONLY,
    RANKING_MODE_GAMMA_CANDIDATE_ONLY,
}
_RANKING_MODE_ALIASES = {
    "score": RANKING_MODE_TOP_SCORE,
    "top": RANKING_MODE_TOP_SCORE,
    "top_score": RANKING_MODE_TOP_SCORE,
    "selected_model": RANKING_MODE_SELECTED_MODEL_SCORE,
    "selected_model_score": RANKING_MODE_SELECTED_MODEL_SCORE,
    "model_score": RANKING_MODE_SELECTED_MODEL_SCORE,
    "highest_confidence": RANKING_MODE_HIGHEST_MODEL_CONFIDENCE,
    "highest_model_confidence": RANKING_MODE_HIGHEST_MODEL_CONFIDENCE,
    "model_confidence": RANKING_MODE_HIGHEST_MODEL_CONFIDENCE,
    "score_increase_1h": RANKING_MODE_SCORE_INCREASE_1H,
    "score_change_1h": RANKING_MODE_SCORE_INCREASE_1H,
    "score_delta_1h": RANKING_MODE_SCORE_INCREASE_1H,
    "delta_1h": RANKING_MODE_SCORE_INCREASE_1H,
    "biggest_1h_score_increase": RANKING_MODE_SCORE_INCREASE_1H,
    "one_hour_score_increase": RANKING_MODE_SCORE_INCREASE_1H,
    "score_increase_24h": RANKING_MODE_SCORE_INCREASE_24H,
    "score_change_24h": RANKING_MODE_SCORE_INCREASE_24H,
    "score_delta_24h": RANKING_MODE_SCORE_INCREASE_24H,
    "delta_24h": RANKING_MODE_SCORE_INCREASE_24H,
    "biggest_24h_score_increase": RANKING_MODE_SCORE_INCREASE_24H,
    "twenty_four_hour_score_increase": RANKING_MODE_SCORE_INCREASE_24H,
    "relative_volume": RANKING_MODE_RELATIVE_VOLUME,
    "highest_relative_volume": RANKING_MODE_RELATIVE_VOLUME,
    "rvol": RANKING_MODE_RELATIVE_VOLUME,
    "short_interest": RANKING_MODE_SHORT_INTEREST,
    "highest_short_interest": RANKING_MODE_SHORT_INTEREST,
    "short_percent_float": RANKING_MODE_SHORT_INTEREST,
    "smallest_float": RANKING_MODE_SMALLEST_FLOAT,
    "small_float": RANKING_MODE_SMALLEST_FLOAT,
    "float": RANKING_MODE_SMALLEST_FLOAT,
    "float_shares": RANKING_MODE_SMALLEST_FLOAT,
    "hybrid": RANKING_MODE_HYBRID_ONLY,
    "hybrid_only": RANKING_MODE_HYBRID_ONLY,
    "hybrid_score": RANKING_MODE_HYBRID_ONLY,
    "gamma": RANKING_MODE_GAMMA_CANDIDATE_ONLY,
    "gamma_candidate": RANKING_MODE_GAMMA_CANDIDATE_ONLY,
    "gamma_candidate_only": RANKING_MODE_GAMMA_CANDIDATE_ONLY,
    "gamma_score": RANKING_MODE_GAMMA_CANDIDATE_ONLY,
}
_MODEL_KEY_ALIASES = {
    "classical": "classical_short_squeeze",
    "classical_short_squeeze": "classical_short_squeeze",
    "short_squeeze": "classical_short_squeeze",
    "float": "float_compression",
    "float_compression": "float_compression",
    "gamma": "gamma_candidate",
    "gamma_candidate": "gamma_candidate",
    "hybrid": "hybrid",
}
_ASCENDING_DIRECTIONS = {"asc", "ascending", "low_to_high", "smallest_first"}
_DESCENDING_DIRECTIONS = {"desc", "descending", "high_to_low", "largest_first"}


@dataclass(frozen=True)
class RankingOptions:
    mode: str = RANKING_MODE_TOP_SCORE
    selected_model: str | None = None
    sort_direction: str = "desc"


class ScannerService:
    def __init__(
        self,
        provider: MarketDataProvider,
        max_workers: int = 5,
        history_store: ScoreHistoryStore | None = None,
    ) -> None:
        self.provider = provider
        self.max_workers = max_workers
        self.history_store = history_store

    def scan(
        self,
        raw_symbols: str | Sequence[str],
        max_symbols: int = 25,
        ranking_mode: str | None = None,
        selected_model: str | None = None,
        sort_direction: str | None = None,
        guardrail_filter: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbols = normalize_symbols(raw_symbols, max_symbols=max_symbols)
        results: list[ScanResult] = []
        errors: list[dict[str, str]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.max_workers, len(symbols))) as executor:
            futures = {executor.submit(self.provider.fetch, symbol): symbol for symbol in symbols}
            for future in concurrent.futures.as_completed(futures):
                symbol = futures[future]
                try:
                    snapshot = future.result()
                except DataProviderError as exc:
                    errors.append({"symbol": symbol, "message": str(exc)})
                    continue
                except Exception as exc:
                    errors.append({"symbol": symbol, "message": f"{symbol}: unexpected scanner error ({exc})"})
                    continue

                results.append(score_snapshot(snapshot))

        scan_times = _scan_times(self.provider, results)
        history_writes = record_score_history(self.provider, self.history_store, results)
        score_deltas = score_deltas_for_history_writes(self.history_store, history_writes, results)

        return build_scan_response(
            results,
            errors,
            scan_times=scan_times,
            score_deltas=score_deltas,
            ranking_mode=ranking_mode,
            selected_model=selected_model,
            sort_direction=sort_direction,
            guardrail_filter=guardrail_filter,
        )

    def deltas_for_results(self, results: Sequence[ScanResult]) -> dict[str, dict[str, Any]]:
        return score_deltas_from_history(self.provider, self.history_store, results)


def normalize_symbols(raw_symbols: str | Sequence[str], max_symbols: int = 25) -> list[str]:
    if isinstance(raw_symbols, str):
        candidates = _split_symbols([raw_symbols])
    elif isinstance(raw_symbols, Iterable):
        candidates = _split_symbols(str(item) for item in raw_symbols)
    else:
        raise InvalidSymbolError("Enter one or more ticker symbols.")

    symbols: list[str] = []
    seen: set[str] = set()
    invalid: list[str] = []

    for candidate in candidates:
        symbol = candidate.upper()
        if not SYMBOL_PATTERN.match(symbol):
            invalid.append(candidate)
            continue
        if symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)

    if invalid:
        raise InvalidSymbolError(f"Invalid ticker symbol(s): {', '.join(invalid)}")
    if not symbols:
        raise InvalidSymbolError("Enter one or more ticker symbols.")
    if len(symbols) > max_symbols:
        raise InvalidSymbolError(f"Scan up to {max_symbols} symbols at a time.")
    return symbols


def build_scan_response(
    results: Sequence[ScanResult],
    errors: Sequence[dict[str, str]] | None = None,
    scan_times: Mapping[str, float] | None = None,
    score_deltas: Mapping[str, Mapping[str, Any]] | None = None,
    ranking_mode: str | None = None,
    selected_model: str | None = None,
    sort_direction: str | None = None,
    guardrail_filter: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ranking = _ranking_options(ranking_mode, selected_model, sort_direction)
    filtered_results = filter_results_by_guardrails(results, guardrail_filter)
    delta_lookup = score_deltas or {}
    ranked_results = sorted(
        filtered_results,
        key=lambda result: _ranking_sort_key(result, ranking, delta_lookup),
    )
    generated_at = datetime.now(timezone.utc)
    return {
        "model": scoring_model_metadata(),
        "ranking": {
            "mode": ranking.mode,
            "selected_model": ranking.selected_model,
            "sort_direction": ranking.sort_direction,
        },
        "generated_at": generated_at.isoformat(),
        "count": len(ranked_results),
        "results": [
            _result_with_scan_time(result, scan_times or {}, generated_at, delta_lookup)
            for result in ranked_results
        ],
        "errors": sorted(errors or [], key=lambda error: error["symbol"]),
    }


def filter_results_by_guardrails(
    results: Sequence[ScanResult],
    guardrail_filter: Mapping[str, Any] | None = None,
) -> list[ScanResult]:
    if not guardrail_filter:
        return list(results)

    excluded_flags = _as_string_set(
        guardrail_filter.get("exclude_risk_flags")
        or guardrail_filter.get("exclude_flags")
        or guardrail_filter.get("risk_flags")
    )
    excluded_severities = _as_string_set(guardrail_filter.get("exclude_severities"))
    if guardrail_filter.get("exclude_high_risk") or guardrail_filter.get("hide_high_risk"):
        excluded_severities.add("high")

    if not excluded_flags and not excluded_severities:
        return list(results)

    filtered: list[ScanResult] = []
    for result in results:
        flags = _field(result, "risk_flags") or []
        flag_keys = {str(flag.get("key")) for flag in flags if isinstance(flag, Mapping)}
        severities = {str(flag.get("severity")) for flag in flags if isinstance(flag, Mapping)}
        if excluded_flags & flag_keys:
            continue
        if excluded_severities & severities:
            continue
        filtered.append(result)
    return filtered


def _as_string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, Iterable):
        return {str(item).strip() for item in value if str(item).strip()}
    text = str(value).strip()
    return {text} if text else set()


def normalize_ranking_mode(ranking_mode: str | None) -> str:
    if ranking_mode is None or not ranking_mode.strip():
        return RANKING_MODE_TOP_SCORE

    key = _normalize_option(ranking_mode)
    mode = _RANKING_MODE_ALIASES.get(key)
    if mode is None:
        raise InvalidRankingModeError(
            f"Invalid ranking mode '{ranking_mode}'. Supported modes: {', '.join(sorted(RANKING_MODES))}."
        )
    return mode


def normalize_sort_direction(sort_direction: str | None, ranking_mode: str = RANKING_MODE_TOP_SCORE) -> str:
    if sort_direction is None or not sort_direction.strip():
        return "asc" if ranking_mode == RANKING_MODE_SMALLEST_FLOAT else "desc"

    key = _normalize_option(sort_direction)
    if key in _ASCENDING_DIRECTIONS:
        return "asc"
    if key in _DESCENDING_DIRECTIONS:
        return "desc"
    raise InvalidRankingModeError("Invalid sort direction. Use 'asc' or 'desc'.")


def _scan_times(provider: MarketDataProvider, results: Sequence[ScanResult]) -> dict[str, float]:
    get_scan_times = getattr(provider, "scan_times", None)
    if not callable(get_scan_times):
        return {}
    return get_scan_times([result.symbol for result in results])


def record_score_history(
    provider: MarketDataProvider,
    history_store: ScoreHistoryStore | None,
    results: Sequence[ScanResult],
    *,
    scan_run_id: str = "live",
) -> dict[str, ScoreHistoryWrite]:
    if history_store is None or not results:
        return {}

    references = _raw_history_references(provider, results)
    writes: dict[str, ScoreHistoryWrite] = {}
    for result in results:
        reference = references.get(result.symbol)
        if not reference:
            continue
        try:
            writes[result.symbol] = history_store.record_scan_result(
                result,
                provider=str(reference["provider"]),
                raw_history_id=reference.get("raw_history_id"),
                raw_fetched_at=float(reference["raw_fetched_at"]),
                scan_run_id=scan_run_id,
            )
        except Exception as exc:
            logger.warning("Could not persist score history for %s: %s", result.symbol, exc)
    return writes


def score_deltas_for_history_writes(
    history_store: ScoreHistoryStore | None,
    writes: Mapping[str, ScoreHistoryWrite],
    results: Sequence[ScanResult],
) -> dict[str, dict[str, Any]]:
    if history_store is None or not writes:
        return {}

    deltas: dict[str, dict[str, Any]] = {}
    for result in results:
        write = writes.get(result.symbol)
        if write is None:
            continue
        try:
            deltas[result.symbol] = history_store.score_deltas(
                symbol=result.symbol,
                current_score=result.score,
                current_row_id=write.row_id,
            )
        except Exception as exc:
            logger.warning("Could not compute score deltas for %s: %s", result.symbol, exc)
    return deltas


def score_deltas_from_history(
    provider: MarketDataProvider,
    history_store: ScoreHistoryStore | None,
    results: Sequence[ScanResult],
) -> dict[str, dict[str, Any]]:
    if history_store is None or not results:
        return {}

    references = _raw_history_references(provider, results)
    deltas: dict[str, dict[str, Any]] = {}
    for result in results:
        reference = references.get(result.symbol)
        if not reference:
            continue
        try:
            row_id = history_store.find_score_row_id(
                provider=str(reference["provider"]),
                symbol=result.symbol,
                raw_fetched_at=float(reference["raw_fetched_at"]),
            )
            if row_id is None:
                continue
            deltas[result.symbol] = history_store.score_deltas(
                symbol=result.symbol,
                current_score=result.score,
                current_row_id=row_id,
            )
        except Exception as exc:
            logger.warning("Could not load score deltas for %s: %s", result.symbol, exc)
    return deltas


def recompute_score_history(
    provider: MarketDataProvider,
    history_store: ScoreHistoryStore | None,
    *,
    symbols: list[str] | None = None,
    from_timestamp: float | None = None,
    to_timestamp: float | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if history_store is None:
        return {"model": scoring_model_metadata(), "scan_run_id": None, "count": 0, "results": []}

    get_snapshots = getattr(provider, "historical_snapshots", None)
    if not callable(get_snapshots):
        return {"model": scoring_model_metadata(), "scan_run_id": None, "count": 0, "results": []}

    records = get_snapshots(
        symbols=symbols,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        limit=limit,
    )
    scan_run_id = f"recompute:{time.time():.6f}"
    rows: list[dict[str, Any]] = []
    for record in records:
        result = score_snapshot(record.snapshot)
        write = history_store.record_scan_result(
            result,
            provider=record.provider,
            raw_history_id=record.id,
            raw_fetched_at=record.fetched_at,
            scan_run_id=scan_run_id,
        )
        row = history_store.get(write.row_id)
        if row is not None:
            rows.append(row)

    return {
        "model": scoring_model_metadata(),
        "scan_run_id": scan_run_id,
        "count": len(rows),
        "results": rows,
    }


def _raw_history_references(
    provider: MarketDataProvider,
    results: Sequence[ScanResult],
) -> dict[str, dict[str, Any]]:
    get_references = getattr(provider, "raw_history_references", None)
    if not callable(get_references):
        return {}
    try:
        references = get_references([result.symbol for result in results])
    except Exception as exc:
        logger.warning("Could not load raw history references: %s", exc)
        return {}
    return {
        str(symbol): dict(reference)
        for symbol, reference in references.items()
        if reference and reference.get("raw_fetched_at") is not None
    }


def _ranking_options(
    ranking_mode: str | None,
    selected_model: str | None,
    sort_direction: str | None,
) -> RankingOptions:
    mode = normalize_ranking_mode(ranking_mode)
    return RankingOptions(
        mode=mode,
        selected_model=_normalize_model_key(selected_model) if selected_model else None,
        sort_direction=normalize_sort_direction(sort_direction, mode),
    )


def _ranking_sort_key(
    result: ScanResult,
    ranking: RankingOptions,
    score_deltas: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[bool, float, float, float, str]:
    value = _ranking_value(result, ranking, score_deltas or {})
    score = _numeric_field(result, "score") or 0.0
    data_quality = _numeric_field(result, "data_quality") or 0.0
    symbol = str(_field(result, "symbol") or "")

    if ranking.sort_direction == "asc":
        primary = value if value is not None else math.inf
    else:
        primary = -value if value is not None else math.inf

    return (value is None, primary, -score, -data_quality, symbol)


def _ranking_value(
    result: ScanResult,
    ranking: RankingOptions,
    score_deltas: Mapping[str, Mapping[str, Any]] | None = None,
) -> float | None:
    if ranking.mode == RANKING_MODE_TOP_SCORE:
        return _numeric_field(result, "score")
    if ranking.mode == RANKING_MODE_SELECTED_MODEL_SCORE:
        return _selected_model_score(result, ranking.selected_model)
    if ranking.mode == RANKING_MODE_HIGHEST_MODEL_CONFIDENCE:
        return _highest_model_confidence(result)
    if ranking.mode == RANKING_MODE_SCORE_INCREASE_1H:
        mapped_delta = _mapped_score_delta(result, score_deltas or {}, "1h")
        if mapped_delta is not None:
            return mapped_delta
        return _score_delta(result, "1h")
    if ranking.mode == RANKING_MODE_SCORE_INCREASE_24H:
        mapped_delta = _mapped_score_delta(result, score_deltas or {}, "24h")
        if mapped_delta is not None:
            return mapped_delta
        return _score_delta(result, "24h")
    if ranking.mode == RANKING_MODE_RELATIVE_VOLUME:
        return _metric_number(result, "relative_volume")
    if ranking.mode == RANKING_MODE_SHORT_INTEREST:
        return _metric_number(result, "short_percent_float", "short_interest")
    if ranking.mode == RANKING_MODE_SMALLEST_FLOAT:
        return _metric_number(result, "float_shares", "float")
    if ranking.mode == RANKING_MODE_HYBRID_ONLY:
        return _model_score(result, "hybrid")
    if ranking.mode == RANKING_MODE_GAMMA_CANDIDATE_ONLY:
        return _model_score(result, "gamma_candidate")
    return _numeric_field(result, "score")


def _mapped_score_delta(
    result: ScanResult,
    score_deltas: Mapping[str, Mapping[str, Any]],
    bucket: str,
) -> float | None:
    symbol = str(_field(result, "symbol") or "")
    payload = score_deltas.get(symbol)
    if not payload:
        return None
    keys = ("delta_1h", "score_delta_1h") if bucket == "1h" else ("delta_24h", "score_delta_24h")
    for key in keys:
        value = _as_number(payload.get(key))
        if value is not None:
            return value
    return None


def _selected_model_score(result: ScanResult, selected_model: str | None) -> float | None:
    model_key = selected_model
    if model_key is None:
        primary_model = _field(result, "primary_model")
        model_key = str(primary_model) if primary_model else None

    value = _model_score(result, model_key) if model_key else None
    if value is not None:
        return value
    return None if selected_model else _numeric_field(result, "score")


def _highest_model_confidence(result: ScanResult) -> float | None:
    for field_name in ("model_confidences", "model_confidence_scores", "model_confidence"):
        mapping = _mapping_field(result, field_name)
        values = [_as_number(value) for value in mapping.values()]
        numeric_values = [value for value in values if value is not None]
        if numeric_values:
            return max(numeric_values)

    return _metric_number(
        result,
        "highest_model_confidence",
        "model_confidence",
        "confidence",
        "data_confidence",
    )


def _score_delta(result: ScanResult, bucket: str) -> float | None:
    bucket_keys = {
        "1h": ("1h", "1_hour", "one_hour", "60m", "60_minute"),
        "24h": ("24h", "24_hour", "twenty_four_hour", "1d", "one_day"),
    }[bucket]
    for field_name in ("score_deltas", "score_changes", "score_increases", "deltas"):
        mapping = _mapping_field(result, field_name)
        for key in bucket_keys:
            value = _as_number(mapping.get(key))
            if value is not None:
                return value

    metric_keys = tuple(
        candidate
        for key in bucket_keys
        for candidate in (
            f"score_delta_{key}",
            f"score_change_{key}",
            f"score_increase_{key}",
            f"delta_{key}",
        )
    )
    return _metric_number(result, *metric_keys)


def _model_score(result: ScanResult, model_key: str | None) -> float | None:
    if not model_key:
        return None
    model_scores = _mapping_field(result, "model_scores")
    return _as_number(model_scores.get(model_key))


def _metric_number(result: ScanResult, *keys: str) -> float | None:
    metrics = _mapping_field(result, "metrics")
    for key in keys:
        value = _as_number(metrics.get(key))
        if value is not None:
            return value
        value = _numeric_field(result, key)
        if value is not None:
            return value
    return None


def _numeric_field(result: ScanResult, key: str) -> float | None:
    return _as_number(_field(result, key))


def _mapping_field(result: ScanResult, key: str) -> Mapping[str, Any]:
    value = _field(result, key)
    return value if isinstance(value, Mapping) else {}


def _field(result: ScanResult, key: str) -> Any:
    if isinstance(result, Mapping):
        return result.get(key)
    return getattr(result, key, None)


def _as_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize_option(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _normalize_model_key(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    key = _normalize_option(value)
    return _MODEL_KEY_ALIASES.get(key, key)


def _result_with_scan_time(
    result: ScanResult,
    scan_times: Mapping[str, float],
    generated_at: datetime,
    score_deltas: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    result_payload = result.to_dict()
    _apply_score_delta_fields(result_payload, (score_deltas or {}).get(result.symbol))
    scanned_at = scan_times.get(result.symbol)
    if scanned_at is None:
        result_payload["scanned_at"] = None
        result_payload["minutes_since_scan"] = None
        return result_payload

    scanned_at_datetime = datetime.fromtimestamp(scanned_at, timezone.utc)
    result_payload["scanned_at"] = scanned_at_datetime.isoformat()
    result_payload["minutes_since_scan"] = max(0, int((generated_at - scanned_at_datetime).total_seconds() // 60))
    return result_payload


def _apply_score_delta_fields(
    result_payload: dict[str, Any],
    deltas: Mapping[str, Any] | None,
) -> None:
    result_payload["previous_scan_delta"] = None
    result_payload["delta_24h"] = None
    result_payload["score_delta_status"] = "history_unavailable"
    if deltas:
        result_payload.update(dict(deltas))

    metrics = result_payload.get("metrics")
    if isinstance(metrics, dict):
        metrics["previous_scan_delta"] = result_payload["previous_scan_delta"]
        metrics["delta_24h"] = result_payload["delta_24h"]
        metrics["score_delta_24h"] = result_payload["delta_24h"]


def _split_symbols(values: Iterable[str]) -> list[str]:
    symbols: list[str] = []
    for value in values:
        symbols.extend(part.strip() for part in re.split(r"[\s,;]+", value) if part.strip())
    return symbols
