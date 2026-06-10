from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .alert_delivery import AlertDeliveryOutcome, AlertDeliveryService, normalize_delivery_channels
from .domain import InvalidSymbolError, ScreenerError
from .service import normalize_symbols

logger = logging.getLogger(__name__)

VALID_TARGET_TYPES = {"saved_screen", "watchlist", "yahoo_most_shorted", "symbols"}
MAX_SCHEDULE_SYMBOLS = 250
DEFAULT_SCHEDULER_POLL_SECONDS = 30
_UNSET = object()


class AutomationError(RuntimeError):
    """Base error for scheduled scan and alert automation failures."""


class ScheduleNotFoundError(AutomationError):
    """Raised when a scheduled scan does not exist."""


class AlertNotFoundError(AutomationError):
    """Raised when an alert rule or event does not exist."""


class ScheduleTargetError(AutomationError):
    """Raised when a scheduled target cannot be resolved to symbols."""


@dataclass(frozen=True)
class AlertEvaluation:
    rule_type: str
    condition_key: str
    active: bool
    value: float
    threshold: float
    message: str
    previous_value: float | None = None


class AutomationService:
    """SQLite-backed scheduled scan and alert service."""

    def __init__(
        self,
        db_path: str | Path,
        scanner: Any,
        yahoo_screener: Any | None = None,
        alert_delivery: AlertDeliveryService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.scanner = scanner
        self.yahoo_screener = yahoo_screener
        self.alert_delivery = alert_delivery
        self.clock = clock or _utc_now
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def create_schedule(
        self,
        name: str,
        target_type: str,
        target: Mapping[str, Any] | Sequence[str] | str | None,
        interval_seconds: int,
        enabled: bool = True,
        next_run_at: datetime | str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        name = _require_name(name, "Schedule name is required.")
        target_type = _normalize_target_type(target_type)
        interval_seconds = _validate_interval(interval_seconds)
        target_payload = _normalize_target_payload(target_type, target)
        normalized_owner_id = _normalize_owner_id(owner_id)
        now = self.clock()
        next_run = _coerce_datetime(next_run_at) if next_run_at is not None else now + timedelta(seconds=interval_seconds)

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scheduled_scans (
                    owner_id,
                    name,
                    target_type,
                    target_json,
                    interval_seconds,
                    enabled,
                    last_run_at,
                    next_run_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                """,
                (
                    normalized_owner_id,
                    name,
                    target_type,
                    _json_dumps(target_payload),
                    interval_seconds,
                    int(enabled),
                    _datetime_to_json(next_run) if enabled else None,
                    _datetime_to_json(now),
                    _datetime_to_json(now),
                ),
            )
            schedule_id = int(cursor.lastrowid)
        return self.get_schedule(schedule_id)

    def list_schedules(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        self._ensure_schema()
        normalized_owner_id = _normalize_owner_id(owner_id)
        where = "WHERE owner_id = ?" if normalized_owner_id is not None else ""
        params: tuple[Any, ...] = (normalized_owner_id,) if normalized_owner_id is not None else ()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM scheduled_scans
                {where}
                ORDER BY enabled DESC, next_run_at IS NULL, next_run_at ASC, id ASC
                """,
                params,
            ).fetchall()
        return [_schedule_from_row(row) for row in rows]

    def get_schedule(self, schedule_id: int, owner_id: str | None = None) -> dict[str, Any]:
        self._ensure_schema()
        normalized_owner_id = _normalize_owner_id(owner_id)
        owner_clause = "AND owner_id = ?" if normalized_owner_id is not None else ""
        params: tuple[Any, ...] = (
            (schedule_id, normalized_owner_id) if normalized_owner_id is not None else (schedule_id,)
        )
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM scheduled_scans WHERE id = ? {owner_clause}",
                params,
            ).fetchone()
        if row is None:
            raise ScheduleNotFoundError(f"Scheduled scan {schedule_id} was not found.")
        return _schedule_from_row(row)

    def update_schedule(
        self,
        schedule_id: int,
        *,
        name: str | None = None,
        target_type: str | None = None,
        target: Mapping[str, Any] | Sequence[str] | str | None = None,
        interval_seconds: int | None = None,
        enabled: bool | None = None,
        next_run_at: datetime | str | None = None,
        owner_id: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        existing = self.get_schedule(schedule_id)
        updated_name = existing["name"] if name is None else _require_name(name, "Schedule name is required.")
        updated_type = existing["target_type"] if target_type is None else _normalize_target_type(target_type)
        updated_interval = existing["interval_seconds"] if interval_seconds is None else _validate_interval(interval_seconds)
        updated_enabled = existing["enabled"] if enabled is None else bool(enabled)
        updated_target = existing["target"] if target is None else _normalize_target_payload(updated_type, target)
        updated_owner_id = existing.get("owner_id") if owner_id is _UNSET else _normalize_owner_id(owner_id)
        if target_type is not None and target is None:
            updated_target = _normalize_target_payload(updated_type, existing["target"])

        if next_run_at is None:
            if updated_enabled:
                existing_next_run = _coerce_datetime(existing.get("next_run_at"))
                updated_next_run = existing_next_run or self.clock() + timedelta(seconds=updated_interval)
            else:
                updated_next_run = None
        else:
            updated_next_run = _coerce_datetime(next_run_at)

        now = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE scheduled_scans
                SET owner_id = ?,
                    name = ?,
                    target_type = ?,
                    target_json = ?,
                    interval_seconds = ?,
                    enabled = ?,
                    next_run_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    updated_owner_id,
                    updated_name,
                    updated_type,
                    _json_dumps(updated_target),
                    updated_interval,
                    int(updated_enabled),
                    _datetime_to_json(updated_next_run),
                    _datetime_to_json(now),
                    schedule_id,
                ),
            )
            if cursor.rowcount == 0:
                raise ScheduleNotFoundError(f"Scheduled scan {schedule_id} was not found.")
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: int, owner_id: str | None = None) -> bool:
        self._ensure_schema()
        normalized_owner_id = _normalize_owner_id(owner_id)
        owner_clause = "AND owner_id = ?" if normalized_owner_id is not None else ""
        params: tuple[Any, ...] = (
            (schedule_id, normalized_owner_id) if normalized_owner_id is not None else (schedule_id,)
        )
        with self._connect() as connection:
            cursor = connection.execute(f"DELETE FROM scheduled_scans WHERE id = ? {owner_clause}", params)
        return cursor.rowcount > 0

    def run_scheduled_scan(self, schedule_id: int) -> dict[str, Any]:
        schedule = self.get_schedule(schedule_id)
        started_at = self.clock()
        run_id = self._create_run(schedule_id, started_at)
        symbols: list[str] = []
        errors: list[dict[str, str]] = []
        response: dict[str, Any] | None = None
        alert_events: list[dict[str, Any]] = []
        status = "failure"
        error_message: str | None = None

        try:
            symbols = self.resolve_target_symbols(schedule)
            max_symbols = _target_max_symbols(schedule["target"], symbols)
            response = self.scanner.scan(symbols, max_symbols=max_symbols)
            if not isinstance(response, Mapping):
                raise AutomationError("Scanner returned an invalid response.")

            response_errors = _coerce_error_list(response.get("errors"))
            errors.extend(response_errors)
            results = _coerce_result_list(response.get("results"))
            result_count = _result_count(response, results)
            status = _run_status(result_count, errors)
            alert_events = self.process_alerts(results, scan_run_id=run_id, owner_id=schedule.get("owner_id"))
        except Exception as exc:
            error_message = str(exc)
            errors.append({"symbol": "*", "message": error_message})
            result_count = 0
            status = "failure"
            logger.warning("Scheduled scan %s failed: %s", schedule_id, exc)

        finished_at = self.clock()
        self._finish_run(
            run_id,
            finished_at=finished_at,
            status=status,
            symbols=symbols,
            errors=errors,
            result_count=result_count,
            response=response,
            error_message=error_message,
        )
        self._update_schedule_after_run(schedule, finished_at)
        run = self.get_run(run_id)
        run["alert_events"] = alert_events
        return run

    def run_due_schedules(self, limit: int | None = None) -> list[dict[str, Any]]:
        self._ensure_schema()
        now_json = _datetime_to_json(self.clock())
        sql = """
            SELECT *
            FROM scheduled_scans
            WHERE enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?)
            ORDER BY next_run_at IS NULL, next_run_at ASC, id ASC
        """
        params: tuple[Any, ...] = (now_json,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (now_json, int(limit))

        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()

        return [self.run_scheduled_scan(int(row["id"])) for row in rows]

    def list_runs(self, schedule_id: int | None = None, limit: int = 50) -> list[dict[str, Any]]:
        self._ensure_schema()
        limit = max(1, min(int(limit), 500))
        if schedule_id is None:
            sql = """
                SELECT *
                FROM scheduled_scan_runs
                ORDER BY started_at DESC, id DESC
                LIMIT ?
            """
            params: tuple[Any, ...] = (limit,)
        else:
            sql = """
                SELECT *
                FROM scheduled_scan_runs
                WHERE scheduled_scan_id = ?
                ORDER BY started_at DESC, id DESC
                LIMIT ?
            """
            params = (schedule_id, limit)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_run_from_row(row) for row in rows]

    def get_run(self, run_id: int) -> dict[str, Any]:
        self._ensure_schema()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scheduled_scan_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise AutomationError(f"Scheduled scan run {run_id} was not found.")
        return _run_from_row(row)

    def resolve_target_symbols(self, schedule: Mapping[str, Any]) -> list[str]:
        target_type = str(schedule["target_type"])
        target = schedule.get("target")
        if not isinstance(target, Mapping):
            target = _loads_json_object(str(schedule.get("target_json", "{}")))

        if target_type == "symbols":
            return _normalize_symbols_from_target(target)
        if target_type == "yahoo_most_shorted":
            return self._yahoo_most_shorted_symbols(target)
        if target_type == "watchlist":
            return self._watchlist_symbols(target)
        if target_type == "saved_screen":
            return self._saved_screen_symbols(target)
        raise ScheduleTargetError(f"Unsupported scheduled target type: {target_type}")

    def create_alert(
        self,
        name: str,
        rule: Mapping[str, Any],
        enabled: bool = True,
        delivery_channels: Sequence[str] | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_schema()
        name = _require_name(name, "Alert name is required.")
        normalized_rule = _normalize_alert_rule(rule)
        normalized_channels = self._normalize_alert_delivery_channels(delivery_channels)
        normalized_owner_id = _normalize_owner_id(owner_id)
        now = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO alerts (owner_id, name, rule_json, enabled, delivery_channels_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_owner_id,
                    name,
                    _json_dumps(normalized_rule),
                    int(enabled),
                    _json_dumps(normalized_channels),
                    _datetime_to_json(now),
                    _datetime_to_json(now),
                ),
            )
            alert_id = int(cursor.lastrowid)
        return self.get_alert(alert_id)

    def list_alerts(self, enabled_only: bool = False, owner_id: str | None = None) -> list[dict[str, Any]]:
        self._ensure_schema()
        normalized_owner_id = _normalize_owner_id(owner_id)
        sql = "SELECT * FROM alerts"
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if normalized_owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(normalized_owner_id)
        if clauses:
            sql += f" WHERE {' AND '.join(clauses)}"
        sql += " ORDER BY enabled DESC, name ASC, id ASC"
        with self._connect() as connection:
            rows = connection.execute(sql, tuple(params)).fetchall()
        return [_alert_from_row(row) for row in rows]

    def get_alert(self, alert_id: int, owner_id: str | None = None) -> dict[str, Any]:
        self._ensure_schema()
        normalized_owner_id = _normalize_owner_id(owner_id)
        owner_clause = "AND owner_id = ?" if normalized_owner_id is not None else ""
        params: tuple[Any, ...] = (alert_id, normalized_owner_id) if normalized_owner_id is not None else (alert_id,)
        with self._connect() as connection:
            row = connection.execute(f"SELECT * FROM alerts WHERE id = ? {owner_clause}", params).fetchone()
        if row is None:
            raise AlertNotFoundError(f"Alert {alert_id} was not found.")
        return _alert_from_row(row)

    def update_alert(
        self,
        alert_id: int,
        *,
        name: str | None = None,
        rule: Mapping[str, Any] | None = None,
        enabled: bool | None = None,
        delivery_channels: Sequence[str] | None = None,
        owner_id: str | None | object = _UNSET,
    ) -> dict[str, Any]:
        existing = self.get_alert(alert_id)
        updated_name = existing["name"] if name is None else _require_name(name, "Alert name is required.")
        updated_rule = existing["rule"] if rule is None else _normalize_alert_rule(rule)
        updated_enabled = existing["enabled"] if enabled is None else bool(enabled)
        updated_owner_id = existing.get("owner_id") if owner_id is _UNSET else _normalize_owner_id(owner_id)
        updated_channels = (
            existing["delivery_channels"]
            if delivery_channels is None
            else normalize_delivery_channels(delivery_channels)
        )
        now = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE alerts
                SET owner_id = ?, name = ?, rule_json = ?, enabled = ?, delivery_channels_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    updated_owner_id,
                    updated_name,
                    _json_dumps(updated_rule),
                    int(updated_enabled),
                    _json_dumps(updated_channels),
                    _datetime_to_json(now),
                    alert_id,
                ),
            )
            if cursor.rowcount == 0:
                raise AlertNotFoundError(f"Alert {alert_id} was not found.")
        return self.get_alert(alert_id)

    def delete_alert(self, alert_id: int, owner_id: str | None = None) -> bool:
        self._ensure_schema()
        normalized_owner_id = _normalize_owner_id(owner_id)
        owner_clause = "AND owner_id = ?" if normalized_owner_id is not None else ""
        params: tuple[Any, ...] = (alert_id, normalized_owner_id) if normalized_owner_id is not None else (alert_id,)
        with self._connect() as connection:
            cursor = connection.execute(f"DELETE FROM alerts WHERE id = ? {owner_clause}", params)
        return cursor.rowcount > 0

    def process_alerts(
        self,
        results: Sequence[Mapping[str, Any]],
        scan_run_id: int | None = None,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        alerts = self.list_alerts(enabled_only=True, owner_id=owner_id)
        new_events: list[dict[str, Any]] = []

        for result in results:
            symbol = str(result.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            for alert in alerts:
                evaluation = _evaluate_alert(alert["rule"], result, symbol)
                if evaluation is None:
                    continue
                if evaluation.active:
                    event = self._create_alert_event(alert, result, evaluation, scan_run_id)
                    if event is not None:
                        new_events.append(event)
                else:
                    self._clear_alert_event(alert["id"], symbol, evaluation.condition_key)

        return new_events

    def list_alert_events(
        self,
        alert_id: int | None = None,
        symbol: str | None = None,
        active_only: bool = False,
        limit: int = 100,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        normalized_owner_id = _normalize_owner_id(owner_id)
        clauses: list[str] = []
        params: list[Any] = []
        if alert_id is not None:
            clauses.append("alert_id = ?")
            params.append(alert_id)
        if normalized_owner_id is not None:
            clauses.append("alert_id IN (SELECT id FROM alerts WHERE owner_id = ?)")
            params.append(normalized_owner_id)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.strip().upper())
        if active_only:
            clauses.append("cleared_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM alert_events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        events = [_event_from_row(row) for row in rows]
        self._attach_delivery_attempts(events)
        return events

    def acknowledge_alert_event(self, event_id: int) -> dict[str, Any]:
        self._ensure_schema()
        now = self.clock()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE alert_events
                SET acknowledged_at = COALESCE(acknowledged_at, ?)
                WHERE id = ?
                """,
                (_datetime_to_json(now), event_id),
            )
            if cursor.rowcount == 0:
                raise AlertNotFoundError(f"Alert event {event_id} was not found.")
        return self._get_alert_event(event_id)

    def list_alert_delivery_attempts(
        self,
        alert_event_id: int | None = None,
        alert_id: int | None = None,
        status: str | None = None,
        limit: int = 100,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._ensure_schema()
        normalized_owner_id = _normalize_owner_id(owner_id)
        clauses: list[str] = []
        params: list[Any] = []
        if alert_event_id is not None:
            clauses.append("alert_event_id = ?")
            params.append(alert_event_id)
        if alert_id is not None:
            clauses.append("alert_id = ?")
            params.append(alert_id)
        if normalized_owner_id is not None:
            clauses.append("alert_id IN (SELECT id FROM alerts WHERE owner_id = ?)")
            params.append(normalized_owner_id)
        if status:
            clauses.append("status = ?")
            params.append(status.strip().lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM alert_delivery_attempts
                {where}
                ORDER BY last_attempted_at DESC, id DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return [_delivery_attempt_from_row(row) for row in rows]

    def retry_alert_delivery_attempt(self, attempt_id: int, owner_id: str | None = None) -> dict[str, Any]:
        self._ensure_schema()
        attempt = self._get_alert_delivery_attempt(attempt_id)
        event = self._get_alert_event(int(attempt["alert_event_id"]), include_delivery_attempts=False)
        alert = self.get_alert(int(attempt["alert_id"]), owner_id=owner_id)
        outcomes = self._deliver_alert_event(
            alert,
            event,
            [str(attempt["channel"])],
            retry_attempt_id=attempt_id,
        )
        return outcomes[0] if outcomes else self._get_alert_delivery_attempt(attempt_id)

    def status(self) -> dict[str, Any]:
        try:
            self._ensure_schema()
            now = self.clock()
            now_json = _datetime_to_json(now)
            day_ago_json = _datetime_to_json(now - timedelta(days=1))
            with self._connect() as connection:
                schedules = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled,
                        SUM(CASE WHEN enabled = 1 AND (next_run_at IS NULL OR next_run_at <= ?) THEN 1 ELSE 0 END) AS due
                    FROM scheduled_scans
                    """,
                    (now_json,),
                ).fetchone()
                runs = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running,
                        SUM(CASE WHEN status = 'failure' AND started_at >= ? THEN 1 ELSE 0 END) AS failures_24h
                    FROM scheduled_scan_runs
                    """,
                    (day_ago_json,),
                ).fetchone()
                last_run = connection.execute(
                    """
                    SELECT id, scheduled_scan_id, status, started_at, finished_at, error_message
                    FROM scheduled_scan_runs
                    ORDER BY started_at DESC, id DESC
                    LIMIT 1
                    """
                ).fetchone()
                alerts = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled
                    FROM alerts
                    """
                ).fetchone()
                active_events = connection.execute(
                    "SELECT COUNT(*) AS total FROM alert_events WHERE cleared_at IS NULL"
                ).fetchone()
                delivery = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN status = 'failure' THEN 1 ELSE 0 END) AS failures
                    FROM alert_delivery_attempts
                    """
                ).fetchone()
        except Exception as exc:
            return {
                "status": "degraded",
                "error": str(exc),
            }

        return {
            "status": "ok",
            "schedules": {
                "total": _row_int(schedules, "total"),
                "enabled": _row_int(schedules, "enabled"),
                "due": _row_int(schedules, "due"),
            },
            "runs": {
                "total": _row_int(runs, "total"),
                "running": _row_int(runs, "running"),
                "failures_24h": _row_int(runs, "failures_24h"),
                "last": _run_status_row(last_run),
            },
            "alerts": {
                "total": _row_int(alerts, "total"),
                "enabled": _row_int(alerts, "enabled"),
                "active_events": _row_int(active_events, "total"),
            },
            "delivery": {
                "enabled": self.alert_delivery is not None,
                "attempts": _row_int(delivery, "total"),
                "failures": _row_int(delivery, "failures"),
            },
        }

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS scheduled_scans (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        owner_id TEXT,
                        name TEXT NOT NULL,
                        target_type TEXT NOT NULL,
                        target_json TEXT NOT NULL,
                        interval_seconds INTEGER NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        last_run_at TEXT,
                        next_run_at TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS scheduled_scan_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scheduled_scan_id INTEGER NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        status TEXT NOT NULL,
                        symbols_scanned_json TEXT NOT NULL DEFAULT '[]',
                        errors_json TEXT NOT NULL DEFAULT '[]',
                        result_count INTEGER NOT NULL DEFAULT 0,
                        response_json TEXT,
                        error_message TEXT,
                        FOREIGN KEY (scheduled_scan_id) REFERENCES scheduled_scans(id) ON DELETE CASCADE
                    );

                    CREATE TABLE IF NOT EXISTS alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        owner_id TEXT,
                        name TEXT NOT NULL,
                        rule_json TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        delivery_channels_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS alert_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        alert_id INTEGER NOT NULL,
                        symbol TEXT NOT NULL,
                        scan_run_id INTEGER,
                        scan_score_history_id INTEGER,
                        condition_key TEXT NOT NULL,
                        rule_type TEXT NOT NULL,
                        message TEXT NOT NULL,
                        value REAL,
                        threshold REAL,
                        previous_value REAL,
                        result_json TEXT,
                        created_at TEXT NOT NULL,
                        acknowledged_at TEXT,
                        cleared_at TEXT,
                        FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE,
                        FOREIGN KEY (scan_run_id) REFERENCES scheduled_scan_runs(id) ON DELETE SET NULL
                    );

                    CREATE TABLE IF NOT EXISTS alert_delivery_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        alert_event_id INTEGER NOT NULL,
                        alert_id INTEGER NOT NULL,
                        channel TEXT NOT NULL,
                        destination TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL,
                        retry_count INTEGER NOT NULL DEFAULT 0,
                        last_attempted_at TEXT NOT NULL,
                        error_message TEXT,
                        response_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY (alert_event_id) REFERENCES alert_events(id) ON DELETE CASCADE,
                        FOREIGN KEY (alert_id) REFERENCES alerts(id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_scheduled_scans_due
                    ON scheduled_scans (enabled, next_run_at);

                    CREATE INDEX IF NOT EXISTS idx_scheduled_scan_runs_schedule_started
                    ON scheduled_scan_runs (scheduled_scan_id, started_at);

                    CREATE INDEX IF NOT EXISTS idx_alert_events_created
                    ON alert_events (created_at);

                    CREATE INDEX IF NOT EXISTS idx_alert_events_alert_symbol
                    ON alert_events (alert_id, symbol, condition_key);

                    CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_events_open_condition
                    ON alert_events (alert_id, symbol, condition_key)
                    WHERE cleared_at IS NULL;

                    CREATE UNIQUE INDEX IF NOT EXISTS ux_alert_delivery_attempt_event_channel
                    ON alert_delivery_attempts (alert_event_id, channel);

                    CREATE INDEX IF NOT EXISTS idx_alert_delivery_attempts_status
                    ON alert_delivery_attempts (status, last_attempted_at);
                    """
                )
                self._ensure_schema_migrations(connection)
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_scheduled_scans_owner_due
                    ON scheduled_scans (owner_id, enabled, next_run_at)
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_alerts_owner_enabled
                    ON alerts (owner_id, enabled)
                    """
                )
            self._schema_ready = True

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_schema_migrations(self, connection: sqlite3.Connection) -> None:
        _ensure_columns(connection, "scheduled_scans", {"owner_id": "TEXT"})
        _ensure_columns(
            connection,
            "alerts",
            {
                "owner_id": "TEXT",
                "delivery_channels_json": "TEXT NOT NULL DEFAULT '[]'",
            },
        )

    def _create_run(self, schedule_id: int, started_at: datetime) -> int:
        self._ensure_schema()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scheduled_scan_runs (
                    scheduled_scan_id,
                    started_at,
                    status,
                    symbols_scanned_json,
                    errors_json,
                    result_count
                )
                VALUES (?, ?, 'running', '[]', '[]', 0)
                """,
                (schedule_id, _datetime_to_json(started_at)),
            )
            return int(cursor.lastrowid)

    def _finish_run(
        self,
        run_id: int,
        *,
        finished_at: datetime,
        status: str,
        symbols: Sequence[str],
        errors: Sequence[Mapping[str, Any]],
        result_count: int,
        response: Mapping[str, Any] | None,
        error_message: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE scheduled_scan_runs
                SET finished_at = ?,
                    status = ?,
                    symbols_scanned_json = ?,
                    errors_json = ?,
                    result_count = ?,
                    response_json = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    _datetime_to_json(finished_at),
                    status,
                    _json_dumps(list(symbols)),
                    _json_dumps([dict(error) for error in errors]),
                    result_count,
                    _json_dumps(dict(response)) if response is not None else None,
                    error_message,
                    run_id,
                ),
            )

    def _update_schedule_after_run(self, schedule: Mapping[str, Any], finished_at: datetime) -> None:
        next_run = None
        if schedule["enabled"]:
            next_run = finished_at + timedelta(seconds=int(schedule["interval_seconds"]))
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE scheduled_scans
                SET last_run_at = ?, next_run_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    _datetime_to_json(finished_at),
                    _datetime_to_json(next_run),
                    _datetime_to_json(finished_at),
                    schedule["id"],
                ),
            )

    def _yahoo_most_shorted_symbols(self, target: Mapping[str, Any]) -> list[str]:
        if self.yahoo_screener is None:
            raise ScheduleTargetError("Yahoo most-shorted schedules require a screener.")
        count = int(target.get("count", 100))
        if count < 1 or count > MAX_SCHEDULE_SYMBOLS:
            raise ScheduleTargetError(f"Yahoo most-shorted count must be between 1 and {MAX_SCHEDULE_SYMBOLS}.")
        try:
            symbols = self.yahoo_screener.most_shorted_symbols(count=count)
        except ScreenerError:
            raise
        except Exception as exc:
            raise ScheduleTargetError(f"Yahoo most-shorted target failed: {exc}") from exc
        return normalize_symbols(symbols, max_symbols=count)

    def _watchlist_symbols(self, target: Mapping[str, Any]) -> list[str]:
        explicit = _maybe_symbols_from_mapping(target)
        if explicit is not None:
            return explicit

        if not self._table_exists("watchlist_symbols"):
            raise ScheduleTargetError("Watchlist schedules require watchlist_symbols data or explicit symbols.")
        watchlist_id = self._resolve_named_id("watchlists", target, "watchlist")
        if watchlist_id is None:
            raise ScheduleTargetError("Watchlist schedules require watchlist_id, id, name, or explicit symbols.")

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbol
                FROM watchlist_symbols
                WHERE watchlist_id = ?
                ORDER BY symbol ASC
                """,
                (watchlist_id,),
            ).fetchall()
        return _normalize_resolved_symbols([row["symbol"] for row in rows], "Watchlist target did not contain symbols.")

    def _saved_screen_symbols(self, target: Mapping[str, Any]) -> list[str]:
        explicit = _maybe_symbols_from_mapping(target)
        if explicit is not None:
            return explicit

        saved_screen_id = self._resolve_named_id("saved_screens", target, "saved_screen")
        if saved_screen_id is not None and self._table_exists("saved_screen_symbols"):
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT symbol
                    FROM saved_screen_symbols
                    WHERE saved_screen_id = ?
                    ORDER BY symbol ASC
                    """,
                    (saved_screen_id,),
                ).fetchall()
            if rows:
                return _normalize_resolved_symbols(
                    [row["symbol"] for row in rows],
                    "Saved screen target did not contain symbols.",
                )

        if saved_screen_id is None:
            raise ScheduleTargetError("Saved-screen schedules require saved_screen_id, id, name, or explicit symbols.")
        if not self._table_exists("saved_screens"):
            raise ScheduleTargetError("Saved-screen schedules require saved_screens data or explicit symbols.")

        with self._connect() as connection:
            row = connection.execute(
                "SELECT filters_json FROM saved_screens WHERE id = ?",
                (saved_screen_id,),
            ).fetchone()
        if row is None:
            raise ScheduleTargetError(f"Saved screen {saved_screen_id} was not found.")
        filters = _loads_json_object(row["filters_json"] or "{}")
        symbols = _maybe_symbols_from_mapping(filters)
        if symbols is None:
            raise ScheduleTargetError(
                "Saved-screen schedules need symbols in filters_json until saved-screen filtering is available."
            )
        return symbols

    def _resolve_named_id(self, table_name: str, target: Mapping[str, Any], target_label: str) -> int | None:
        candidate = target.get(f"{target_label}_id", target.get("id"))
        if candidate is not None:
            try:
                return int(candidate)
            except (TypeError, ValueError) as exc:
                raise ScheduleTargetError(f"Invalid {target_label} id: {candidate}") from exc

        name = target.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        if not self._table_exists(table_name):
            raise ScheduleTargetError(f"{target_label.replace('_', ' ').title()} schedules require {table_name} data.")
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT id FROM {table_name} WHERE name = ? ORDER BY id ASC LIMIT 1",
                (name.strip(),),
            ).fetchone()
        if row is None:
            raise ScheduleTargetError(f"{target_label.replace('_', ' ').title()} named {name!r} was not found.")
        return int(row["id"])

    def _table_exists(self, table_name: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table_name,),
            ).fetchone()
        return row is not None

    def _create_alert_event(
        self,
        alert: Mapping[str, Any],
        result: Mapping[str, Any],
        evaluation: AlertEvaluation,
        scan_run_id: int | None,
    ) -> dict[str, Any] | None:
        symbol = str(result.get("symbol") or "").strip().upper()
        now = self.clock()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO alert_events (
                        alert_id,
                        symbol,
                        scan_run_id,
                        scan_score_history_id,
                        condition_key,
                        rule_type,
                        message,
                        value,
                        threshold,
                        previous_value,
                        result_json,
                        created_at
                    )
                    VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alert["id"],
                        symbol,
                        scan_run_id,
                        evaluation.condition_key,
                        evaluation.rule_type,
                        evaluation.message,
                        evaluation.value,
                        evaluation.threshold,
                        evaluation.previous_value,
                        _json_dumps(dict(result)),
                        _datetime_to_json(now),
                    ),
                )
                event_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None
        event = self._get_alert_event(event_id, include_delivery_attempts=False)
        event["delivery_attempts"] = self._deliver_alert_event(alert, event, alert.get("delivery_channels", []))
        return event

    def _clear_alert_event(self, alert_id: int, symbol: str, condition_key: str) -> None:
        now = self.clock()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE alert_events
                SET cleared_at = ?
                WHERE alert_id = ?
                  AND symbol = ?
                  AND condition_key = ?
                  AND cleared_at IS NULL
                """,
                (_datetime_to_json(now), alert_id, symbol, condition_key),
            )

    def _deliver_alert_event(
        self,
        alert: Mapping[str, Any],
        event: Mapping[str, Any],
        channel_names: Sequence[str],
        *,
        retry_attempt_id: int | None = None,
    ) -> list[dict[str, Any]]:
        channels = normalize_delivery_channels(channel_names)
        if not channels:
            return []

        if self.alert_delivery is None:
            outcomes = [
                AlertDeliveryOutcome(
                    channel=channel,
                    destination="disabled",
                    status="failure",
                    response={},
                    error_message="Alert delivery is disabled.",
                )
                for channel in channels
            ]
        else:
            outcomes = self.alert_delivery.deliver(alert, event, channels)

        attempts: list[dict[str, Any]] = []
        for outcome in outcomes:
            attempt = self._record_alert_delivery_outcome(
                alert_event_id=int(event["id"]),
                alert_id=int(alert["id"]),
                outcome=outcome,
                attempt_id=retry_attempt_id,
            )
            attempts.append(attempt)
            if outcome.status != "success":
                logger.warning(
                    "Alert delivery failed for event %s via %s: %s",
                    event["id"],
                    outcome.channel,
                    outcome.error_message,
                )
        return attempts

    def _record_alert_delivery_outcome(
        self,
        *,
        alert_event_id: int,
        alert_id: int,
        outcome: AlertDeliveryOutcome,
        attempt_id: int | None = None,
    ) -> dict[str, Any]:
        now_json = _datetime_to_json(self.clock())
        if attempt_id is None:
            try:
                with self._connect() as connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO alert_delivery_attempts (
                            alert_event_id,
                            alert_id,
                            channel,
                            destination,
                            status,
                            retry_count,
                            last_attempted_at,
                            error_message,
                            response_json,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                        """,
                        (
                            alert_event_id,
                            alert_id,
                            outcome.channel,
                            outcome.destination,
                            outcome.status,
                            now_json,
                            outcome.error_message,
                            _json_dumps(dict(outcome.response)),
                            now_json,
                            now_json,
                        ),
                    )
                    attempt_id = int(cursor.lastrowid)
            except sqlite3.IntegrityError:
                with self._connect() as connection:
                    row = connection.execute(
                        """
                        SELECT id
                        FROM alert_delivery_attempts
                        WHERE alert_event_id = ? AND channel = ?
                        """,
                        (alert_event_id, outcome.channel),
                    ).fetchone()
                if row is None:
                    raise
                attempt_id = int(row["id"])
        else:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    UPDATE alert_delivery_attempts
                    SET destination = ?,
                        status = ?,
                        retry_count = retry_count + 1,
                        last_attempted_at = ?,
                        error_message = ?,
                        response_json = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        outcome.destination,
                        outcome.status,
                        now_json,
                        outcome.error_message,
                        _json_dumps(dict(outcome.response)),
                        now_json,
                        attempt_id,
                    ),
                )
                if cursor.rowcount == 0:
                    raise AlertNotFoundError(f"Alert delivery attempt {attempt_id} was not found.")
        return self._get_alert_delivery_attempt(attempt_id)

    def _get_alert_event(self, event_id: int, *, include_delivery_attempts: bool = True) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM alert_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise AlertNotFoundError(f"Alert event {event_id} was not found.")
        event = _event_from_row(row)
        if include_delivery_attempts:
            self._attach_delivery_attempts([event])
        return event

    def _get_alert_delivery_attempt(self, attempt_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM alert_delivery_attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise AlertNotFoundError(f"Alert delivery attempt {attempt_id} was not found.")
        return _delivery_attempt_from_row(row)

    def _attach_delivery_attempts(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        event_ids = [int(event["id"]) for event in events]
        placeholders = ", ".join("?" for _ in event_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM alert_delivery_attempts
                WHERE alert_event_id IN ({placeholders})
                ORDER BY id ASC
                """,
                tuple(event_ids),
            ).fetchall()
        attempts_by_event: dict[int, list[dict[str, Any]]] = {event_id: [] for event_id in event_ids}
        for row in rows:
            attempt = _delivery_attempt_from_row(row)
            attempts_by_event[int(attempt["alert_event_id"])].append(attempt)
        for event in events:
            event["delivery_attempts"] = attempts_by_event.get(int(event["id"]), [])

    def _normalize_alert_delivery_channels(self, delivery_channels: Sequence[str] | None) -> list[str]:
        if delivery_channels is None and self.alert_delivery is not None:
            return list(self.alert_delivery.default_channels)
        return normalize_delivery_channels(delivery_channels)


class AutomationScheduler:
    """Small in-process polling scheduler for due scheduled scans."""

    def __init__(self, service: AutomationService, poll_interval_seconds: int = DEFAULT_SCHEDULER_POLL_SECONDS) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        self.service = service
        self.poll_interval_seconds = poll_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._last_poll_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_error_at: datetime | None = None
        self._last_error: str | None = None
        self._total_polls = 0
        self._total_errors = 0
        self._total_runs_started = 0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        with self._state_lock:
            self._started_at = _utc_now()
            self._stopped_at = None
        self._thread = threading.Thread(
            target=self._run,
            name="squeeze-scanner-automation-scheduler",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        with self._state_lock:
            self._stopped_at = _utc_now()

    def run_once(self) -> list[dict[str, Any]]:
        poll_started = _utc_now()
        with self._state_lock:
            self._last_poll_at = poll_started
            self._total_polls += 1
        try:
            runs = self.service.run_due_schedules()
        except Exception as exc:
            with self._state_lock:
                self._last_error_at = _utc_now()
                self._last_error = str(exc)
                self._total_errors += 1
            raise
        with self._state_lock:
            self._last_success_at = _utc_now()
            self._last_error = None
            self._total_runs_started += len(runs)
        return runs

    def status(self) -> dict[str, Any]:
        thread_alive = self._thread is not None and self._thread.is_alive()
        with self._state_lock:
            return {
                "mode": "in_process",
                "running": thread_alive,
                "poll_interval_seconds": self.poll_interval_seconds,
                "started_at": _datetime_to_json(self._started_at),
                "stopped_at": _datetime_to_json(self._stopped_at),
                "last_poll_at": _datetime_to_json(self._last_poll_at),
                "last_success_at": _datetime_to_json(self._last_success_at),
                "last_error_at": _datetime_to_json(self._last_error_at),
                "last_error": self._last_error,
                "total_polls": self._total_polls,
                "total_errors": self._total_errors,
                "total_runs_started": self._total_runs_started,
            }

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Automation scheduler poll failed")
            self._stop_event.wait(self.poll_interval_seconds)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _datetime_to_json(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise AutomationError(f"Invalid datetime: {value}") from exc
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    raise AutomationError(f"Invalid datetime: {value}")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _loads_json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise AutomationError(f"Invalid JSON payload: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AutomationError("Expected a JSON object.")
    return parsed


def _loads_json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _require_name(value: str, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AutomationError(message)
    return value.strip()


def _normalize_owner_id(owner_id: Any) -> str | None:
    if owner_id is None:
        return None
    normalized = str(owner_id).strip()
    return normalized or None


def _ensure_columns(connection: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    for column, ddl in columns.items():
        if column not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _normalize_target_type(target_type: str) -> str:
    normalized = str(target_type or "").strip().lower().replace("-", "_")
    aliases = {
        "saved_screen": "saved_screen",
        "saved_screen_id": "saved_screen",
        "watchlist": "watchlist",
        "yahoo": "yahoo_most_shorted",
        "yahoo_most_shorted": "yahoo_most_shorted",
        "most_shorted": "yahoo_most_shorted",
        "symbols": "symbols",
        "symbol_list": "symbols",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in VALID_TARGET_TYPES:
        raise AutomationError(f"Unsupported scheduled target type: {target_type}")
    return normalized


def _validate_interval(interval_seconds: int) -> int:
    try:
        interval = int(interval_seconds)
    except (TypeError, ValueError) as exc:
        raise AutomationError("interval_seconds must be an integer.") from exc
    if interval <= 0:
        raise AutomationError("interval_seconds must be greater than zero.")
    return interval


def _normalize_target_payload(
    target_type: str,
    target: Mapping[str, Any] | Sequence[str] | str | None,
) -> dict[str, Any]:
    if target_type == "symbols":
        if isinstance(target, Mapping):
            symbols = target.get("symbols", target.get("symbol"))
        else:
            symbols = target
        normalized = normalize_symbols(symbols or [], max_symbols=MAX_SCHEDULE_SYMBOLS)
        return {"symbols": normalized}

    if target_type == "yahoo_most_shorted":
        count = 100
        if isinstance(target, Mapping) and target.get("count") is not None:
            count = int(target["count"])
        if count < 1 or count > MAX_SCHEDULE_SYMBOLS:
            raise AutomationError(f"Yahoo most-shorted count must be between 1 and {MAX_SCHEDULE_SYMBOLS}.")
        return {"count": count}

    if target is None:
        return {}
    if isinstance(target, Mapping):
        payload = dict(target)
    elif isinstance(target, str):
        payload = {"name": target}
    else:
        payload = {"symbols": list(target)}

    if "symbols" in payload or "symbol" in payload:
        symbols = payload.get("symbols", payload.get("symbol"))
        payload["symbols"] = normalize_symbols(symbols, max_symbols=MAX_SCHEDULE_SYMBOLS)
        payload.pop("symbol", None)
    return payload


def _normalize_symbols_from_target(target: Mapping[str, Any]) -> list[str]:
    symbols = target.get("symbols", target.get("symbol"))
    return normalize_symbols(symbols or [], max_symbols=MAX_SCHEDULE_SYMBOLS)


def _maybe_symbols_from_mapping(target: Mapping[str, Any]) -> list[str] | None:
    for key in ("symbols", "symbol", "tickers", "ticker_symbols"):
        if key in target:
            return normalize_symbols(target[key], max_symbols=MAX_SCHEDULE_SYMBOLS)
    return None


def _normalize_resolved_symbols(symbols: Sequence[Any], empty_message: str) -> list[str]:
    try:
        return normalize_symbols([str(symbol) for symbol in symbols], max_symbols=MAX_SCHEDULE_SYMBOLS)
    except InvalidSymbolError as exc:
        raise ScheduleTargetError(str(exc) if symbols else empty_message) from exc


def _target_max_symbols(target: Mapping[str, Any], symbols: Sequence[str]) -> int:
    try:
        configured = int(target.get("max_symbols", 0))
    except (TypeError, ValueError):
        configured = 0
    return max(configured, len(symbols), 1)


def _coerce_error_list(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    errors: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, Mapping):
            symbol = str(item.get("symbol", "*") or "*")
            message = str(item.get("message", item) or item)
        else:
            symbol = "*"
            message = str(item)
        errors.append({"symbol": symbol, "message": message})
    return errors


def _coerce_result_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _result_count(response: Mapping[str, Any], results: Sequence[Mapping[str, Any]]) -> int:
    try:
        return int(response.get("count", len(results)))
    except (TypeError, ValueError):
        return len(results)


def _run_status(result_count: int, errors: Sequence[Mapping[str, str]]) -> str:
    if errors and result_count <= 0:
        return "failure"
    if errors:
        return "partial_success"
    return "success"


def _row_int(row: sqlite3.Row | None, key: str) -> int:
    if row is None or row[key] is None:
        return 0
    return int(row[key])


def _run_status_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "scheduled_scan_id": int(row["scheduled_scan_id"]),
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error_message": row["error_message"],
    }


def _schedule_from_row(row: sqlite3.Row) -> dict[str, Any]:
    target = _loads_json(row["target_json"], {})
    return {
        "id": int(row["id"]),
        "owner_id": row["owner_id"],
        "name": row["name"],
        "target_type": row["target_type"],
        "target": target if isinstance(target, dict) else {},
        "interval_seconds": int(row["interval_seconds"]),
        "enabled": bool(row["enabled"]),
        "last_run_at": row["last_run_at"],
        "next_run_at": row["next_run_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _run_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "scheduled_scan_id": int(row["scheduled_scan_id"]),
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "symbols_scanned": _loads_json(row["symbols_scanned_json"], []),
        "errors": _loads_json(row["errors_json"], []),
        "result_count": int(row["result_count"]),
        "response": _loads_json(row["response_json"], None),
        "error_message": row["error_message"],
    }


def _alert_from_row(row: sqlite3.Row) -> dict[str, Any]:
    rule = _loads_json(row["rule_json"], {})
    delivery_channels = _loads_json(row["delivery_channels_json"], [])
    return {
        "id": int(row["id"]),
        "owner_id": row["owner_id"],
        "name": row["name"],
        "rule": rule if isinstance(rule, dict) else {},
        "enabled": bool(row["enabled"]),
        "delivery_channels": normalize_delivery_channels(delivery_channels if isinstance(delivery_channels, list) else []),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _event_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "alert_id": int(row["alert_id"]),
        "symbol": row["symbol"],
        "scan_run_id": row["scan_run_id"],
        "scan_score_history_id": row["scan_score_history_id"],
        "condition_key": row["condition_key"],
        "rule_type": row["rule_type"],
        "message": row["message"],
        "value": row["value"],
        "threshold": row["threshold"],
        "previous_value": row["previous_value"],
        "result": _loads_json(row["result_json"], None),
        "created_at": row["created_at"],
        "acknowledged_at": row["acknowledged_at"],
        "cleared_at": row["cleared_at"],
        "active": row["cleared_at"] is None,
    }


def _delivery_attempt_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "alert_event_id": int(row["alert_event_id"]),
        "alert_id": int(row["alert_id"]),
        "channel": row["channel"],
        "destination": row["destination"],
        "status": row["status"],
        "retry_count": int(row["retry_count"]),
        "last_attempted_at": row["last_attempted_at"],
        "error_message": row["error_message"],
        "response": _loads_json(row["response_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _normalize_alert_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(rule, Mapping):
        raise AutomationError("Alert rule must be an object.")
    raw_type = str(rule.get("type", rule.get("rule_type", ""))).strip().lower().replace("-", "_")
    aliases = {
        "score_crosses_threshold": "score_threshold",
        "score": "score_threshold",
        "model_crosses_threshold": "model_threshold",
        "selected_model_threshold": "model_threshold",
        "score_increase_1h": "score_increase",
        "score_increase_24h": "score_increase",
        "relative_volume": "relative_volume_threshold",
        "short_interest": "short_interest_threshold",
        "float_compression": "float_compression_threshold",
        "float_compression_score": "float_compression_threshold",
        "gamma": "gamma_score_threshold",
        "gamma_score": "gamma_score_threshold",
    }
    rule_type = aliases.get(raw_type, raw_type)
    valid_types = {
        "score_threshold",
        "model_threshold",
        "score_increase",
        "relative_volume_threshold",
        "short_interest_threshold",
        "float_compression_threshold",
        "gamma_score_threshold",
    }
    if rule_type not in valid_types:
        raise AutomationError(f"Unsupported alert rule type: {raw_type or '<missing>'}")

    threshold_key = "delta" if rule_type == "score_increase" else "threshold"
    threshold = _number(rule.get(threshold_key, rule.get("threshold", rule.get("value"))))
    if threshold is None:
        raise AutomationError(f"Alert rule {rule_type} requires a numeric {threshold_key}.")

    direction = str(rule.get("direction", "above")).strip().lower()
    if direction not in {"above", "below"}:
        raise AutomationError("Alert direction must be 'above' or 'below'.")

    normalized: dict[str, Any] = {
        "type": rule_type,
        "threshold": threshold,
        "direction": direction,
    }
    if rule_type == "model_threshold":
        model = str(rule.get("model", rule.get("model_key", rule.get("selected_model", "")))).strip()
        if not model:
            raise AutomationError("Model threshold alerts require model or model_key.")
        normalized["model"] = model
    if rule_type == "score_increase":
        window = str(rule.get("window", "")).strip().lower().replace(" ", "")
        if not window:
            window = "1h" if raw_type.endswith("1h") else "24h" if raw_type.endswith("24h") else "1h"
        aliases_by_window = {"60m": "1h", "1hour": "1h", "24hour": "24h", "1d": "24h", "day": "24h"}
        window = aliases_by_window.get(window, window)
        if window not in {"1h", "24h"}:
            raise AutomationError("Score increase alerts support only 1h and 24h windows.")
        normalized["window"] = window
    return normalized


def _evaluate_alert(rule: Mapping[str, Any], result: Mapping[str, Any], symbol: str) -> AlertEvaluation | None:
    rule_type = str(rule.get("type", ""))
    threshold = _number(rule.get("threshold"))
    direction = str(rule.get("direction", "above"))
    if threshold is None:
        return None

    if rule_type == "score_threshold":
        value = _number(result.get("score"))
        label = "score"
        condition = f"score:{direction}:{threshold:g}"
    elif rule_type == "model_threshold":
        model = str(rule.get("model", ""))
        value = _model_score(result, model)
        label = f"{model.replace('_', ' ')} model score"
        condition = f"model:{model}:{direction}:{threshold:g}"
    elif rule_type == "score_increase":
        window = str(rule.get("window", "1h"))
        value = _score_delta(result, window)
        label = f"{window} score increase"
        condition = f"score_delta:{window}:{direction}:{threshold:g}"
    elif rule_type == "relative_volume_threshold":
        value = _metric_value(result, "relative_volume")
        label = "relative volume"
        condition = f"metric:relative_volume:{direction}:{threshold:g}"
    elif rule_type == "short_interest_threshold":
        value = _metric_value(result, "short_percent_float")
        label = "short interest"
        condition = f"metric:short_percent_float:{direction}:{threshold:g}"
    elif rule_type == "float_compression_threshold":
        value = _model_score(result, "float_compression")
        label = "float compression score"
        condition = f"model:float_compression:{direction}:{threshold:g}"
    elif rule_type == "gamma_score_threshold":
        value = _model_score(result, "gamma_candidate")
        label = "gamma score"
        condition = f"model:gamma_candidate:{direction}:{threshold:g}"
    else:
        return None

    if value is None:
        return None

    active = value >= threshold if direction == "above" else value <= threshold
    verb = "crossed above" if direction == "above" else "crossed below"
    message = f"{symbol} {label} {verb} {threshold:g} (now {value:g})."
    return AlertEvaluation(
        rule_type=rule_type,
        condition_key=condition,
        active=active,
        value=value,
        threshold=threshold,
        message=message,
    )


def _model_score(result: Mapping[str, Any], model_key: str) -> float | None:
    model_scores = result.get("model_scores")
    if isinstance(model_scores, Mapping):
        value = _number(model_scores.get(model_key))
        if value is not None:
            return value
    return _number(result.get(f"{model_key}_score"))


def _metric_value(result: Mapping[str, Any], metric_key: str) -> float | None:
    value = _number(result.get(metric_key))
    if value is not None:
        return value
    metrics = result.get("metrics")
    if isinstance(metrics, Mapping):
        return _number(metrics.get(metric_key))
    return None


def _score_delta(result: Mapping[str, Any], window: str) -> float | None:
    candidates = [
        f"score_delta_{window}",
        f"score_change_{window}",
        f"score_increase_{window}",
        f"delta_{window}",
    ]
    if window == "1h":
        candidates.extend(["score_delta_60m", "delta_60m", "previous_scan_delta"])
    if window == "24h":
        candidates.extend(["score_delta_1d", "score_change_1d", "delta_1d"])

    for key in candidates:
        value = _number(result.get(key))
        if value is not None:
            return value
    for field_name in ("score_deltas", "score_changes", "score_increases", "deltas"):
        mapping = result.get(field_name)
        if not isinstance(mapping, Mapping):
            continue
        for key in (window, *candidates):
            value = _number(mapping.get(key))
            if value is not None:
                return value
    metrics = result.get("metrics")
    if isinstance(metrics, Mapping):
        for key in candidates:
            value = _number(metrics.get(key))
            if value is not None:
                return value
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number
