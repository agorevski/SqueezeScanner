from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from dataclasses import MISSING, asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .config import DEFAULT_CACHE_TTL_SECONDS
from .domain import MarketDataProvider, TickerSnapshot
from .options import normalize_option_chain_records

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawSnapshotRecord:
    id: int
    provider: str
    symbol: str
    fetched_at: float
    scanned_at: float | None
    snapshot: TickerSnapshot


class CachedMarketDataProvider:
    """Caches raw market data snapshots without storing derived scanner scores."""

    def __init__(
        self,
        provider: MarketDataProvider,
        db_path: str | Path,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        provider_name: str = "yahoo_finance",
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than zero")

        self.provider = provider
        self.db_path = Path(db_path)
        self.ttl_seconds = ttl_seconds
        self.provider_name = provider_name
        self.clock = clock
        self._schema_lock = Lock()
        self._schema_ready = False
        self._provider_status_lock = Lock()
        self._last_provider_fetch: dict[str, Any] = {"status": "not_requested"}

    def fetch(self, symbol: str) -> TickerSnapshot:
        self._ensure_schema()
        now = self.clock()

        cached = self._read(symbol, now)
        if cached is not None:
            self._touch_scan(symbol, now)
            return cached

        provider_started_at = self.clock()
        monotonic_started_at = time.perf_counter()
        try:
            snapshot = self.provider.fetch(symbol)
        except Exception as exc:
            self._record_provider_fetch(
                symbol=symbol,
                started_at=provider_started_at,
                latency_ms=(time.perf_counter() - monotonic_started_at) * 1000,
                status="error",
                error=str(exc),
            )
            raise
        self._record_provider_fetch(
            symbol=symbol,
            started_at=provider_started_at,
            latency_ms=(time.perf_counter() - monotonic_started_at) * 1000,
            status="ok",
            error=None,
        )
        self._write(symbol, snapshot, now)
        return snapshot

    def recent_snapshots(self, max_age_seconds: int | None = None) -> list[TickerSnapshot]:
        self._ensure_schema()
        max_age = max_age_seconds if max_age_seconds is not None else self.ttl_seconds
        cutoff = self.clock() - max_age

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol, payload_json
                FROM market_data_cache
                WHERE provider = ? AND COALESCE(scanned_at, fetched_at) >= ?
                ORDER BY COALESCE(scanned_at, fetched_at) DESC, symbol ASC
                """,
                (self.provider_name, cutoff),
            ).fetchall()

        snapshots: list[TickerSnapshot] = []
        corrupt_symbols: list[str] = []
        for row in rows:
            try:
                snapshots.append(snapshot_from_json(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Ignoring corrupt cached market data for %s: %s", row["symbol"], exc)
                corrupt_symbols.append(row["symbol"])

        for symbol in corrupt_symbols:
            self.delete(symbol)

        return snapshots

    def scan_times(self, symbols: list[str]) -> dict[str, float]:
        self._ensure_schema()
        if not symbols:
            return {}

        placeholders = ", ".join("?" for _ in symbols)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT symbol, COALESCE(scanned_at, fetched_at) AS scanned_at
                FROM market_data_cache
                WHERE provider = ? AND symbol IN ({placeholders})
                """,
                (self.provider_name, *symbols),
            ).fetchall()

        return {row["symbol"]: float(row["scanned_at"]) for row in rows}

    def raw_history_references(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        self._ensure_schema()
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols:
            return {}

        placeholders = ", ".join("?" for _ in unique_symbols)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    cache.symbol,
                    cache.provider,
                    cache.fetched_at AS raw_fetched_at,
                    history.id AS raw_history_id
                FROM market_data_cache AS cache
                LEFT JOIN market_data_history AS history
                  ON history.provider = cache.provider
                 AND history.symbol = cache.symbol
                 AND history.fetched_at = cache.fetched_at
                WHERE cache.provider = ? AND cache.symbol IN ({placeholders})
                """,
                (self.provider_name, *unique_symbols),
            ).fetchall()

        return {
            row["symbol"]: {
                "provider": row["provider"],
                "raw_history_id": row["raw_history_id"],
                "raw_fetched_at": float(row["raw_fetched_at"]),
            }
            for row in rows
        }

    def historical_snapshots(
        self,
        *,
        symbols: list[str] | None = None,
        from_timestamp: float | None = None,
        to_timestamp: float | None = None,
        limit: int = 100,
    ) -> list[RawSnapshotRecord]:
        self._ensure_schema()
        clauses = ["provider = ?"]
        params: list[Any] = [self.provider_name]
        if symbols:
            unique_symbols = list(dict.fromkeys(symbols))
            placeholders = ", ".join("?" for _ in unique_symbols)
            clauses.append(f"symbol IN ({placeholders})")
            params.extend(unique_symbols)
        if from_timestamp is not None:
            clauses.append("fetched_at >= ?")
            params.append(float(from_timestamp))
        if to_timestamp is not None:
            clauses.append("fetched_at <= ?")
            params.append(float(to_timestamp))
        params.append(max(1, min(int(limit), 1_000)))

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, provider, symbol, fetched_at, scanned_at, payload_json
                FROM market_data_history
                WHERE {' AND '.join(clauses)}
                ORDER BY fetched_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        records: list[RawSnapshotRecord] = []
        for row in rows:
            try:
                snapshot = snapshot_from_json(row["payload_json"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("Ignoring corrupt historical market data for %s: %s", row["symbol"], exc)
                continue
            records.append(
                RawSnapshotRecord(
                    id=int(row["id"]),
                    provider=row["provider"],
                    symbol=row["symbol"],
                    fetched_at=float(row["fetched_at"]),
                    scanned_at=float(row["scanned_at"]) if row["scanned_at"] is not None else None,
                    snapshot=snapshot,
                )
            )
        return records

    def delete(self, symbol: str) -> bool:
        self._ensure_schema()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM market_data_cache WHERE provider = ? AND symbol = ?",
                (self.provider_name, symbol),
            )
            return cursor.rowcount > 0

    def status(self) -> dict[str, Any]:
        try:
            self._ensure_schema()
            now = self.clock()
            with self._connect() as connection:
                cache_row = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS total_rows,
                        SUM(CASE WHEN ? - fetched_at <= ? THEN 1 ELSE 0 END) AS fresh_rows,
                        SUM(CASE WHEN ? - fetched_at > ? THEN 1 ELSE 0 END) AS stale_rows,
                        MAX(fetched_at) AS latest_fetched_at,
                        MAX(scanned_at) AS latest_scanned_at
                    FROM market_data_cache
                    WHERE provider = ?
                    """,
                    (now, self.ttl_seconds, now, self.ttl_seconds, self.provider_name),
                ).fetchone()
                history_row = connection.execute(
                    """
                    SELECT COUNT(*) AS history_rows, MAX(fetched_at) AS latest_history_fetched_at
                    FROM market_data_history
                    WHERE provider = ?
                    """,
                    (self.provider_name,),
                ).fetchone()
        except Exception as exc:
            return {
                "status": "degraded",
                "provider": self._provider_status(),
                "database": {
                    "backend": "sqlite",
                    "accessible": False,
                    "error": str(exc),
                },
                "cache": {
                    "ttl_seconds": self.ttl_seconds,
                    "fresh_rows": 0,
                    "stale_rows": 0,
                    "total_rows": 0,
                },
            }

        latest_fetched_at = _optional_float(cache_row["latest_fetched_at"]) if cache_row is not None else None
        latest_scanned_at = _optional_float(cache_row["latest_scanned_at"]) if cache_row is not None else None
        return {
            "status": "ok",
            "provider": self._provider_status(),
            "database": {
                "backend": "sqlite",
                "accessible": True,
                "latest_rows": _row_int(cache_row, "total_rows"),
                "history_rows": _row_int(history_row, "history_rows"),
            },
            "cache": {
                "ttl_seconds": self.ttl_seconds,
                "total_rows": _row_int(cache_row, "total_rows"),
                "fresh_rows": _row_int(cache_row, "fresh_rows"),
                "stale_rows": _row_int(cache_row, "stale_rows"),
                "latest_fetched_at": _timestamp_to_iso(latest_fetched_at),
                "latest_scanned_at": _timestamp_to_iso(latest_scanned_at),
                "seconds_since_latest_fetch": round(now - latest_fetched_at, 3) if latest_fetched_at else None,
            },
        }

    def _read(self, symbol: str, now: float) -> TickerSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT fetched_at, payload_json
                FROM market_data_cache
                WHERE provider = ? AND symbol = ?
                """,
                (self.provider_name, symbol),
            ).fetchone()

        if row is None:
            return None

        fetched_at = float(row["fetched_at"])
        if now - fetched_at > self.ttl_seconds:
            return None

        try:
            return snapshot_from_json(row["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring corrupt cached market data for %s: %s", symbol, exc)
            self.delete(symbol)
            return None

    def _write(self, symbol: str, snapshot: TickerSnapshot, fetched_at: float) -> None:
        payload_json = json.dumps(asdict(snapshot), sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO market_data_cache (provider, symbol, fetched_at, scanned_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider, symbol) DO UPDATE SET
                    fetched_at = excluded.fetched_at,
                    scanned_at = excluded.scanned_at,
                    payload_json = excluded.payload_json
                """,
                (self.provider_name, symbol, fetched_at, fetched_at, payload_json),
            )
            self._write_history(connection, symbol, fetched_at, fetched_at, payload_json)

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
                    CREATE TABLE IF NOT EXISTS market_data_cache (
                        provider TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        fetched_at REAL NOT NULL,
                        scanned_at REAL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY (provider, symbol)
                    )
                    """
                )
                columns = {
                    row["name"]
                    for row in connection.execute("PRAGMA table_info(market_data_cache)").fetchall()
                }
                if "scanned_at" not in columns:
                    connection.execute("ALTER TABLE market_data_cache ADD COLUMN scanned_at REAL")
                    connection.execute(
                        "UPDATE market_data_cache SET scanned_at = fetched_at WHERE scanned_at IS NULL"
                    )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS market_data_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        provider TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        fetched_at REAL NOT NULL,
                        scanned_at REAL,
                        payload_json TEXT NOT NULL,
                        UNIQUE (provider, symbol, fetched_at)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO market_data_history (
                        provider,
                        symbol,
                        fetched_at,
                        scanned_at,
                        payload_json
                    )
                    SELECT
                        provider,
                        symbol,
                        fetched_at,
                        scanned_at,
                        payload_json
                    FROM market_data_cache
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_market_data_cache_fetched_at
                    ON market_data_cache (fetched_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_market_data_cache_scanned_at
                    ON market_data_cache (scanned_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_market_data_history_provider_symbol_fetched
                    ON market_data_history (provider, symbol, fetched_at)
                    """
                )

            self._schema_ready = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _touch_scan(self, symbol: str, scanned_at: float) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE market_data_cache
                SET scanned_at = ?
                WHERE provider = ? AND symbol = ?
                """,
                (scanned_at, self.provider_name, symbol),
            )

    def _write_history(
        self,
        connection: sqlite3.Connection,
        symbol: str,
        fetched_at: float,
        scanned_at: float,
        payload_json: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO market_data_history (
                provider,
                symbol,
                fetched_at,
                scanned_at,
                payload_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (self.provider_name, symbol, fetched_at, scanned_at, payload_json),
        )

    def _record_provider_fetch(
        self,
        *,
        symbol: str,
        started_at: float,
        latency_ms: float,
        status: str,
        error: str | None,
    ) -> None:
        with self._provider_status_lock:
            self._last_provider_fetch = {
                "provider": self.provider_name,
                "symbol": symbol,
                "status": status,
                "started_at": _timestamp_to_iso(started_at),
                "latency_ms": round(latency_ms, 3),
                "error": error,
            }

    def _provider_status(self) -> dict[str, Any]:
        with self._provider_status_lock:
            return {
                "name": self.provider_name,
                "last_fetch": dict(self._last_provider_fetch),
            }


def snapshot_from_json(payload_json: str) -> TickerSnapshot:
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError("cached payload must be a JSON object")

    snapshot_payload: dict[str, object] = {}
    for snapshot_field in fields(TickerSnapshot):
        if snapshot_field.name in payload:
            value = payload.get(snapshot_field.name)
        elif snapshot_field.default is not MISSING:
            value = snapshot_field.default
        elif snapshot_field.default_factory is not MISSING:
            value = snapshot_field.default_factory()
        else:
            value = None

        if value is None and snapshot_field.default_factory is not MISSING:
            value = snapshot_field.default_factory()
        snapshot_payload[snapshot_field.name] = value

    if not isinstance(snapshot_payload["symbol"], str) or not snapshot_payload["symbol"]:
        raise ValueError("cached payload is missing symbol")
    if not isinstance(snapshot_payload["source_warnings"], list):
        snapshot_payload["source_warnings"] = []
    if not isinstance(snapshot_payload["field_sources"], dict):
        snapshot_payload["field_sources"] = {}
    if not isinstance(snapshot_payload["field_quality"], dict):
        snapshot_payload["field_quality"] = {}
    if not isinstance(snapshot_payload["source_quality"], dict):
        snapshot_payload["source_quality"] = {}
    if not isinstance(snapshot_payload["option_chain_capabilities"], dict):
        snapshot_payload["option_chain_capabilities"] = {}
    else:
        snapshot_payload["option_chain_capabilities"] = {
            str(key): bool(value)
            for key, value in snapshot_payload["option_chain_capabilities"].items()
        }
    snapshot_payload["option_chain_records"] = normalize_option_chain_records(
        snapshot_payload.get("option_chain_records"),
        fallback_symbol=snapshot_payload["symbol"],
        provider=snapshot_payload.get("option_chain_provider"),
        source=snapshot_payload.get("option_chain_source"),
    )

    return TickerSnapshot(**snapshot_payload)


def _optional_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _row_int(row: sqlite3.Row | None, key: str) -> int:
    if row is None or row[key] is None:
        return 0
    return int(row[key])


def _timestamp_to_iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
