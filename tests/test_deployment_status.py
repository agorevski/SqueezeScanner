from __future__ import annotations

from types import SimpleNamespace

from squeeze_scanner import config
from squeeze_scanner.automation import AutomationScheduler, AutomationService
from squeeze_scanner.cache import CachedMarketDataProvider
from squeeze_scanner.domain import TickerSnapshot
from squeeze_scanner.web import _build_status_payload


class StaticProvider:
    def fetch(self, symbol):
        return TickerSnapshot(symbol=symbol, price=10.0)


class StaticScanner:
    def scan(self, symbols, max_symbols=25):
        return {"count": 0, "results": [], "errors": []}


def test_local_configuration_defaults_remain_zero_config(monkeypatch):
    monkeypatch.setattr(config, "load_environment", lambda: None)
    for name in (
        "SQUEEZE_SCANNER_SCHEDULER_ENABLED",
        "SQUEEZE_SCANNER_SCHEDULER_POLL_SECONDS",
        "SQUEEZE_SCANNER_DEFAULT_OWNER_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    config.get_settings.cache_clear()

    settings = config.get_settings()

    assert settings.scheduler_enabled is True
    assert settings.scheduler_poll_seconds == config.DEFAULT_SCHEDULER_POLL_SECONDS
    assert settings.default_owner_id is None

    config.get_settings.cache_clear()


def test_structured_status_payload_includes_cache_scheduler_and_owner_foundations(tmp_path):
    provider = CachedMarketDataProvider(StaticProvider(), tmp_path / "market.sqlite3")
    automation = AutomationService(tmp_path / "automation.sqlite3", StaticScanner())
    scheduler = AutomationScheduler(automation)
    settings = SimpleNamespace(scheduler_enabled=True, default_owner_id=None)

    payload = _build_status_payload(settings, provider, automation, scheduler)

    assert payload["status"] == "ok"
    assert payload["app"]["auth_required"] is False
    assert payload["app"]["owner_scoping_available"] is True
    assert payload["storage"]["backend"] == "sqlite"
    assert payload["cache"]["total_rows"] == 0
    assert payload["scheduler"]["enabled"] is True
    assert payload["automation"]["status"] == "ok"
