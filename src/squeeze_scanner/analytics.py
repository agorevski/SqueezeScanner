from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Mapping, Sequence

from .domain import ScanResult
from .scoring import SCORING_MODEL_VERSION, SCORING_MODELS

DEFAULT_HORIZONS: dict[str, int] = {
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
    "3d": 3 * 24 * 60 * 60,
    "5d": 5 * 24 * 60 * 60,
}
DEFAULT_DELTA_WINDOWS: tuple[str, ...] = ("previous", "1h", "24h", "7d")
DELTA_WINDOW_SECONDS: dict[str, int] = {
    "1h": 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
}
MODEL_LABELS = {str(model["key"]): str(model["label"]) for model in SCORING_MODELS}
DEFAULT_GAMMA_THRESHOLD_METRICS: tuple[str, ...] = (
    "net_gamma_exposure_pct_market_cap",
    "absolute_gamma_exposure_pct_market_cap",
    "gamma_flip_distance_pct",
    "call_wall_distance_pct",
    "put_wall_distance_pct",
    "gamma_strike_concentration_pct",
    "gamma_expiration_concentration_pct",
    "open_interest_change",
)
GAMMA_THRESHOLD_METRIC_LABELS: dict[str, str] = {
    "net_gamma_exposure_pct_market_cap": "Net GEX % market cap",
    "absolute_gamma_exposure_pct_market_cap": "Absolute GEX % market cap",
    "gamma_flip_distance_pct": "Gamma flip distance %",
    "call_wall_distance_pct": "Call-wall distance %",
    "put_wall_distance_pct": "Put-wall distance %",
    "gamma_strike_concentration_pct": "Gamma strike concentration %",
    "gamma_expiration_concentration_pct": "Gamma expiration concentration %",
    "open_interest_change": "Open-interest change",
}


@dataclass(frozen=True)
class PriceBar:
    symbol: str
    observed_at: datetime | str | int | float
    close: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    provider: str = "manual"


@dataclass(frozen=True)
class Horizon:
    label: str
    seconds: int


class AnalyticsStore:
    """SQLite-backed analytics, calibration, delta, and report queries.

    The store reads persisted score rows and price bars only. Outcome generation
    intentionally does not call live providers or rescore snapshots, which keeps
    scoring inputs point-in-time and avoids lookahead bias.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._schema_ready = False
        self._schema_lock = Lock()

    def insert_price_bar(self, bar: PriceBar | Mapping[str, Any]) -> int:
        return self.insert_price_history([bar])[0]

    def insert_price_history(self, bars: Iterable[PriceBar | Mapping[str, Any]]) -> list[int]:
        self._ensure_schema()
        rows = [_price_bar_payload(bar) for bar in bars]
        if not rows:
            return []

        ids: list[int] = []
        with self._connect() as connection:
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO price_history (
                        symbol,
                        provider,
                        observed_at,
                        open,
                        high,
                        low,
                        close,
                        volume
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(symbol, provider, observed_at) DO UPDATE SET
                        open = excluded.open,
                        high = excluded.high,
                        low = excluded.low,
                        close = excluded.close,
                        volume = excluded.volume
                    """,
                    (
                        row["symbol"],
                        row["provider"],
                        row["observed_at"],
                        row["open"],
                        row["high"],
                        row["low"],
                        row["close"],
                        row["volume"],
                    ),
                )
                stored_id = connection.execute(
                    """
                    SELECT id
                    FROM price_history
                    WHERE symbol = ? AND provider = ? AND observed_at = ?
                    """,
                    (row["symbol"], row["provider"], row["observed_at"]),
                ).fetchone()
                ids.append(int(stored_id["id"]))
        return ids

    def insert_score_history(
        self,
        result: ScanResult | Mapping[str, Any],
        *,
        created_at: datetime | str | int | float | None = None,
        provider: str | None = None,
        raw_history_id: int | None = None,
        scoring_model_version: str = SCORING_MODEL_VERSION,
    ) -> int:
        self._ensure_schema()
        payload = result.to_dict() if isinstance(result, ScanResult) else dict(result)
        created_at_iso = _utc_iso(created_at if created_at is not None else time.time())
        model_scores = _coerce_json_object(payload.get("model_scores") or payload.get("model_scores_json"))
        model_components = _coerce_json_object(
            payload.get("model_components") or payload.get("model_components_json")
        )
        model_rationales = _coerce_json_object(
            payload.get("model_rationales") or payload.get("model_rationales_json")
        )
        model_confidence = _coerce_json_object(
            payload.get("model_confidence")
            or payload.get("model_confidence_json")
            or payload.get("confidence")
        )
        metrics = _coerce_json_object(payload.get("metrics") or payload.get("metrics_json"))
        metrics = _metrics_with_source_metadata(metrics, payload)
        risk_flags = _coerce_json_value(payload.get("risk_flags") or payload.get("risk_flags_json"), default=[])
        warnings = _coerce_json_value(payload.get("warnings") or payload.get("warnings_json"), default=[])
        symbol = str(payload["symbol"]).upper()

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_score_history (
                    symbol,
                    company_name,
                    provider,
                    raw_history_id,
                    scoring_model_version,
                    primary_model,
                    score,
                    risk_level,
                    data_quality,
                    model_scores_json,
                    model_components_json,
                    model_rationales_json,
                    model_confidence_json,
                    metrics_json,
                    risk_flags_json,
                    warnings_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    payload.get("company_name"),
                    provider or payload.get("provider"),
                    raw_history_id if raw_history_id is not None else payload.get("raw_history_id"),
                    payload.get("scoring_model_version") or scoring_model_version,
                    payload.get("primary_model"),
                    _as_float(payload.get("score"), default=0.0),
                    payload.get("risk_level"),
                    _as_float(payload.get("data_quality")),
                    _json_dumps(model_scores),
                    _json_dumps(model_components),
                    _json_dumps(model_rationales),
                    _json_dumps(model_confidence),
                    _json_dumps(metrics),
                    _json_dumps(risk_flags),
                    _json_dumps(warnings),
                    created_at_iso,
                ),
            )
            return int(cursor.lastrowid)

    def compute_due_outcomes(
        self,
        *,
        as_of: datetime | str | int | float | None = None,
        horizons: Mapping[str, int] | Sequence[str | int | Horizon] | None = None,
        move_threshold_pct: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Compute outcomes whose horizons have elapsed as of ``as_of``.

        The calculation uses only persisted scan rows and persisted prices. A
        scan at T is skipped until T + horizon <= as_of even when later price
        bars are already present in storage.
        """

        self._ensure_schema()
        as_of_dt = _coerce_datetime(as_of if as_of is not None else time.time())
        horizon_list = _normalize_horizons(horizons)
        inserted: list[dict[str, Any]] = []

        with self._connect() as connection:
            score_rows = [
                _score_row_payload(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM scan_score_history
                    ORDER BY created_at ASC, id ASC
                    """
                ).fetchall()
            ]

            for score_row in score_rows:
                scan_at = score_row["created_at_dt"]
                if scan_at > as_of_dt:
                    continue
                for horizon in horizon_list:
                    if scan_at + timedelta(seconds=horizon.seconds) > as_of_dt:
                        continue
                    outcomes = self._compute_outcomes_for_score_row(
                        connection,
                        score_row,
                        horizon,
                        as_of_dt,
                        move_threshold_pct,
                    )
                    inserted.extend(outcomes)
                    if limit is not None and len(inserted) >= limit:
                        return inserted[:limit]

        return inserted

    def calibration_report(
        self,
        *,
        model: str,
        horizon: str | int | Horizon = "1d",
        bucket_size: float = 10.0,
        scoring_model_version: str | None = None,
        slice_by_source_quality: bool = False,
        source_quality_slice: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        if bucket_size <= 0:
            raise ValueError("bucket_size must be greater than zero")

        selected_horizon = _normalize_horizons([horizon])[0]
        rows: list[sqlite3.Row]
        with self._connect() as connection:
            params: list[Any] = [model, selected_horizon.seconds]
            version_clause = ""
            if scoring_model_version is not None:
                version_clause = "AND o.scoring_model_version = ?"
                params.append(scoring_model_version)
            rows = connection.execute(
                f"""
                SELECT o.score_at_scan,
                       o.forward_return_pct,
                       o.max_favorable_excursion_pct,
                       o.max_adverse_excursion_pct,
                       o.scoring_model_version,
                       s.provider,
                       s.data_quality,
                       s.metrics_json,
                       s.risk_flags_json,
                       s.warnings_json
                FROM scan_outcomes AS o
                LEFT JOIN scan_score_history AS s
                  ON s.id = o.scan_score_history_id
                WHERE o.model = ?
                  AND o.horizon_seconds = ?
                  {version_clause}
                ORDER BY o.score_at_scan ASC, o.id ASC
                """,
                params,
            ).fetchall()

        normalized_source_slice = _normalize_source_slice(source_quality_slice)
        use_source_slices = slice_by_source_quality or normalized_source_slice is not None
        buckets: dict[tuple[float, str | None], list[dict[str, float | None]]] = {}
        for row in rows:
            score = _as_float(row["score_at_scan"])
            if score is None:
                continue
            bucket_start = _bucket_start(score, bucket_size)
            source_slices: list[str | None] = [None]
            if use_source_slices:
                source_slices = _source_quality_slices(_outcome_source_context(row))
                if normalized_source_slice is not None:
                    source_slices = [name for name in source_slices if name == normalized_source_slice]
                if not source_slices:
                    continue
            values = {
                "return": _as_float(row["forward_return_pct"]),
                "mfe": _as_float(row["max_favorable_excursion_pct"]),
                "mae": _as_float(row["max_adverse_excursion_pct"]),
            }
            for source_slice in source_slices:
                buckets.setdefault((bucket_start, source_slice), []).append(values)

        report: list[dict[str, Any]] = []
        for bucket_start, source_slice in sorted(buckets, key=lambda key: (str(key[1] or ""), key[0])):
            values = buckets[(bucket_start, source_slice)]
            returns = [float(value["return"]) for value in values if value["return"] is not None]
            adverse = [float(value["mae"]) for value in values if value["mae"] is not None]
            favorable = [float(value["mfe"]) for value in values if value["mfe"] is not None]
            bucket_end = min(100.0, bucket_start + bucket_size)
            row_payload = {
                "model": model,
                "horizon": selected_horizon.label,
                "horizon_seconds": selected_horizon.seconds,
                "score_bucket": _bucket_label(bucket_start, bucket_end),
                "bucket_start": _clean_number(bucket_start),
                "bucket_end": _clean_number(bucket_end),
                "count": len(values),
                "avg_return_pct": _mean(returns),
                "win_rate": _mean([1.0 if value > 0 else 0.0 for value in returns]),
                "avg_max_favorable_excursion_pct": _mean(favorable),
                "avg_max_adverse_excursion_pct": _mean(adverse),
                "worst_max_adverse_excursion_pct": _round_number(min(adverse)) if adverse else None,
            }
            if source_slice is not None:
                row_payload["source_quality_slice"] = source_slice
            report.append(row_payload)
        return report

    def report_model_version_comparison(
        self,
        *,
        model: str,
        horizon: str | int | Horizon = "1d",
        base_version: str | None = None,
        compare_version: str | None = None,
        bucket_size: float = 10.0,
        deterioration_threshold_pct: float = 0.0,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        if bucket_size <= 0:
            raise ValueError("bucket_size must be greater than zero")
        selected_horizon = _normalize_horizons([horizon])[0]
        base_version, compare_version = self._resolve_model_versions(
            model=model,
            horizon=selected_horizon,
            base_version=base_version,
            compare_version=compare_version,
        )
        if base_version is None or compare_version is None:
            return []
        if base_version == compare_version:
            raise ValueError("base_version and compare_version must be different")

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT base.symbol,
                       base.scan_score_history_id AS base_score_history_id,
                       compare.scan_score_history_id AS compare_score_history_id,
                       base.scan_created_at,
                       base.model,
                       base.horizon_label,
                       base.horizon_seconds,
                       base.scoring_model_version AS base_scoring_model_version,
                       compare.scoring_model_version AS compare_scoring_model_version,
                       base.score_at_scan AS base_score,
                       compare.score_at_scan AS compare_score,
                       base.forward_return_pct AS base_forward_return_pct,
                       compare.forward_return_pct AS compare_forward_return_pct,
                       base.max_favorable_excursion_pct AS base_max_favorable_excursion_pct,
                       compare.max_favorable_excursion_pct AS compare_max_favorable_excursion_pct,
                       base.max_adverse_excursion_pct AS base_max_adverse_excursion_pct,
                       compare.max_adverse_excursion_pct AS compare_max_adverse_excursion_pct
                FROM scan_outcomes AS base
                JOIN scan_outcomes AS compare
                  ON compare.symbol = base.symbol
                 AND compare.scan_created_at = base.scan_created_at
                 AND compare.model = base.model
                 AND compare.horizon_seconds = base.horizon_seconds
                WHERE base.model = ?
                  AND base.horizon_seconds = ?
                  AND base.scoring_model_version = ?
                  AND compare.scoring_model_version = ?
                ORDER BY ABS(COALESCE(compare.score_at_scan, 0) - COALESCE(base.score_at_scan, 0)) DESC,
                         base.scan_created_at DESC,
                         base.symbol ASC
                """,
                (model, selected_horizon.seconds, base_version, compare_version),
            ).fetchall()

        report_rows: list[dict[str, Any]] = []
        for row in rows:
            base_score = _as_float(row["base_score"])
            compare_score = _as_float(row["compare_score"])
            if base_score is None or compare_score is None:
                continue
            base_bucket_start = _bucket_start(base_score, bucket_size)
            compare_bucket_start = _bucket_start(compare_score, bucket_size)
            base_bucket_end = min(100.0, base_bucket_start + bucket_size)
            compare_bucket_end = min(100.0, compare_bucket_start + bucket_size)
            compare_return = _as_float(row["compare_forward_return_pct"])
            compare_mfe = _as_float(row["compare_max_favorable_excursion_pct"])
            compare_mae = _as_float(row["compare_max_adverse_excursion_pct"])
            report_rows.append(
                {
                    "symbol": row["symbol"],
                    "scan_created_at": row["scan_created_at"],
                    "model": row["model"],
                    "horizon": row["horizon_label"],
                    "horizon_seconds": int(row["horizon_seconds"]),
                    "base_scoring_model_version": row["base_scoring_model_version"],
                    "compare_scoring_model_version": row["compare_scoring_model_version"],
                    "base_score_history_id": row["base_score_history_id"],
                    "compare_score_history_id": row["compare_score_history_id"],
                    "base_score": _round_number(base_score),
                    "compare_score": _round_number(compare_score),
                    "score_delta": _round_number(compare_score - base_score),
                    "base_score_bucket": _bucket_label(base_bucket_start, base_bucket_end),
                    "compare_score_bucket": _bucket_label(compare_bucket_start, compare_bucket_end),
                    "base_forward_return_pct": _round_number(_as_float(row["base_forward_return_pct"])),
                    "compare_forward_return_pct": _round_number(compare_return),
                    "forward_return_pct": _round_number(compare_return),
                    "base_max_favorable_excursion_pct": _round_number(
                        _as_float(row["base_max_favorable_excursion_pct"])
                    ),
                    "compare_max_favorable_excursion_pct": _round_number(compare_mfe),
                    "max_favorable_excursion_pct": _round_number(compare_mfe),
                    "base_max_adverse_excursion_pct": _round_number(
                        _as_float(row["base_max_adverse_excursion_pct"])
                    ),
                    "compare_max_adverse_excursion_pct": _round_number(compare_mae),
                    "max_adverse_excursion_pct": _round_number(compare_mae),
                    "base_deteriorated": _outcome_deteriorated(
                        row["base_forward_return_pct"],
                        row["base_max_adverse_excursion_pct"],
                        deterioration_threshold_pct,
                    ),
                    "compare_deteriorated": _outcome_deteriorated(
                        row["compare_forward_return_pct"],
                        row["compare_max_adverse_excursion_pct"],
                        deterioration_threshold_pct,
                    ),
                }
            )
        return _ranked(report_rows, limit=limit, offset=offset)

    def report_gamma_threshold_review(
        self,
        *,
        model: str = "gamma_candidate",
        horizon: str | int | Horizon = "1d",
        metrics: Sequence[str] | str | None = None,
        bucket_size: float = 5.0,
        scoring_model_version: str | None = None,
        include_missing: bool = True,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        if bucket_size <= 0:
            raise ValueError("bucket_size must be greater than zero")
        selected_horizon = _normalize_horizons([horizon])[0]
        selected_metrics = _normalize_gamma_metrics(metrics)

        with self._connect() as connection:
            params: list[Any] = [model, selected_horizon.seconds]
            version_clause = ""
            if scoring_model_version is not None:
                version_clause = "AND o.scoring_model_version = ?"
                params.append(scoring_model_version)
            rows = connection.execute(
                f"""
                SELECT o.model,
                       o.horizon_label,
                       o.horizon_seconds,
                       o.scoring_model_version,
                       o.forward_return_pct,
                       o.max_favorable_excursion_pct,
                       o.max_adverse_excursion_pct,
                       s.metrics_json
                FROM scan_outcomes AS o
                LEFT JOIN scan_score_history AS s
                  ON s.id = o.scan_score_history_id
                WHERE o.model = ?
                  AND o.horizon_seconds = ?
                  {version_clause}
                ORDER BY o.scan_created_at ASC, o.id ASC
                """,
                params,
            ).fetchall()

        grouped: dict[tuple[str, str], list[dict[str, float | None]]] = {}
        bucket_meta: dict[tuple[str, str], dict[str, Any]] = {}
        metric_order = {metric: index for index, metric in enumerate(selected_metrics)}
        for row in rows:
            row_metrics = _loads_json(row["metrics_json"], default={})
            if not isinstance(row_metrics, Mapping):
                row_metrics = {}
            for metric in selected_metrics:
                metric_value = _gamma_metric_value(metric, row_metrics)
                if metric_value is None:
                    if not include_missing:
                        continue
                    bucket_key = "missing"
                    meta = {
                        "metric": metric,
                        "metric_label": GAMMA_THRESHOLD_METRIC_LABELS.get(metric, metric),
                        "metric_bucket": "missing",
                        "bucket_start": None,
                        "bucket_end": None,
                        "is_missing_bucket": True,
                    }
                else:
                    bucket_start = _value_bucket_start(metric_value, bucket_size)
                    bucket_end = bucket_start + bucket_size
                    bucket_key = _bucket_label(bucket_start, bucket_end)
                    meta = {
                        "metric": metric,
                        "metric_label": GAMMA_THRESHOLD_METRIC_LABELS.get(metric, metric),
                        "metric_bucket": bucket_key,
                        "bucket_start": _clean_number(bucket_start),
                        "bucket_end": _clean_number(bucket_end),
                        "is_missing_bucket": False,
                    }
                key = (metric, bucket_key)
                bucket_meta.setdefault(key, meta)
                grouped.setdefault(key, []).append(
                    {
                        "metric_value": metric_value,
                        "return": _as_float(row["forward_return_pct"]),
                        "mfe": _as_float(row["max_favorable_excursion_pct"]),
                        "mae": _as_float(row["max_adverse_excursion_pct"]),
                    }
                )

        report_rows: list[dict[str, Any]] = []
        sorted_keys = sorted(
            grouped,
            key=lambda key: (
                metric_order.get(key[0], len(metric_order)),
                bool(bucket_meta[key]["is_missing_bucket"]),
                float(bucket_meta[key]["bucket_start"] or 0),
                key[1],
            ),
        )
        for key in sorted_keys:
            values = grouped[key]
            metric_values = [
                float(value["metric_value"]) for value in values if value["metric_value"] is not None
            ]
            returns = [float(value["return"]) for value in values if value["return"] is not None]
            adverse = [float(value["mae"]) for value in values if value["mae"] is not None]
            favorable = [float(value["mfe"]) for value in values if value["mfe"] is not None]
            row_payload = {
                "model": model,
                "horizon": selected_horizon.label,
                "horizon_seconds": selected_horizon.seconds,
                **bucket_meta[key],
                "count": len(values),
                "missing_count": sum(1 for value in values if value["metric_value"] is None),
                "avg_metric_value": _mean(metric_values),
                "avg_return_pct": _mean(returns),
                "win_rate": _mean([1.0 if value > 0 else 0.0 for value in returns]),
                "avg_max_favorable_excursion_pct": _mean(favorable),
                "avg_max_adverse_excursion_pct": _mean(adverse),
                "worst_max_adverse_excursion_pct": _round_number(min(adverse)) if adverse else None,
            }
            row_payload.pop("is_missing_bucket", None)
            report_rows.append(row_payload)
        return _ranked(report_rows, limit=limit, offset=offset)

    def explain_score_deltas(
        self,
        symbol: str,
        *,
        windows: Sequence[str] = DEFAULT_DELTA_WINDOWS,
        as_of: datetime | str | int | float | None = None,
        driver_limit: int = 10,
    ) -> dict[str, Any]:
        self._ensure_schema()
        normalized_symbol = symbol.upper()
        rows = self._score_rows_for_symbol(normalized_symbol, as_of=as_of)
        if not rows:
            return {
                "symbol": normalized_symbol,
                "status": "not_enough_history",
                "reason": "No score history found.",
                "windows": [],
            }

        latest = rows[-1]
        payload = {
            "symbol": normalized_symbol,
            "status": "ok",
            "latest_score_history_id": latest["id"],
            "latest_created_at": latest["created_at"],
            "latest_score": _round_number(latest["score"]),
            "windows": [],
        }
        for window in windows:
            payload["windows"].append(
                self._explain_window_delta(rows, latest, window, driver_limit=driver_limit)
            )
        return payload

    def report_top_new_high_setups(
        self,
        *,
        start_at: datetime | str | int | float,
        end_at: datetime | str | int | float,
        model: str | None = None,
        min_score: float = 70.0,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        start_dt = _coerce_datetime(start_at)
        end_dt = _coerce_datetime(end_at)
        rows = self._all_score_rows(end_at=end_dt)
        prior_high_symbols = {
            row["symbol"]
            for row in rows
            if row["created_at_dt"] < start_dt and _report_score(row, model) >= min_score
        }
        best_by_symbol: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not (start_dt <= row["created_at_dt"] <= end_dt):
                continue
            if row["symbol"] in prior_high_symbols:
                continue
            score = _report_score(row, model)
            if score < min_score:
                continue
            current = _report_row(row, score=score)
            existing = best_by_symbol.get(row["symbol"])
            if existing is None or (
                current["score"],
                current["created_at"],
                current["symbol"],
            ) > (
                existing["score"],
                existing["created_at"],
                existing["symbol"],
            ):
                best_by_symbol[row["symbol"]] = current

        report_rows = sorted(
            best_by_symbol.values(),
            key=lambda row: (-row["score"], row["created_at"], row["symbol"]),
        )
        return _ranked(report_rows, limit=limit, offset=offset)

    def report_biggest_score_increases(
        self,
        *,
        window: str = "1h",
        start_at: datetime | str | int | float | None = None,
        end_at: datetime | str | int | float | None = None,
        model: str | None = None,
        min_delta: float = 0.0,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        window_seconds = _window_seconds(window)
        rows = self._all_score_rows(end_at=end_at)
        start_dt = _coerce_datetime(start_at) if start_at is not None else None
        end_dt = _coerce_datetime(end_at) if end_at is not None else None
        best_by_symbol: dict[str, dict[str, Any]] = {}

        for row in rows:
            if start_dt is not None and row["created_at_dt"] < start_dt:
                continue
            if end_dt is not None and row["created_at_dt"] > end_dt:
                continue
            baseline = _baseline_at_or_before(rows, row["symbol"], row["created_at_dt"], window_seconds)
            if baseline is None:
                continue
            score = _report_score(row, model)
            baseline_score = _report_score(baseline, model)
            delta = score - baseline_score
            if delta <= min_delta:
                continue
            current = _report_row(row, score=score)
            current.update(
                {
                    "window": window,
                    "score_delta": _round_number(delta),
                    "baseline_score": _round_number(baseline_score),
                    "baseline_created_at": baseline["created_at"],
                    "baseline_score_history_id": baseline["id"],
                }
            )
            existing = best_by_symbol.get(row["symbol"])
            if existing is None or (
                current["score_delta"],
                current["score"],
                current["created_at"],
            ) > (
                existing["score_delta"],
                existing["score"],
                existing["created_at"],
            ):
                best_by_symbol[row["symbol"]] = current

        report_rows = sorted(
            best_by_symbol.values(),
            key=lambda row: (-row["score_delta"], -row["score"], row["symbol"]),
        )
        return _ranked(report_rows, limit=limit, offset=offset)

    def report_biggest_1h_increases(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.report_biggest_score_increases(window="1h", **kwargs)

    def report_biggest_24h_increases(self, **kwargs: Any) -> list[dict[str, Any]]:
        return self.report_biggest_score_increases(window="24h", **kwargs)

    def report_repeated_high_setups(
        self,
        *,
        start_at: datetime | str | int | float,
        end_at: datetime | str | int | float,
        model: str | None = None,
        min_score: float = 70.0,
        min_count: int = 2,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        start_dt = _coerce_datetime(start_at)
        end_dt = _coerce_datetime(end_at)
        rows = [
            row
            for row in self._all_score_rows(start_at=start_dt, end_at=end_dt)
            if _report_score(row, model) >= min_score
        ]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["symbol"], []).append(row)

        report_rows: list[dict[str, Any]] = []
        for symbol, symbol_rows in grouped.items():
            if len(symbol_rows) < min_count:
                continue
            scores = [_report_score(row, model) for row in symbol_rows]
            report_rows.append(
                {
                    "symbol": symbol,
                    "company_name": symbol_rows[-1]["company_name"],
                    "setup_count": len(symbol_rows),
                    "max_score": _round_number(max(scores)),
                    "first_seen_at": symbol_rows[0]["created_at"],
                    "last_seen_at": symbol_rows[-1]["created_at"],
                    "latest_score_history_id": symbol_rows[-1]["id"],
                    "scoring_model_version": symbol_rows[-1]["scoring_model_version"],
                }
            )

        report_rows.sort(key=lambda row: (-row["setup_count"], -row["max_score"], row["symbol"]))
        return _ranked(report_rows, limit=limit, offset=offset)

    def report_deterioration(
        self,
        *,
        window: str = "24h",
        start_at: datetime | str | int | float | None = None,
        end_at: datetime | str | int | float | None = None,
        model: str | None = None,
        min_drop: float = 0.0,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        window_seconds = _window_seconds(window)
        rows = self._all_score_rows(end_at=end_at)
        start_dt = _coerce_datetime(start_at) if start_at is not None else None
        end_dt = _coerce_datetime(end_at) if end_at is not None else None
        best_by_symbol: dict[str, dict[str, Any]] = {}

        for row in rows:
            if start_dt is not None and row["created_at_dt"] < start_dt:
                continue
            if end_dt is not None and row["created_at_dt"] > end_dt:
                continue
            baseline = _baseline_at_or_before(rows, row["symbol"], row["created_at_dt"], window_seconds)
            if baseline is None:
                continue
            score = _report_score(row, model)
            baseline_score = _report_score(baseline, model)
            delta = score - baseline_score
            drop = -delta
            if drop <= min_drop:
                continue
            current = _report_row(row, score=score)
            current.update(
                {
                    "window": window,
                    "score_delta": _round_number(delta),
                    "score_drop": _round_number(drop),
                    "baseline_score": _round_number(baseline_score),
                    "baseline_created_at": baseline["created_at"],
                    "baseline_score_history_id": baseline["id"],
                }
            )
            existing = best_by_symbol.get(row["symbol"])
            if existing is None or (
                current["score_drop"],
                current["baseline_score"],
                current["created_at"],
            ) > (
                existing["score_drop"],
                existing["baseline_score"],
                existing["created_at"],
            ):
                best_by_symbol[row["symbol"]] = current

        report_rows = sorted(
            best_by_symbol.values(),
            key=lambda row: (-row["score_drop"], row["score"], row["symbol"]),
        )
        return _ranked(report_rows, limit=limit, offset=offset)

    def rows_to_csv(self, rows: Sequence[Mapping[str, Any]], columns: Sequence[str] | None = None) -> str:
        return rows_to_csv(rows, columns=columns)

    def _resolve_model_versions(
        self,
        *,
        model: str,
        horizon: Horizon,
        base_version: str | None,
        compare_version: str | None,
    ) -> tuple[str | None, str | None]:
        if base_version is not None and compare_version is not None:
            return base_version, compare_version
        with self._connect() as connection:
            version_rows = connection.execute(
                """
                SELECT scoring_model_version,
                       MIN(scan_created_at) AS first_scan_at,
                       MAX(scan_created_at) AS last_scan_at
                FROM scan_outcomes
                WHERE model = ?
                  AND horizon_seconds = ?
                GROUP BY scoring_model_version
                ORDER BY first_scan_at ASC, last_scan_at ASC, scoring_model_version ASC
                """,
                (model, horizon.seconds),
            ).fetchall()
        versions = [str(row["scoring_model_version"]) for row in version_rows]
        if len(versions) < 2:
            return base_version, compare_version
        if compare_version is None:
            candidates = [version for version in versions if version != base_version]
            compare_version = candidates[-1] if candidates else None
        if base_version is None:
            candidates = [version for version in versions if version != compare_version]
            base_version = candidates[-1] if candidates else None
        return base_version, compare_version

    def _compute_outcomes_for_score_row(
        self,
        connection: sqlite3.Connection,
        score_row: dict[str, Any],
        horizon: Horizon,
        as_of_dt: datetime,
        move_threshold_pct: float | None,
    ) -> list[dict[str, Any]]:
        scan_at = score_row["created_at_dt"]
        target_at = scan_at + timedelta(seconds=horizon.seconds)
        model_scores = score_row["model_scores"] or {}
        if not model_scores and score_row["primary_model"]:
            model_scores = {score_row["primary_model"]: score_row["score"]}
        if not model_scores:
            return []

        metrics = score_row["metrics"] or {}
        entry_price = _positive_float(metrics.get("price"))
        entry_observed_at = score_row["created_at"]
        if entry_price is None:
            entry = _price_at_or_before(connection, score_row["symbol"], scan_at)
            if entry is None:
                return []
            entry_price = float(entry["close"])
            entry_observed_at = entry["observed_at"]

        exit_bar = _first_price_at_or_after(connection, score_row["symbol"], target_at, as_of_dt)
        if exit_bar is None:
            return []

        exit_price = _positive_float(exit_bar["close"])
        if exit_price is None:
            return []
        exit_at = _coerce_datetime(exit_bar["observed_at"])
        price_window = _price_window(connection, score_row["symbol"], scan_at, exit_at)
        forward_return_pct = _pct_change(entry_price, exit_price)
        max_favorable, max_adverse = _excursions(entry_price, price_window, fallback_return=forward_return_pct)
        peak_volume_expansion = _peak_volume_expansion(metrics, price_window)
        next_gap_pct = _next_day_gap_pct(connection, score_row["symbol"], scan_at, entry_price, as_of_dt)
        threshold = _as_float(move_threshold_pct)
        hit_threshold = None
        if threshold is not None:
            hit_threshold = int(forward_return_pct >= threshold)

        inserted: list[dict[str, Any]] = []
        for model, model_score in sorted(model_scores.items()):
            score_at_scan = _as_float(model_score)
            if score_at_scan is None:
                continue
            if _outcome_exists(connection, score_row, model, horizon):
                continue
            outcome_payload = {
                "symbol": score_row["symbol"],
                "scan_score_history_id": score_row["id"],
                "scoring_model_version": score_row["scoring_model_version"],
                "model": model,
                "score_at_scan": _round_number(score_at_scan),
                "horizon_label": horizon.label,
                "horizon_seconds": horizon.seconds,
                "scan_created_at": score_row["created_at"],
                "outcome_computed_at": _utc_iso(as_of_dt),
                "entry_price": _round_number(entry_price),
                "entry_observed_at": entry_observed_at,
                "exit_price": _round_number(exit_price),
                "exit_observed_at": exit_bar["observed_at"],
                "forward_return_pct": _round_number(forward_return_pct),
                "max_favorable_excursion_pct": _round_number(max_favorable),
                "max_adverse_excursion_pct": _round_number(max_adverse),
                "next_day_gap_pct": _round_number(next_gap_pct) if next_gap_pct is not None else None,
                "peak_volume_expansion": _round_number(peak_volume_expansion)
                if peak_volume_expansion is not None
                else None,
                "move_threshold_pct": threshold,
                "hit_move_threshold": hit_threshold,
            }
            connection.execute(
                """
                INSERT INTO scan_outcomes (
                    symbol,
                    scan_score_history_id,
                    scoring_model_version,
                    model,
                    score_at_scan,
                    horizon_label,
                    horizon_seconds,
                    scan_created_at,
                    outcome_computed_at,
                    entry_price,
                    entry_observed_at,
                    exit_price,
                    exit_observed_at,
                    forward_return_pct,
                    max_favorable_excursion_pct,
                    max_adverse_excursion_pct,
                    next_day_gap_pct,
                    peak_volume_expansion,
                    move_threshold_pct,
                    hit_move_threshold
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome_payload["symbol"],
                    outcome_payload["scan_score_history_id"],
                    outcome_payload["scoring_model_version"],
                    outcome_payload["model"],
                    outcome_payload["score_at_scan"],
                    outcome_payload["horizon_label"],
                    outcome_payload["horizon_seconds"],
                    outcome_payload["scan_created_at"],
                    outcome_payload["outcome_computed_at"],
                    outcome_payload["entry_price"],
                    outcome_payload["entry_observed_at"],
                    outcome_payload["exit_price"],
                    outcome_payload["exit_observed_at"],
                    outcome_payload["forward_return_pct"],
                    outcome_payload["max_favorable_excursion_pct"],
                    outcome_payload["max_adverse_excursion_pct"],
                    outcome_payload["next_day_gap_pct"],
                    outcome_payload["peak_volume_expansion"],
                    outcome_payload["move_threshold_pct"],
                    outcome_payload["hit_move_threshold"],
                ),
            )
            inserted.append(outcome_payload)
        return inserted

    def _explain_window_delta(
        self,
        rows: Sequence[dict[str, Any]],
        latest: dict[str, Any],
        window: str,
        *,
        driver_limit: int,
    ) -> dict[str, Any]:
        baseline = _baseline_for_delta(rows, latest, window)
        if baseline is None:
            reason = "No previous score row found."
            if window != "previous":
                target_at = latest["created_at_dt"] - timedelta(seconds=_window_seconds(window))
                reason = f"No baseline score row at or before {_utc_iso(target_at)}."
            return {
                "symbol": latest["symbol"],
                "window": window,
                "status": "not_enough_history",
                "reason": reason,
                "latest_score_history_id": latest["id"],
                "latest_created_at": latest["created_at"],
                "drivers": [],
            }

        score_delta = latest["score"] - baseline["score"]
        drivers = _delta_drivers(baseline, latest)[:driver_limit]
        return {
            "symbol": latest["symbol"],
            "window": window,
            "status": "ok",
            "latest_score_history_id": latest["id"],
            "latest_created_at": latest["created_at"],
            "baseline_score_history_id": baseline["id"],
            "baseline_created_at": baseline["created_at"],
            "score_delta": _round_number(score_delta),
            "latest_score": _round_number(latest["score"]),
            "baseline_score": _round_number(baseline["score"]),
            "drivers": drivers,
        }

    def _score_rows_for_symbol(
        self,
        symbol: str,
        *,
        as_of: datetime | str | int | float | None = None,
    ) -> list[dict[str, Any]]:
        as_of_dt = _coerce_datetime(as_of) if as_of is not None else None
        with self._connect() as connection:
            rows = [
                _score_row_payload(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM scan_score_history
                    WHERE symbol = ?
                    ORDER BY created_at ASC, id ASC
                    """,
                    (symbol,),
                ).fetchall()
            ]
        if as_of_dt is not None:
            rows = [row for row in rows if row["created_at_dt"] <= as_of_dt]
        return sorted(rows, key=lambda row: (row["created_at_dt"], row["id"]))

    def _all_score_rows(
        self,
        *,
        start_at: datetime | str | int | float | None = None,
        end_at: datetime | str | int | float | None = None,
    ) -> list[dict[str, Any]]:
        start_dt = _coerce_datetime(start_at) if start_at is not None else None
        end_dt = _coerce_datetime(end_at) if end_at is not None else None
        with self._connect() as connection:
            rows = [
                _score_row_payload(row)
                for row in connection.execute(
                    """
                    SELECT *
                    FROM scan_score_history
                    ORDER BY created_at ASC, id ASC
                    """
                ).fetchall()
            ]
        if start_dt is not None:
            rows = [row for row in rows if row["created_at_dt"] >= start_dt]
        if end_dt is not None:
            rows = [row for row in rows if row["created_at_dt"] <= end_dt]
        return sorted(rows, key=lambda row: (row["created_at_dt"], row["id"]))

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return

        with self._schema_lock:
            if self._schema_ready:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS scan_score_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        company_name TEXT,
                        provider TEXT,
                        raw_history_id INTEGER,
                        scoring_model_version TEXT NOT NULL DEFAULT 'unknown',
                        primary_model TEXT,
                        score REAL NOT NULL DEFAULT 0,
                        risk_level TEXT,
                        data_quality REAL,
                        model_scores_json TEXT NOT NULL DEFAULT '{}',
                        model_components_json TEXT NOT NULL DEFAULT '{}',
                        model_rationales_json TEXT NOT NULL DEFAULT '{}',
                        model_confidence_json TEXT NOT NULL DEFAULT '{}',
                        metrics_json TEXT NOT NULL DEFAULT '{}',
                        risk_flags_json TEXT NOT NULL DEFAULT '[]',
                        warnings_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL
                    )
                    """
                )
                _ensure_columns(
                    connection,
                    "scan_score_history",
                    {
                        "company_name": "TEXT",
                        "provider": "TEXT",
                        "raw_history_id": "INTEGER",
                        "scoring_model_version": "TEXT NOT NULL DEFAULT 'unknown'",
                        "primary_model": "TEXT",
                        "score": "REAL NOT NULL DEFAULT 0",
                        "risk_level": "TEXT",
                        "data_quality": "REAL",
                        "model_scores_json": "TEXT NOT NULL DEFAULT '{}'",
                        "model_components_json": "TEXT NOT NULL DEFAULT '{}'",
                        "model_rationales_json": "TEXT NOT NULL DEFAULT '{}'",
                        "model_confidence_json": "TEXT NOT NULL DEFAULT '{}'",
                        "metrics_json": "TEXT NOT NULL DEFAULT '{}'",
                        "risk_flags_json": "TEXT NOT NULL DEFAULT '[]'",
                        "warnings_json": "TEXT NOT NULL DEFAULT '[]'",
                        "created_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'",
                    },
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS price_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        provider TEXT NOT NULL DEFAULT 'manual',
                        observed_at TEXT NOT NULL,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL NOT NULL,
                        volume REAL,
                        UNIQUE(symbol, provider, observed_at)
                    )
                    """
                )
                _ensure_columns(
                    connection,
                    "price_history",
                    {
                        "provider": "TEXT NOT NULL DEFAULT 'manual'",
                        "observed_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'",
                        "open": "REAL",
                        "high": "REAL",
                        "low": "REAL",
                        "close": "REAL NOT NULL DEFAULT 0",
                        "volume": "REAL",
                    },
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS scan_outcomes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT NOT NULL,
                        scan_score_history_id INTEGER,
                        scoring_model_version TEXT NOT NULL,
                        model TEXT NOT NULL,
                        score_at_scan REAL,
                        horizon_label TEXT NOT NULL,
                        horizon_seconds INTEGER NOT NULL,
                        scan_created_at TEXT NOT NULL,
                        outcome_computed_at TEXT NOT NULL,
                        entry_price REAL NOT NULL,
                        entry_observed_at TEXT NOT NULL,
                        exit_price REAL NOT NULL,
                        exit_observed_at TEXT NOT NULL,
                        forward_return_pct REAL NOT NULL,
                        max_favorable_excursion_pct REAL,
                        max_adverse_excursion_pct REAL,
                        next_day_gap_pct REAL,
                        peak_volume_expansion REAL,
                        move_threshold_pct REAL,
                        hit_move_threshold INTEGER,
                        UNIQUE (
                            symbol,
                            scan_created_at,
                            scoring_model_version,
                            model,
                            horizon_seconds
                        )
                    )
                    """
                )
                _ensure_columns(
                    connection,
                    "scan_outcomes",
                    {
                        "scan_score_history_id": "INTEGER",
                        "scoring_model_version": "TEXT NOT NULL DEFAULT 'unknown'",
                        "model": "TEXT NOT NULL DEFAULT 'unknown'",
                        "score_at_scan": "REAL",
                        "horizon_label": "TEXT NOT NULL DEFAULT 'unknown'",
                        "horizon_seconds": "INTEGER NOT NULL DEFAULT 0",
                        "scan_created_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'",
                        "outcome_computed_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'",
                        "entry_price": "REAL NOT NULL DEFAULT 0",
                        "entry_observed_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'",
                        "exit_price": "REAL NOT NULL DEFAULT 0",
                        "exit_observed_at": "TEXT NOT NULL DEFAULT '1970-01-01T00:00:00+00:00'",
                        "forward_return_pct": "REAL NOT NULL DEFAULT 0",
                        "max_favorable_excursion_pct": "REAL",
                        "max_adverse_excursion_pct": "REAL",
                        "next_day_gap_pct": "REAL",
                        "peak_volume_expansion": "REAL",
                        "move_threshold_pct": "REAL",
                        "hit_move_threshold": "INTEGER",
                    },
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scan_score_history_symbol_created
                    ON scan_score_history (symbol, created_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scan_score_history_model_score
                    ON scan_score_history (primary_model, score)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scan_score_history_version_created
                    ON scan_score_history (scoring_model_version, created_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_price_history_symbol_observed
                    ON price_history (symbol, observed_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scan_outcomes_model_bucket
                    ON scan_outcomes (model, horizon_seconds, score_at_scan)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scan_outcomes_symbol_scan
                    ON scan_outcomes (symbol, scan_created_at)
                    """
                )
            self._schema_ready = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection


def rows_to_csv(rows: Sequence[Mapping[str, Any]], columns: Sequence[str] | None = None) -> str:
    selected_columns = list(columns) if columns is not None else _columns_from_rows(rows)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=selected_columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _csv_value(row.get(column)) for column in selected_columns})
    return buffer.getvalue()


def _price_bar_payload(bar: PriceBar | Mapping[str, Any]) -> dict[str, Any]:
    payload = asdict(bar) if isinstance(bar, PriceBar) else dict(bar)
    close = _positive_float(payload.get("close"))
    if close is None:
        raise ValueError("price_history close must be a positive number")
    return {
        "symbol": str(payload["symbol"]).upper(),
        "provider": str(payload.get("provider") or "manual"),
        "observed_at": _utc_iso(payload["observed_at"]),
        "open": _as_float(payload.get("open")),
        "high": _as_float(payload.get("high")),
        "low": _as_float(payload.get("low")),
        "close": close,
        "volume": _as_float(payload.get("volume")),
    }


def _score_row_payload(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    created_at = _coerce_datetime(payload.get("created_at"))
    score = _as_float(payload.get("score"), default=0.0) or 0.0
    return {
        **payload,
        "id": int(payload["id"]),
        "symbol": str(payload["symbol"]).upper(),
        "score": float(score),
        "created_at": _utc_iso(created_at),
        "created_at_dt": created_at,
        "scoring_model_version": str(payload.get("scoring_model_version") or "unknown"),
        "primary_model": payload.get("primary_model"),
        "company_name": payload.get("company_name"),
        "model_scores": _loads_json(payload.get("model_scores_json"), default={}),
        "model_components": _loads_json(payload.get("model_components_json"), default={}),
        "model_rationales": _loads_json(payload.get("model_rationales_json"), default={}),
        "model_confidence": _loads_json(payload.get("model_confidence_json"), default={}),
        "metrics": _loads_json(payload.get("metrics_json"), default={}),
        "risk_flags": _loads_json(payload.get("risk_flags_json"), default=[]),
        "warnings": _loads_json(payload.get("warnings_json"), default=[]),
    }


def _ensure_columns(connection: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, ddl in columns.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _normalize_horizons(
    horizons: Mapping[str, int] | Sequence[str | int | Horizon] | None,
) -> list[Horizon]:
    if horizons is None:
        return [Horizon(label, seconds) for label, seconds in DEFAULT_HORIZONS.items()]
    if isinstance(horizons, Mapping):
        items = horizons.items()
        return [Horizon(str(label), int(seconds)) for label, seconds in items]
    normalized: list[Horizon] = []
    for item in horizons:
        if isinstance(item, Horizon):
            normalized.append(item)
        elif isinstance(item, str):
            normalized.append(Horizon(item, _window_seconds(item)))
        else:
            seconds = int(item)
            normalized.append(Horizon(f"{seconds}s", seconds))
    return normalized


def _window_seconds(window: str) -> int:
    if window in DEFAULT_HORIZONS:
        return DEFAULT_HORIZONS[window]
    if window in DELTA_WINDOW_SECONDS:
        return DELTA_WINDOW_SECONDS[window]
    if window.endswith("h") and window[:-1].isdigit():
        return int(window[:-1]) * 60 * 60
    if window.endswith("d") and window[:-1].isdigit():
        return int(window[:-1]) * 24 * 60 * 60
    if window.endswith("s") and window[:-1].isdigit():
        return int(window[:-1])
    raise ValueError(f"Unsupported window or horizon: {window}")


def _coerce_datetime(value: datetime | str | int | float | None) -> datetime:
    if value is None:
        raise ValueError("timestamp value is required")
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), timezone.utc)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("timestamp value is required")
        if stripped.endswith("Z"):
            stripped = f"{stripped[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(stripped)
        except ValueError:
            dt = datetime.fromtimestamp(float(stripped), timezone.utc)
    else:
        raise TypeError(f"Unsupported timestamp type: {type(value)!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utc_iso(value: datetime | str | int | float) -> str:
    return _coerce_datetime(value).isoformat(timespec="seconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _loads_json(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if parsed is not None else default


def _coerce_json_object(value: Any) -> dict[str, Any]:
    parsed = _coerce_json_value(value, default={})
    return parsed if isinstance(parsed, dict) else {}


def _coerce_json_value(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        parsed = _loads_json(value, default=default)
        return parsed
    return value


def _as_float(value: Any, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool):
        return float(value)
    try:
        if math.isnan(float(value)):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _positive_float(value: Any) -> float | None:
    numeric = _as_float(value)
    if numeric is None or numeric <= 0:
        return None
    return numeric


def _round_number(value: float | int | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), digits)
    return _clean_number(rounded)


def _clean_number(value: float | int) -> float | int:
    numeric = float(value)
    if numeric.is_integer():
        return int(numeric)
    return numeric


def _pct_change(start: float, end: float) -> float:
    return (end - start) / start * 100.0


def _price_at_or_before(
    connection: sqlite3.Connection,
    symbol: str,
    observed_at: datetime,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT *
        FROM price_history
        WHERE symbol = ? AND observed_at <= ?
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
        """,
        (symbol, _utc_iso(observed_at)),
    ).fetchone()
    return dict(row) if row else None


def _first_price_at_or_after(
    connection: sqlite3.Connection,
    symbol: str,
    observed_at: datetime,
    as_of: datetime,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT *
        FROM price_history
        WHERE symbol = ?
          AND observed_at >= ?
          AND observed_at <= ?
        ORDER BY observed_at ASC, id ASC
        LIMIT 1
        """,
        (symbol, _utc_iso(observed_at), _utc_iso(as_of)),
    ).fetchone()
    return dict(row) if row else None


def _price_window(
    connection: sqlite3.Connection,
    symbol: str,
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM price_history
        WHERE symbol = ?
          AND observed_at > ?
          AND observed_at <= ?
        ORDER BY observed_at ASC, id ASC
        """,
        (symbol, _utc_iso(start_at), _utc_iso(end_at)),
    ).fetchall()
    return [dict(row) for row in rows]


def _excursions(
    entry_price: float,
    rows: Sequence[Mapping[str, Any]],
    *,
    fallback_return: float,
) -> tuple[float, float]:
    if not rows:
        return fallback_return, fallback_return
    favorable = fallback_return
    adverse = fallback_return
    for row in rows:
        high = _positive_float(row.get("high")) or _positive_float(row.get("close"))
        low = _positive_float(row.get("low")) or _positive_float(row.get("close"))
        if high is not None:
            favorable = max(favorable, _pct_change(entry_price, high))
        if low is not None:
            adverse = min(adverse, _pct_change(entry_price, low))
    return favorable, adverse


def _peak_volume_expansion(metrics: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> float | None:
    avg_volume = _positive_float(metrics.get("avg_volume_20d"))
    if avg_volume is None:
        return None
    expansions = [
        float(volume) / avg_volume
        for volume in (_positive_float(row.get("volume")) for row in rows)
        if volume is not None
    ]
    return max(expansions) if expansions else None


def _next_day_gap_pct(
    connection: sqlite3.Connection,
    symbol: str,
    scan_at: datetime,
    entry_price: float,
    as_of: datetime,
) -> float | None:
    next_day = (scan_at + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if next_day > as_of:
        return None
    row = connection.execute(
        """
        SELECT open, close
        FROM price_history
        WHERE symbol = ?
          AND observed_at >= ?
          AND observed_at <= ?
        ORDER BY observed_at ASC, id ASC
        LIMIT 1
        """,
        (symbol, _utc_iso(next_day), _utc_iso(as_of)),
    ).fetchone()
    if row is None:
        return None
    opening_price = _positive_float(row["open"]) or _positive_float(row["close"])
    return _pct_change(entry_price, opening_price) if opening_price is not None else None


def _outcome_exists(
    connection: sqlite3.Connection,
    score_row: Mapping[str, Any],
    model: str,
    horizon: Horizon,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM scan_outcomes
        WHERE symbol = ?
          AND scan_created_at = ?
          AND scoring_model_version = ?
          AND model = ?
          AND horizon_seconds = ?
        LIMIT 1
        """,
        (
            score_row["symbol"],
            score_row["created_at"],
            score_row["scoring_model_version"],
            model,
            horizon.seconds,
        ),
    ).fetchone()
    return row is not None


def _metrics_with_source_metadata(metrics: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(metrics)
    for metrics_key, payload_key in (
        ("_field_sources", "field_sources"),
        ("_field_quality", "field_quality"),
        ("_source_quality", "source_quality"),
    ):
        value = _coerce_json_object(payload.get(payload_key) or payload.get(f"{payload_key}_json"))
        if value and metrics_key not in merged:
            merged[metrics_key] = value
    return merged


def _outcome_source_context(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    metrics = _loads_json(payload.get("metrics_json"), default={})
    risk_flags = _loads_json(payload.get("risk_flags_json"), default=[])
    warnings = _loads_json(payload.get("warnings_json"), default=[])
    return {
        "provider": payload.get("provider"),
        "data_quality": _as_float(payload.get("data_quality")),
        "metrics": metrics if isinstance(metrics, Mapping) else {},
        "risk_flags": risk_flags,
        "warnings": warnings,
    }


def _normalize_source_slice(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def _source_quality_slices(context: Mapping[str, Any]) -> list[str]:
    metrics = context.get("metrics") if isinstance(context.get("metrics"), Mapping) else {}
    provider = str(context.get("provider") or "").lower()
    field_sources = _metadata_mapping(metrics, "_field_sources")
    field_quality = _metadata_mapping(metrics, "_field_quality")
    capabilities = metrics.get("option_chain_capabilities")
    capabilities = capabilities if isinstance(capabilities, Mapping) else {}
    option_provider = str(metrics.get("option_chain_provider") or "").lower()
    option_source = str(metrics.get("option_chain_source") or "").lower()
    risk_keys = _risk_flag_keys(context.get("risk_flags"))
    warnings = [str(warning).lower() for warning in context.get("warnings") or []]
    slices: set[str] = set()

    true_greeks = bool(capabilities.get("true_gamma_exposure")) or (
        bool(capabilities.get("gamma")) and option_provider and "yahoo" not in option_provider
    )
    if true_greeks:
        slices.add("true_options_greeks")

    borrow_sources = {
        str(field_sources.get(field_name) or "").lower()
        for field_name in (
            "borrow_fee_pct",
            "borrow_available_shares",
            "borrow_utilization_pct",
            "borrow_rebate_rate_pct",
        )
    }
    borrow_values_present = any(
        _as_float(metrics.get(field_name)) is not None
        for field_name in (
            "borrow_fee_pct",
            "borrow_available_shares",
            "borrow_utilization_pct",
            "borrow_rebate_rate_pct",
        )
    )
    if borrow_values_present or any(source and "yahoo" not in source for source in borrow_sources):
        slices.add("premium_borrow")

    if (
        any(str(status).lower() == "stale" for status in field_quality.values())
        or "stale_data" in risk_keys
        or any("stale" in warning for warning in warnings)
        or _option_chain_is_stale(metrics)
    ):
        slices.add("stale_data")

    data_quality = _as_float(context.get("data_quality"))
    missing_statuses = {"missing", "provider-error"}
    if (
        any(str(status).lower() in missing_statuses for status in field_quality.values())
        or any("missing" in key for key in risk_keys)
        or "low_data_quality" in risk_keys
        or any("missing" in warning for warning in warnings)
        or (data_quality is not None and data_quality < 100)
    ):
        slices.add("missing_data")

    has_premium_or_true = bool(slices & {"premium_borrow", "true_options_greeks"})
    yahoo_markers = (provider, option_provider, option_source, *(_metadata_mapping(metrics, "_source_quality").keys()))
    if not has_premium_or_true and any("yahoo" in str(marker).lower() for marker in yahoo_markers):
        slices.add("yahoo_only")

    return sorted(slices) if slices else ["unknown_source"]


def _metadata_mapping(metrics: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = metrics.get(key)
    if value is None and key.startswith("_"):
        value = metrics.get(key[1:])
    return dict(value) if isinstance(value, Mapping) else {}


def _risk_flag_keys(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return {str(key).lower() for key, enabled in value.items() if enabled}
    if isinstance(value, list):
        keys: set[str] = set()
        for item in value:
            if isinstance(item, Mapping):
                key = item.get("key") or item.get("name")
                if key:
                    keys.add(str(key).lower())
            else:
                keys.add(str(item).lower())
        return keys
    return set()


def _option_chain_is_stale(metrics: Mapping[str, Any]) -> bool:
    freshness = _as_float(metrics.get("option_chain_freshness_seconds"))
    stale_after = _as_float(metrics.get("option_chain_stale_after_seconds"))
    return freshness is not None and stale_after is not None and freshness > stale_after


def _normalize_gamma_metrics(metrics: Sequence[str] | str | None) -> list[str]:
    if metrics is None:
        return list(DEFAULT_GAMMA_THRESHOLD_METRICS)
    if isinstance(metrics, str):
        raw_items = metrics.replace(";", ",").split(",")
    else:
        raw_items = [str(metric) for metric in metrics]
    normalized = [item.strip() for item in raw_items if item.strip()]
    return normalized or list(DEFAULT_GAMMA_THRESHOLD_METRICS)


def _gamma_metric_value(metric: str, metrics: Mapping[str, Any]) -> float | None:
    if metric == "net_gamma_exposure_pct_market_cap":
        value = _first_metric_value(
            metrics,
            (
                "net_gamma_exposure_pct_market_cap",
                "net_gex_pct_market_cap",
                "net_gex_percent_market_cap",
            ),
        )
        return value if value is not None else _pct_of_market_cap(
            metrics.get("net_gamma_exposure"),
            metrics.get("market_cap"),
        )
    if metric == "absolute_gamma_exposure_pct_market_cap":
        value = _first_metric_value(
            metrics,
            (
                "absolute_gamma_exposure_pct_market_cap",
                "absolute_gex_pct_market_cap",
                "absolute_gex_percent_market_cap",
                "gamma_exposure_pct_market_cap",
            ),
        )
        return value if value is not None else _pct_of_market_cap(
            metrics.get("absolute_gamma_exposure"),
            metrics.get("market_cap"),
            absolute=True,
        )
    if metric == "call_wall_distance_pct":
        value = _first_metric_value(
            metrics,
            ("call_wall_distance_pct", "call_wall_distance_percent"),
        )
        return value if value is not None else _wall_distance_pct(metrics, "call_wall_strike")
    if metric == "put_wall_distance_pct":
        value = _first_metric_value(
            metrics,
            ("put_wall_distance_pct", "put_wall_distance_percent"),
        )
        return value if value is not None else _wall_distance_pct(metrics, "put_wall_strike")
    if metric == "open_interest_change":
        return _first_metric_value(
            metrics,
            (
                "open_interest_change",
                "open_interest_change_pct",
                "oi_change",
                "oi_change_pct",
                "net_open_interest_change",
                "option_open_interest_change",
                "option_open_interest_change_pct",
            ),
        )
    return _first_metric_value(metrics, (metric,))


def _first_metric_value(metrics: Mapping[str, Any], keys: Sequence[str]) -> float | None:
    for key in keys:
        value = _as_float(metrics.get(key))
        if value is not None:
            return value
    return None


def _pct_of_market_cap(value: Any, market_cap: Any, *, absolute: bool = False) -> float | None:
    numeric_value = _as_float(value)
    numeric_market_cap = _positive_float(market_cap)
    if numeric_value is None or numeric_market_cap is None:
        return None
    if absolute:
        numeric_value = abs(numeric_value)
    return numeric_value / numeric_market_cap * 100.0


def _wall_distance_pct(metrics: Mapping[str, Any], strike_key: str) -> float | None:
    strike = _positive_float(metrics.get(strike_key))
    price = _positive_float(metrics.get("price"))
    if strike is None or price is None:
        return None
    return _pct_change(price, strike)


def _value_bucket_start(value: float, bucket_size: float) -> float:
    return math.floor(float(value) / bucket_size) * bucket_size


def _outcome_deteriorated(
    forward_return_pct: Any,
    max_adverse_excursion_pct: Any,
    threshold_pct: float,
) -> bool:
    threshold = abs(float(threshold_pct))
    forward_return = _as_float(forward_return_pct)
    adverse = _as_float(max_adverse_excursion_pct)
    return (forward_return is not None and forward_return < -threshold) or (
        adverse is not None and adverse < -threshold
    )


def _bucket_start(score: float, bucket_size: float) -> float:
    if score >= 100.0:
        return max(0.0, 100.0 - bucket_size)
    return max(0.0, math.floor(score / bucket_size) * bucket_size)


def _bucket_label(start: float, end: float) -> str:
    return f"{_clean_number(start)}-{_clean_number(end)}"


def _mean(values: Sequence[float]) -> float | None:
    if not values:
        return None
    return _round_number(sum(values) / len(values))


def _baseline_for_delta(
    rows: Sequence[dict[str, Any]],
    latest: Mapping[str, Any],
    window: str,
) -> dict[str, Any] | None:
    if window == "previous":
        prior = [
            row
            for row in rows
            if (row["created_at_dt"], row["id"]) < (latest["created_at_dt"], latest["id"])
        ]
        return prior[-1] if prior else None
    seconds = _window_seconds(window)
    return _baseline_at_or_before(rows, latest["symbol"], latest["created_at_dt"], seconds)


def _baseline_at_or_before(
    rows: Sequence[dict[str, Any]],
    symbol: str,
    current_at: datetime,
    window_seconds: int,
) -> dict[str, Any] | None:
    target = current_at - timedelta(seconds=window_seconds)
    candidates = [row for row in rows if row["symbol"] == symbol and row["created_at_dt"] <= target]
    return candidates[-1] if candidates else None


def _delta_drivers(baseline: Mapping[str, Any], latest: Mapping[str, Any]) -> list[dict[str, Any]]:
    drivers: list[dict[str, Any]] = []
    drivers.extend(
        _numeric_dict_drivers(
            baseline.get("model_scores") or {},
            latest.get("model_scores") or {},
            driver_type="model_score",
        )
    )
    drivers.extend(
        _component_drivers(
            baseline.get("model_components") or {},
            latest.get("model_components") or {},
        )
    )
    drivers.extend(
        _numeric_dict_drivers(
            baseline.get("metrics") or {},
            latest.get("metrics") or {},
            driver_type="metric",
        )
    )
    drivers.extend(
        _numeric_dict_drivers(
            _flatten_confidence(baseline.get("model_confidence") or {}),
            _flatten_confidence(latest.get("model_confidence") or {}),
            driver_type="confidence",
        )
    )
    drivers.extend(_risk_flag_drivers(baseline.get("risk_flags"), latest.get("risk_flags")))

    drivers.sort(
        key=lambda driver: (
            -abs(float(driver.get("delta") or 0.0)),
            str(driver.get("type") or ""),
            str(driver.get("model") or ""),
            str(driver.get("name") or ""),
            str(driver.get("change") or ""),
        )
    )
    return drivers


def _numeric_dict_drivers(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    driver_type: str,
) -> list[dict[str, Any]]:
    drivers: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        before_value = _as_float(before.get(key))
        after_value = _as_float(after.get(key))
        if before_value is None or after_value is None:
            continue
        delta = after_value - before_value
        if delta == 0:
            continue
        driver = {
            "type": driver_type,
            "name": key,
            "before": _round_number(before_value),
            "after": _round_number(after_value),
            "delta": _round_number(delta),
            "message": _driver_message(driver_type, key, before_value, after_value, delta),
        }
        if driver_type == "model_score":
            driver["model"] = key
        drivers.append(driver)
    return drivers


def _component_drivers(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> list[dict[str, Any]]:
    drivers: list[dict[str, Any]] = []
    for model in sorted(set(before) | set(after)):
        before_components = before.get(model) if isinstance(before.get(model), Mapping) else {}
        after_components = after.get(model) if isinstance(after.get(model), Mapping) else {}
        for driver in _numeric_dict_drivers(
            before_components,
            after_components,
            driver_type="component",
        ):
            driver["model"] = model
            driver["message"] = _driver_message(
                "component",
                driver["name"],
                float(driver["before"]),
                float(driver["after"]),
                float(driver["delta"]),
                model=model,
            )
            drivers.append(driver)
    return drivers


def _flatten_confidence(confidence: Mapping[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in confidence.items():
        if isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                flattened[f"{key}.{nested_key}"] = nested_value
        else:
            flattened[str(key)] = value
    return flattened


def _risk_flag_drivers(before: Any, after: Any) -> list[dict[str, Any]]:
    before_set = _risk_flag_set(before)
    after_set = _risk_flag_set(after)
    drivers: list[dict[str, Any]] = []
    for flag in sorted(after_set - before_set):
        drivers.append(
            {
                "type": "risk_flag",
                "name": flag,
                "change": "added",
                "before": False,
                "after": True,
                "delta": 0,
                "message": f"Risk flag added: {flag}.",
            }
        )
    for flag in sorted(before_set - after_set):
        drivers.append(
            {
                "type": "risk_flag",
                "name": flag,
                "change": "removed",
                "before": True,
                "after": False,
                "delta": 0,
                "message": f"Risk flag removed: {flag}.",
            }
        )
    return drivers


def _risk_flag_set(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return {str(key) for key, enabled in value.items() if enabled}
    if isinstance(value, list):
        return {str(item) for item in value}
    return set()


def _driver_message(
    driver_type: str,
    name: str,
    before: float,
    after: float,
    delta: float,
    *,
    model: str | None = None,
) -> str:
    direction = "rose" if delta > 0 else "fell"
    before_value = _round_number(before)
    after_value = _round_number(after)
    if driver_type == "model_score":
        label = MODEL_LABELS.get(name, name)
        return f"{label} score {direction} from {before_value} to {after_value}."
    if driver_type == "component" and model:
        return f"{model}.{name} component {direction} from {before_value} to {after_value}."
    return f"{name} {direction} from {before_value} to {after_value}."


def _report_score(row: Mapping[str, Any], model: str | None) -> float:
    if model is None:
        return float(row["score"])
    model_scores = row.get("model_scores") if isinstance(row.get("model_scores"), Mapping) else {}
    score = _as_float(model_scores.get(model))
    return score if score is not None else -math.inf


def _report_row(row: Mapping[str, Any], *, score: float) -> dict[str, Any]:
    return {
        "score_history_id": row["id"],
        "symbol": row["symbol"],
        "company_name": row.get("company_name"),
        "created_at": row["created_at"],
        "score": _round_number(score),
        "primary_model": row.get("primary_model"),
        "risk_level": row.get("risk_level"),
        "scoring_model_version": row.get("scoring_model_version"),
    }


def _ranked(rows: Sequence[dict[str, Any]], *, limit: int, offset: int) -> list[dict[str, Any]]:
    page = list(rows)[max(0, offset) : max(0, offset) + max(0, limit)]
    for index, row in enumerate(page, start=max(0, offset) + 1):
        row["rank"] = index
    return page


def _columns_from_rows(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for column in row:
            if column not in seen:
                columns.append(column)
                seen.add(column)
    return columns


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return _json_dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else value
