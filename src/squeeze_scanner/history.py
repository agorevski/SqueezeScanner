from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from .domain import ScanResult
from .scoring import SCORING_MODEL_VERSION

DEFAULT_HISTORY_LIMIT = 100
MAX_HISTORY_LIMIT = 1_000


@dataclass(frozen=True)
class ScoreHistoryWrite:
    row_id: int
    inserted: bool


class ScoreHistoryStore:
    """Persists derived scan scores separately from raw market-data cache logic."""

    def __init__(self, db_path: str | Path, clock: Any = time.time) -> None:
        self.db_path = Path(db_path)
        self.clock = clock
        self._schema_lock = Lock()
        self._schema_ready = False

    def record_scan_result(
        self,
        result: ScanResult,
        *,
        provider: str,
        raw_fetched_at: float,
        raw_history_id: int | None = None,
        scoring_model_version: str = SCORING_MODEL_VERSION,
        scan_run_id: str = "live",
        created_at: float | None = None,
    ) -> ScoreHistoryWrite:
        self._ensure_schema()
        created_at = self.clock() if created_at is None else created_at
        created_at_iso = format_history_timestamp(created_at)
        existing = self.find_score_row_id(
            provider=provider,
            symbol=result.symbol,
            raw_fetched_at=float(raw_fetched_at),
            scoring_model_version=scoring_model_version,
            scan_run_id=scan_run_id,
        )
        if existing is not None:
            return ScoreHistoryWrite(row_id=existing, inserted=False)

        payload = (
            result.symbol,
            result.company_name,
            provider,
            raw_history_id,
            float(raw_fetched_at),
            scoring_model_version,
            scan_run_id,
            result.primary_model,
            result.score,
            result.risk_level,
            result.data_quality,
            _to_json(result.model_scores),
            _to_json(result.model_components),
            _to_json(result.model_rationales),
            _to_json(result.model_confidence),
            _to_json(result.metrics),
            _to_json(result.risk_flags),
            _to_json(result.warnings),
            created_at_iso,
        )

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO scan_score_history (
                    symbol,
                    company_name,
                    provider,
                    raw_history_id,
                    raw_fetched_at,
                    scoring_model_version,
                    scan_run_id,
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
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                payload,
            )
            inserted = cursor.rowcount == 1
            row_id = int(cursor.lastrowid) if inserted else self._find_existing_row_id(
                connection,
                provider=provider,
                symbol=result.symbol,
                raw_fetched_at=float(raw_fetched_at),
                scoring_model_version=scoring_model_version,
                scan_run_id=scan_run_id,
            )

        return ScoreHistoryWrite(row_id=row_id, inserted=inserted)

    def find_score_row_id(
        self,
        *,
        provider: str,
        symbol: str,
        raw_fetched_at: float,
        scoring_model_version: str = SCORING_MODEL_VERSION,
        scan_run_id: str = "live",
    ) -> int | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id
                FROM scan_score_history
                WHERE provider = ?
                  AND symbol = ?
                  AND raw_fetched_at = ?
                  AND scoring_model_version = ?
                  AND scan_run_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (provider, symbol, float(raw_fetched_at), scoring_model_version, scan_run_id),
            ).fetchone()
        return int(row["id"]) if row is not None else None

    def get(self, row_id: int) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scan_score_history WHERE id = ?",
                (row_id,),
            ).fetchone()
        return _history_row_to_dict(row) if row is not None else None

    def history_for_symbol(
        self,
        symbol: str,
        *,
        from_timestamp: float | None = None,
        to_timestamp: float | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
        primary_model: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        risk_level: str | None = None,
        scoring_model_version: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.query_history(
            symbol=symbol,
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            limit=limit,
            primary_model=primary_model,
            min_score=min_score,
            max_score=max_score,
            risk_level=risk_level,
            scoring_model_version=scoring_model_version,
        )

    def query_history(
        self,
        *,
        symbol: str | None = None,
        from_timestamp: float | None = None,
        to_timestamp: float | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
        primary_model: str | None = None,
        min_score: float | None = None,
        max_score: float | None = None,
        risk_level: str | None = None,
        scoring_model_version: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        clauses: list[str] = []
        params: list[Any] = []

        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if from_timestamp is not None:
            clauses.append("raw_fetched_at >= ?")
            params.append(float(from_timestamp))
        if to_timestamp is not None:
            clauses.append("raw_fetched_at <= ?")
            params.append(float(to_timestamp))
        if primary_model is not None:
            clauses.append("primary_model = ?")
            params.append(primary_model)
        if min_score is not None:
            clauses.append("score >= ?")
            params.append(float(min_score))
        if max_score is not None:
            clauses.append("score <= ?")
            params.append(float(max_score))
        if risk_level is not None:
            clauses.append("risk_level = ?")
            params.append(risk_level)
        if scoring_model_version is not None:
            clauses.append("scoring_model_version = ?")
            params.append(scoring_model_version)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(_coerce_limit(limit))

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM scan_score_history
                {where_sql}
                ORDER BY raw_fetched_at DESC, created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        return [_history_row_to_dict(row) for row in rows]

    def score_deltas(
        self,
        *,
        symbol: str,
        current_score: float,
        current_row_id: int | None = None,
        current_raw_fetched_at: float | None = None,
        current_created_at: float | None = None,
        scoring_model_version: str = SCORING_MODEL_VERSION,
    ) -> dict[str, Any]:
        self._ensure_schema()
        with self._connect() as connection:
            current_row = None
            if current_row_id is not None:
                current_row = connection.execute(
                    "SELECT * FROM scan_score_history WHERE id = ?",
                    (current_row_id,),
                ).fetchone()
                if current_row is not None:
                    symbol = str(current_row["symbol"])
                    current_score = float(current_row["score"])
                    current_raw_fetched_at = float(current_row["raw_fetched_at"])
                    current_created_at = _timestamp_to_float(current_row["created_at"])
                    scoring_model_version = str(current_row["scoring_model_version"])

            previous_row = self._previous_score_row(
                connection,
                symbol=symbol,
                scoring_model_version=scoring_model_version,
                current_row_id=current_row_id,
                current_raw_fetched_at=current_raw_fetched_at,
                current_created_at=current_created_at,
            )
            day_row = self._day_score_row(
                connection,
                symbol=symbol,
                scoring_model_version=scoring_model_version,
                current_raw_fetched_at=current_raw_fetched_at,
            )

        previous_delta = _delta(current_score, previous_row)
        day_delta = _delta(current_score, day_row)
        if previous_delta is None and day_delta is None:
            status = "not_enough_history"
        elif previous_delta is None or day_delta is None:
            status = "partial_history"
        else:
            status = "ok"

        return {
            "previous_scan_delta": previous_delta,
            "previous_scan_score": _score(previous_row),
            "previous_scan_at": _row_time(previous_row),
            "delta_24h": day_delta,
            "delta_24h_score": _score(day_row),
            "delta_24h_at": _row_time(day_row),
            "score_delta_status": status,
        }

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
                        raw_fetched_at REAL NOT NULL DEFAULT 0,
                        scoring_model_version TEXT NOT NULL,
                        scan_run_id TEXT NOT NULL DEFAULT 'legacy',
                        primary_model TEXT,
                        score REAL NOT NULL,
                        risk_level TEXT,
                        data_quality REAL,
                        model_scores_json TEXT NOT NULL DEFAULT '{}',
                        model_components_json TEXT NOT NULL DEFAULT '{}',
                        model_rationales_json TEXT NOT NULL DEFAULT '{}',
                        model_confidence_json TEXT NOT NULL DEFAULT '{}',
                        metrics_json TEXT NOT NULL DEFAULT '{}',
                        risk_flags_json TEXT NOT NULL DEFAULT '[]',
                        warnings_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        UNIQUE (
                            provider,
                            symbol,
                            raw_fetched_at,
                            scoring_model_version,
                            scan_run_id
                        )
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
                        "raw_fetched_at": "REAL NOT NULL DEFAULT 0",
                        "scoring_model_version": "TEXT NOT NULL DEFAULT 'unknown'",
                        "scan_run_id": "TEXT NOT NULL DEFAULT 'legacy'",
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
                    CREATE INDEX IF NOT EXISTS idx_scan_score_history_symbol_created
                    ON scan_score_history (symbol, created_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scan_score_history_symbol_raw_time
                    ON scan_score_history (symbol, raw_fetched_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scan_score_history_primary_model_score
                    ON scan_score_history (primary_model, score)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scan_score_history_version_created
                    ON scan_score_history (scoring_model_version, created_at)
                    """
                )

            self._schema_ready = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _find_existing_row_id(
        self,
        connection: sqlite3.Connection,
        *,
        provider: str,
        symbol: str,
        raw_fetched_at: float,
        scoring_model_version: str,
        scan_run_id: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT id
            FROM scan_score_history
            WHERE provider = ?
              AND symbol = ?
              AND raw_fetched_at = ?
              AND scoring_model_version = ?
              AND scan_run_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (provider, symbol, raw_fetched_at, scoring_model_version, scan_run_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("scan score history insert was ignored but no existing row was found")
        return int(row["id"])

    def _previous_score_row(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        scoring_model_version: str,
        current_row_id: int | None,
        current_raw_fetched_at: float | None,
        current_created_at: float | None,
    ) -> sqlite3.Row | None:
        params: list[Any] = [symbol, scoring_model_version]
        clauses = ["symbol = ?", "scoring_model_version = ?"]
        if current_row_id is not None:
            clauses.append("id != ?")
            params.append(current_row_id)
        if current_raw_fetched_at is not None:
            clauses.append("raw_fetched_at <= ?")
            params.append(float(current_raw_fetched_at))

        rows = connection.execute(
            f"""
            SELECT *
            FROM scan_score_history
            WHERE {' AND '.join(clauses)}
            ORDER BY raw_fetched_at DESC, created_at DESC, id DESC
            LIMIT 5
            """,
            params,
        ).fetchall()
        for row in rows:
            if current_raw_fetched_at is None:
                return row
            row_raw_time = float(row["raw_fetched_at"])
            row_created = _timestamp_to_float(row["created_at"])
            if row_raw_time < current_raw_fetched_at:
                return row
            if current_created_at is not None and row_created < current_created_at:
                return row
        return None

    def _day_score_row(
        self,
        connection: sqlite3.Connection,
        *,
        symbol: str,
        scoring_model_version: str,
        current_raw_fetched_at: float | None,
    ) -> sqlite3.Row | None:
        if current_raw_fetched_at is None:
            return None
        target = float(current_raw_fetched_at) - 86_400
        return connection.execute(
            """
            SELECT *
            FROM scan_score_history
            WHERE symbol = ?
              AND scoring_model_version = ?
              AND raw_fetched_at <= ?
            ORDER BY raw_fetched_at DESC, created_at DESC, id DESC
            LIMIT 1
            """,
            (symbol, scoring_model_version, target),
        ).fetchone()


def parse_history_timestamp(value: str | int | float | None) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass

    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def format_history_timestamp(timestamp: str | int | float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(_timestamp_to_float(timestamp), timezone.utc).isoformat()


def _history_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "symbol": row["symbol"],
        "company_name": row["company_name"],
        "provider": row["provider"],
        "raw_history_id": row["raw_history_id"],
        "raw_fetched_at": format_history_timestamp(row["raw_fetched_at"]),
        "raw_fetched_at_timestamp": float(row["raw_fetched_at"]),
        "scoring_model_version": row["scoring_model_version"],
        "scan_run_id": row["scan_run_id"],
        "primary_model": row["primary_model"],
        "score": float(row["score"]),
        "risk_level": row["risk_level"],
        "data_quality": _optional_float(row["data_quality"]),
        "model_scores": _from_json(row["model_scores_json"], {}),
        "model_components": _from_json(row["model_components_json"], {}),
        "model_rationales": _from_json(row["model_rationales_json"], {}),
        "model_confidence": _from_json(row["model_confidence_json"], {}),
        "metrics": _from_json(row["metrics_json"], {}),
        "risk_flags": _from_json(row["risk_flags_json"], []),
        "warnings": _from_json(row["warnings_json"], []),
        "created_at": format_history_timestamp(row["created_at"]),
        "created_at_timestamp": _timestamp_to_float(row["created_at"]),
    }


def _to_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _from_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _ensure_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, ddl in columns.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _coerce_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_HISTORY_LIMIT))


def _timestamp_to_float(value: str | int | float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _delta(current_score: float, row: sqlite3.Row | None) -> float | None:
    if row is None:
        return None
    return round(float(current_score) - float(row["score"]), 1)


def _score(row: sqlite3.Row | None) -> float | None:
    return float(row["score"]) if row is not None else None


def _row_time(row: sqlite3.Row | None) -> str | None:
    if row is None:
        return None
    return format_history_timestamp(row["raw_fetched_at"])


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None
