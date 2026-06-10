from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import MISSING, asdict, dataclass, fields
from pathlib import Path
from threading import Lock
from typing import Any, Callable

from .config import DEFAULT_CACHE_TTL_SECONDS
from .domain import MarketDataProvider, TickerSnapshot

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

    def fetch(self, symbol: str) -> TickerSnapshot:
        self._ensure_schema()
        now = self.clock()

        cached = self._read(symbol, now)
        if cached is not None:
            self._touch_scan(symbol, now)
            return cached

        snapshot = self.provider.fetch(symbol)
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

    return TickerSnapshot(**snapshot_payload)
