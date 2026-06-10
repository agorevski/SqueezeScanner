from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from .domain import InvalidSymbolError

if TYPE_CHECKING:
    from .service import ScannerService

_UNSET = object()


class ScreenStoreError(ValueError):
    """Raised when saved screen or watchlist input cannot be persisted."""


class ScreenStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._schema_lock = Lock()
        self._schema_ready = False

    def list_screens(self) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, filters_json, created_at, updated_at
                FROM saved_screens
                ORDER BY updated_at DESC, name COLLATE NOCASE ASC
                """
            ).fetchall()
        return [_screen_from_row(row) for row in rows]

    def get_screen(self, screen_id: int) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, filters_json, created_at, updated_at
                FROM saved_screens
                WHERE id = ?
                """,
                (screen_id,),
            ).fetchone()
        return _screen_from_row(row) if row is not None else None

    def create_screen(self, name: str, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_schema()
        screen_name = _normalize_name(name, "Saved screen name")
        filters_payload = _normalize_filters(filters)
        now = _utc_now()

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO saved_screens (name, filters_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (_normalize_name(screen_name, "Saved screen name"), _json_dumps(filters_payload), now, now),
            )
            screen_id = int(cursor.lastrowid)

        created = self.get_screen(screen_id)
        if created is None:
            raise ScreenStoreError("Saved screen could not be created.")
        return created

    def update_screen(
        self,
        screen_id: int,
        *,
        name: str | None = None,
        filters: Mapping[str, Any] | object = _UNSET,
    ) -> dict[str, Any] | None:
        self._ensure_schema()
        updates: list[str] = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(_normalize_name(name, "Saved screen name"))
        if filters is not _UNSET:
            updates.append("filters_json = ?")
            params.append(_json_dumps(_normalize_filters(filters)))

        if not updates:
            return self.get_screen(screen_id)

        updates.append("updated_at = ?")
        params.extend([_utc_now(), screen_id])
        with self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE saved_screens
                SET {', '.join(updates)}
                WHERE id = ?
                """,
                params,
            )
            if cursor.rowcount == 0:
                return None
        return self.get_screen(screen_id)

    def delete_screen(self, screen_id: int) -> bool:
        self._ensure_schema()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM saved_screens WHERE id = ?", (screen_id,))
            return cursor.rowcount > 0

    def list_watchlists(self) -> list[dict[str, Any]]:
        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, created_at, updated_at
                FROM watchlists
                ORDER BY updated_at DESC, name COLLATE NOCASE ASC
                """
            ).fetchall()
            symbols = _watchlist_symbols_by_id(connection)
        return [_watchlist_from_row(row, symbols.get(int(row["id"]), [])) for row in rows]

    def get_watchlist(self, watchlist_id: int) -> dict[str, Any] | None:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, created_at, updated_at
                FROM watchlists
                WHERE id = ?
                """,
                (watchlist_id,),
            ).fetchone()
            if row is None:
                return None
            symbols = _watchlist_symbols(connection, watchlist_id)
        return _watchlist_from_row(row, symbols)

    def create_watchlist(
        self,
        name: str,
        symbols: str | Sequence[str] | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        watchlist_name = _normalize_name(name, "Watchlist name")
        normalized_symbols = _normalize_watchlist_symbols(symbols) if symbols is not None else []
        now = _utc_now()

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO watchlists (name, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (watchlist_name, now, now),
            )
            watchlist_id = int(cursor.lastrowid)
            _insert_watchlist_symbols(connection, watchlist_id, normalized_symbols, now)

        created = self.get_watchlist(watchlist_id)
        if created is None:
            raise ScreenStoreError("Watchlist could not be created.")
        return created

    def update_watchlist(self, watchlist_id: int, *, name: str | None = None) -> dict[str, Any] | None:
        self._ensure_schema()
        if name is None:
            return self.get_watchlist(watchlist_id)

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE watchlists
                SET name = ?, updated_at = ?
                WHERE id = ?
                """,
                (_normalize_name(name, "Watchlist name"), _utc_now(), watchlist_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_watchlist(watchlist_id)

    def delete_watchlist(self, watchlist_id: int) -> bool:
        self._ensure_schema()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM watchlists WHERE id = ?", (watchlist_id,))
            return cursor.rowcount > 0

    def list_watchlist_symbols(self, watchlist_id: int) -> list[str] | None:
        watchlist = self.get_watchlist(watchlist_id)
        if watchlist is None:
            return None
        return list(watchlist["symbols"])

    def add_symbols(self, watchlist_id: int, symbols: str | Sequence[str]) -> dict[str, Any] | None:
        self._ensure_schema()
        normalized_symbols = _normalize_watchlist_symbols(symbols)
        with self._connect() as connection:
            if not _watchlist_exists(connection, watchlist_id):
                return None
            now = _utc_now()
            _insert_watchlist_symbols(connection, watchlist_id, normalized_symbols, now)
            connection.execute(
                "UPDATE watchlists SET updated_at = ? WHERE id = ?",
                (now, watchlist_id),
            )
        return self.get_watchlist(watchlist_id)

    def remove_symbol(self, watchlist_id: int, symbol: str) -> bool | None:
        self._ensure_schema()
        normalized_symbol = _normalize_watchlist_symbols(symbol, max_symbols=1)[0]
        with self._connect() as connection:
            if not _watchlist_exists(connection, watchlist_id):
                return None
            cursor = connection.execute(
                """
                DELETE FROM watchlist_symbols
                WHERE watchlist_id = ? AND symbol = ?
                """,
                (watchlist_id, normalized_symbol),
            )
            if cursor.rowcount > 0:
                connection.execute(
                    "UPDATE watchlists SET updated_at = ? WHERE id = ?",
                    (_utc_now(), watchlist_id),
                )
            return cursor.rowcount > 0

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
                    CREATE TABLE IF NOT EXISTS saved_screens (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        filters_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS watchlists (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS watchlist_symbols (
                        watchlist_id INTEGER NOT NULL,
                        symbol TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (watchlist_id, symbol),
                        FOREIGN KEY (watchlist_id) REFERENCES watchlists(id) ON DELETE CASCADE
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_watchlist_symbols_symbol
                    ON watchlist_symbols (symbol)
                    """
                )
            self._schema_ready = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def scan_watchlist(
    store: ScreenStore,
    scanner: "ScannerService",
    watchlist_id: int,
    *,
    ranking_mode: str | None = None,
    selected_model: str | None = None,
    sort_direction: str | None = None,
) -> dict[str, Any] | None:
    symbols = store.list_watchlist_symbols(watchlist_id)
    if symbols is None:
        return None
    if not symbols:
        from .service import build_scan_response

        payload = build_scan_response(
            [],
            ranking_mode=ranking_mode,
            selected_model=selected_model,
            sort_direction=sort_direction,
        )
    else:
        payload = scanner.scan(
            symbols,
            max_symbols=max(len(symbols), 1),
            ranking_mode=ranking_mode,
            selected_model=selected_model,
            sort_direction=sort_direction,
        )
    payload["watchlist_id"] = watchlist_id
    payload["symbols"] = symbols
    return payload


def _screen_from_row(row: sqlite3.Row) -> dict[str, Any]:
    filters = json.loads(row["filters_json"])
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "filters": filters,
        "filters_json": filters,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _watchlist_from_row(row: sqlite3.Row, symbols: list[str]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "symbols": symbols,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _watchlist_symbols_by_id(connection: sqlite3.Connection) -> dict[int, list[str]]:
    rows = connection.execute(
        """
        SELECT watchlist_id, symbol
        FROM watchlist_symbols
        ORDER BY symbol ASC
        """
    ).fetchall()
    symbols_by_id: dict[int, list[str]] = {}
    for row in rows:
        symbols_by_id.setdefault(int(row["watchlist_id"]), []).append(row["symbol"])
    return symbols_by_id


def _watchlist_symbols(connection: sqlite3.Connection, watchlist_id: int) -> list[str]:
    rows = connection.execute(
        """
        SELECT symbol
        FROM watchlist_symbols
        WHERE watchlist_id = ?
        ORDER BY symbol ASC
        """,
        (watchlist_id,),
    ).fetchall()
    return [row["symbol"] for row in rows]


def _watchlist_exists(connection: sqlite3.Connection, watchlist_id: int) -> bool:
    row = connection.execute("SELECT 1 FROM watchlists WHERE id = ?", (watchlist_id,)).fetchone()
    return row is not None


def _insert_watchlist_symbols(
    connection: sqlite3.Connection,
    watchlist_id: int,
    symbols: Sequence[str],
    created_at: str,
) -> None:
    connection.executemany(
        """
        INSERT OR IGNORE INTO watchlist_symbols (watchlist_id, symbol, created_at)
        VALUES (?, ?, ?)
        """,
        [(watchlist_id, symbol, created_at) for symbol in symbols],
    )


def _normalize_filters(filters: Mapping[str, Any] | object | None) -> dict[str, Any]:
    if filters is None:
        return {}
    if not isinstance(filters, Mapping):
        raise ScreenStoreError("Saved screen filters must be a JSON object.")
    try:
        return json.loads(json.dumps(dict(filters), sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ScreenStoreError("Saved screen filters must be JSON serializable.") from exc


def _normalize_name(name: str, label: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ScreenStoreError(f"{label} is required.")
    return normalized


def _normalize_watchlist_symbols(
    symbols: str | Sequence[str],
    max_symbols: int = 500,
) -> list[str]:
    from .service import normalize_symbols

    try:
        return normalize_symbols(symbols, max_symbols=max_symbols)
    except InvalidSymbolError:
        raise


def _json_dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
