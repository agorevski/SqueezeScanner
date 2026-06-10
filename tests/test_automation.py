from __future__ import annotations

from datetime import datetime, timezone

from squeeze_scanner.automation import AutomationService


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
