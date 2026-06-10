from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from squeeze_scanner.alert_delivery import AlertDeliveryResult, AlertDeliveryService
from squeeze_scanner.automation import AutomationScheduler, AutomationService


class StaticScanner:
    def __init__(self, score: float = 72.0, errors: list[dict[str, str]] | None = None) -> None:
        self.score = score
        self.errors = errors or []
        self.calls: list[tuple[list[str], int]] = []

    def scan(self, symbols, max_symbols=25):
        normalized = list(symbols)
        self.calls.append((normalized, max_symbols))
        results = [
            {
                "symbol": symbol,
                "score": self.score,
                "model_scores": {
                    "float_compression": self.score - 5,
                    "gamma_candidate": self.score - 10,
                },
                "metrics": {
                    "relative_volume": 4.2,
                    "short_percent_float": 38.0,
                },
            }
            for symbol in normalized
        ]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(results),
            "results": results,
            "errors": self.errors,
        }


class FailingScanner:
    def scan(self, symbols, max_symbols=25):
        raise RuntimeError("provider unavailable")


class RecordingAlertChannel:
    name = "webhook"
    destination = "https://alerts.example.test"

    def __init__(self, result: AlertDeliveryResult | None = None, raises: Exception | None = None) -> None:
        self.result = result or AlertDeliveryResult.success({"accepted": True})
        self.raises = raises
        self.messages: list[dict] = []

    def send(self, message):
        self.messages.append(message.payload)
        if self.raises is not None:
            raise self.raises
        return self.result


def delivery_service(channel: RecordingAlertChannel, default_channels: list[str] | None = None) -> AlertDeliveryService:
    return AlertDeliveryService([channel], default_channels=default_channels or [], public_base_url="http://scanner.local")


def test_schedule_persistence_round_trips_sqlite_rows(tmp_path):
    db_path = tmp_path / "automation.sqlite3"
    service = AutomationService(db_path, StaticScanner())

    created = service.create_schedule(
        name="Hourly squeeze scan",
        target_type="symbols",
        target={"symbols": "gme, amc gme"},
        interval_seconds=3600,
    )

    reloaded = AutomationService(db_path, StaticScanner()).get_schedule(created["id"])

    assert reloaded["name"] == "Hourly squeeze scan"
    assert reloaded["target_type"] == "symbols"
    assert reloaded["target"] == {"symbols": ["GME", "AMC"]}
    assert reloaded["interval_seconds"] == 3600
    assert reloaded["enabled"] is True
    assert reloaded["next_run_at"] is not None


def test_owner_fields_are_optional_and_filterable_for_schedules_and_alerts(tmp_path):
    db_path = tmp_path / "automation.sqlite3"
    service = AutomationService(db_path, StaticScanner())
    legacy_schedule = service.create_schedule("Local", "symbols", {"symbols": ["gme"]}, 60)
    owned_schedule = service.create_schedule("Owned", "symbols", {"symbols": ["amc"]}, 60, owner_id="alice")
    legacy_alert = service.create_alert("Local alert", {"type": "score_threshold", "threshold": 70})
    owned_alert = service.create_alert("Owned alert", {"type": "score_threshold", "threshold": 70}, owner_id="alice")

    assert legacy_schedule["owner_id"] is None
    assert owned_schedule["owner_id"] == "alice"
    assert legacy_alert["owner_id"] is None
    assert owned_alert["owner_id"] == "alice"
    assert [schedule["name"] for schedule in service.list_schedules(owner_id="alice")] == ["Owned"]
    assert [alert["name"] for alert in service.list_alerts(owner_id="alice")] == ["Owned alert"]
    assert {schedule["name"] for schedule in service.list_schedules()} == {"Local", "Owned"}


def test_owner_columns_are_added_to_existing_automation_tables(tmp_path):
    db_path = tmp_path / "automation.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE scheduled_scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_json TEXT NOT NULL,
                interval_seconds INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                last_run_at TEXT,
                next_run_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                rule_json TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    status = AutomationService(db_path, StaticScanner()).status()

    assert status["status"] == "ok"
    with sqlite3.connect(db_path) as connection:
        schedule_columns = {row[1] for row in connection.execute("PRAGMA table_info(scheduled_scans)")}
        alert_columns = {row[1] for row in connection.execute("PRAGMA table_info(alerts)")}
    assert "owner_id" in schedule_columns
    assert "owner_id" in alert_columns
    assert "delivery_channels_json" in alert_columns


def test_scheduler_status_tracks_polls_without_requiring_background_thread(tmp_path):
    service = AutomationService(tmp_path / "automation.sqlite3", StaticScanner())
    scheduler = AutomationScheduler(service, poll_interval_seconds=5)

    assert scheduler.status()["running"] is False
    scheduler.run_once()
    status = scheduler.status()

    assert status["mode"] == "in_process"
    assert status["poll_interval_seconds"] == 5
    assert status["total_polls"] == 1
    assert status["last_success_at"] is not None


def test_run_success_and_failure_are_recorded(tmp_path):
    db_path = tmp_path / "automation.sqlite3"
    service = AutomationService(db_path, StaticScanner())
    schedule = service.create_schedule("Run winners", "symbols", {"symbols": ["high"]}, 60)

    success = service.run_scheduled_scan(schedule["id"])

    assert success["status"] == "success"
    assert success["finished_at"] is not None
    assert success["symbols_scanned"] == ["HIGH"]
    assert success["result_count"] == 1
    assert success["errors"] == []

    failing_service = AutomationService(db_path, FailingScanner())
    failing_schedule = failing_service.create_schedule("Run failures", "symbols", {"symbols": ["bad"]}, 60)

    failure = failing_service.run_scheduled_scan(failing_schedule["id"])

    assert failure["status"] == "failure"
    assert failure["finished_at"] is not None
    assert failure["symbols_scanned"] == ["BAD"]
    assert failure["result_count"] == 0
    assert failure["error_message"] == "provider unavailable"
    assert failure["errors"] == [{"symbol": "*", "message": "provider unavailable"}]


def test_alert_events_are_deduped_until_condition_clears(tmp_path):
    service = AutomationService(tmp_path / "automation.sqlite3", StaticScanner())
    service.create_alert("High score", {"type": "score_threshold", "threshold": 70})
    high = {"symbol": "GME", "score": 75.0, "model_scores": {}, "metrics": {}}
    low = {"symbol": "GME", "score": 60.0, "model_scores": {}, "metrics": {}}

    first_events = service.process_alerts([high])
    duplicate_events = service.process_alerts([high])
    service.process_alerts([low])
    reset_events = service.process_alerts([high])

    all_events = service.list_alert_events()

    assert len(first_events) == 1
    assert duplicate_events == []
    assert len(reset_events) == 1
    assert len(all_events) == 2
    assert all_events[0]["active"] is True
    assert all_events[1]["cleared_at"] is not None


def test_alert_delivery_defaults_to_in_app_only(tmp_path):
    channel = RecordingAlertChannel()
    service = AutomationService(
        tmp_path / "automation.sqlite3",
        StaticScanner(),
        alert_delivery=delivery_service(channel),
    )
    service.create_alert("High score", {"type": "score_threshold", "threshold": 70})

    events = service.process_alerts([{"symbol": "GME", "score": 75.0, "model_scores": {}, "metrics": {}}])

    assert len(events) == 1
    assert events[0]["delivery_attempts"] == []
    assert service.list_alert_delivery_attempts() == []
    assert channel.messages == []


def test_alert_delivery_runs_for_new_alert_event(tmp_path):
    channel = RecordingAlertChannel()
    service = AutomationService(
        tmp_path / "automation.sqlite3",
        StaticScanner(),
        alert_delivery=delivery_service(channel),
    )
    service.create_alert(
        "High score",
        {"type": "score_threshold", "threshold": 70},
        delivery_channels=["webhook"],
    )

    events = service.process_alerts(
        [
            {
                "symbol": "GME",
                "score": 75.0,
                "model_confidence": 0.82,
                "risk_flags": ["low_float"],
                "model_scores": {},
                "metrics": {},
            }
        ]
    )

    attempts = service.list_alert_delivery_attempts()
    assert len(events) == 1
    assert len(channel.messages) == 1
    assert channel.messages[0]["symbol"] == "GME"
    assert channel.messages[0]["score"] == 75.0
    assert channel.messages[0]["model_confidence"] == 0.82
    assert channel.messages[0]["risk_flags"] == ["low_float"]
    assert channel.messages[0]["link"] == f"http://scanner.local/?symbol=GME&alert_event_id={events[0]['id']}"
    assert attempts[0]["status"] == "success"
    assert attempts[0]["channel"] == "webhook"
    assert attempts[0]["destination"] == "https://alerts.example.test"
    assert events[0]["delivery_attempts"] == attempts


def test_alert_delivery_uses_configured_default_channels(tmp_path):
    channel = RecordingAlertChannel()
    service = AutomationService(
        tmp_path / "automation.sqlite3",
        StaticScanner(),
        alert_delivery=delivery_service(channel, default_channels=["webhook"]),
    )

    alert = service.create_alert("High score", {"type": "score_threshold", "threshold": 70})
    service.process_alerts([{"symbol": "GME", "score": 75.0, "model_scores": {}, "metrics": {}}])

    assert alert["delivery_channels"] == ["webhook"]
    assert len(channel.messages) == 1
    assert service.list_alert_delivery_attempts()[0]["status"] == "success"


def test_alert_delivery_is_deduped_while_condition_remains_active(tmp_path):
    channel = RecordingAlertChannel()
    service = AutomationService(
        tmp_path / "automation.sqlite3",
        StaticScanner(),
        alert_delivery=delivery_service(channel),
    )
    service.create_alert(
        "High score",
        {"type": "score_threshold", "threshold": 70},
        delivery_channels=["webhook"],
    )
    result = {"symbol": "GME", "score": 75.0, "model_scores": {}, "metrics": {}}

    first_events = service.process_alerts([result])
    duplicate_events = service.process_alerts([result])

    assert len(first_events) == 1
    assert duplicate_events == []
    assert len(channel.messages) == 1
    assert len(service.list_alert_delivery_attempts()) == 1


def test_alert_delivery_failures_are_persisted_and_retryable(tmp_path, caplog):
    failing_channel = RecordingAlertChannel(AlertDeliveryResult.failure("webhook unavailable"))
    service = AutomationService(
        tmp_path / "automation.sqlite3",
        StaticScanner(),
        alert_delivery=delivery_service(failing_channel),
    )
    service.create_alert(
        "High score",
        {"type": "score_threshold", "threshold": 70},
        delivery_channels=["webhook"],
    )

    with caplog.at_level("WARNING"):
        events = service.process_alerts([{"symbol": "GME", "score": 75.0, "model_scores": {}, "metrics": {}}])

    attempts = service.list_alert_delivery_attempts()
    assert len(events) == 1
    assert attempts[0]["status"] == "failure"
    assert attempts[0]["error_message"] == "webhook unavailable"
    assert attempts[0]["retry_count"] == 0
    assert "Alert delivery failed" in caplog.text

    succeeding_channel = RecordingAlertChannel()
    retrying_service = AutomationService(
        tmp_path / "automation.sqlite3",
        StaticScanner(),
        alert_delivery=delivery_service(succeeding_channel),
    )
    retried = retrying_service.retry_alert_delivery_attempt(attempts[0]["id"])

    assert retried["status"] == "success"
    assert retried["retry_count"] == 1
    assert retried["error_message"] is None
    assert len(succeeding_channel.messages) == 1


def test_alert_rule_types_use_available_result_fields(tmp_path):
    service = AutomationService(tmp_path / "automation.sqlite3", StaticScanner())
    service.create_alert("Score", {"type": "score_threshold", "threshold": 70})
    service.create_alert("Selected model", {"type": "model_threshold", "model": "hybrid", "threshold": 80})
    service.create_alert("One hour delta", {"type": "score_increase", "window": "1h", "delta": 5})
    service.create_alert("Day delta", {"type": "score_increase", "window": "24h", "delta": 10})
    service.create_alert("Relative volume", {"type": "relative_volume", "threshold": 3})
    service.create_alert("Short interest", {"type": "short_interest", "threshold": 30})
    service.create_alert("Float compression", {"type": "float_compression", "threshold": 50})
    service.create_alert("Gamma", {"type": "gamma_score", "threshold": 45})
    result = {
        "symbol": "GME",
        "score": 75,
        "previous_scan_delta": 6,
        "delta_24h": 12,
        "model_scores": {
            "hybrid": 82,
            "float_compression": 55,
            "gamma_candidate": 48,
        },
        "metrics": {
            "relative_volume": 4,
            "short_percent_float": 35,
        },
    }

    events = service.process_alerts([result])

    assert {event["rule_type"] for event in events} == {
        "score_threshold",
        "model_threshold",
        "score_increase",
        "relative_volume_threshold",
        "short_interest_threshold",
        "float_compression_threshold",
        "gamma_score_threshold",
    }
    assert len(events) == 8
