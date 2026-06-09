from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict, fields
from pathlib import Path
from threading import Lock
from typing import Callable

from .config import DEFAULT_CACHE_TTL_SECONDS
from .domain import MarketDataProvider, TickerSnapshot

logger = logging.getLogger(__name__)


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


def snapshot_from_json(payload_json: str) -> TickerSnapshot:
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise ValueError("cached payload must be a JSON object")

    field_names = {field.name for field in fields(TickerSnapshot)}
    snapshot_payload = {name: payload.get(name) for name in field_names}
    if not isinstance(snapshot_payload["symbol"], str) or not snapshot_payload["symbol"]:
        raise ValueError("cached payload is missing symbol")
    if not isinstance(snapshot_payload["source_warnings"], list):
        snapshot_payload["source_warnings"] = []

    return TickerSnapshot(**snapshot_payload)

