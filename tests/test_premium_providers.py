from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from squeeze_scanner.config import get_settings
from squeeze_scanner.domain import PremiumProviderNotConfigured, TickerSnapshot
from squeeze_scanner.providers.premium import (
    CompositeMarketDataProvider,
    PremiumProviderSet,
    build_premium_provider,
    build_premium_provider_set,
)
from squeeze_scanner.scoring import score_snapshot
from squeeze_scanner.web import create_app


PROVIDER_ENV_VARS = (
    "SQUEEZE_SCANNER_QUOTE_PROVIDER",
    "SQUEEZE_SCANNER_BORROW_PROVIDER",
    "SQUEEZE_SCANNER_SHORT_INTEREST_PROVIDER",
    "SQUEEZE_SCANNER_CORPORATE_ACTIONS_PROVIDER",
    "SQUEEZE_SCANNER_FILINGS_PROVIDER",
    "SQUEEZE_SCANNER_EVENT_PROVIDER",
)


def test_provider_settings_default_to_yahoo_with_disabled_premium_feeds(monkeypatch):
    for name in PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()

    try:
        settings = get_settings()
        provider_set = build_premium_provider_set(settings)
    finally:
        get_settings.cache_clear()

    assert settings.quote_provider == "yahoo"
    assert {
        settings.borrow_provider,
        settings.short_interest_provider,
        settings.corporate_actions_provider,
        settings.filings_provider,
        settings.event_provider,
    } == {"disabled"}
    assert all(not provider.enabled for provider in provider_set.all())
    assert {status["status"] for status in provider_set.statuses()} == {"disabled"}


def test_unconfigured_premium_provider_fails_explicitly():
    provider = build_premium_provider("borrow", "PremiumBorrowCo")

    with pytest.raises(PremiumProviderNotConfigured, match="premiumborrowco"):
        provider.fetch_borrow("GME")

    health = provider.health().to_dict()
    assert health["feed"] == "borrow"
    assert health["provider"] == "premiumborrowco"
    assert health["enabled"] is True
    assert health["configured"] is False
    assert health["status"] == "unconfigured"


def test_unconfigured_borrow_provider_preserves_yahoo_data_without_bullish_borrow_score():
    class BaseProvider:
        def fetch(self, symbol):
            return TickerSnapshot(
                symbol=symbol,
                price=10.0,
                volume=2_000_000,
                avg_volume_20d=1_000_000,
                short_percent_float=40.0,
                short_ratio=10.0,
                float_shares=20_000_000,
                market_cap=200_000_000,
                borrow_fee_pct=None,
                recent_reverse_split=False,
                change_5d_pct=10.0,
                change_20d_pct=20.0,
                source_fetched_at=datetime.now(timezone.utc).isoformat(),
                field_sources={
                    "price": "yahoo_finance",
                    "short_percent_float": "yahoo_finance",
                    "short_ratio": "yahoo_finance",
                },
                field_quality={"borrow_fee_pct": "missing"},
                source_quality={"yahoo_finance": 70.0},
            )

    provider_set = PremiumProviderSet(
        borrow=build_premium_provider("borrow", "PremiumBorrowCo"),
        short_interest=build_premium_provider("short_interest", "disabled"),
        corporate_actions=build_premium_provider("corporate_actions", "disabled"),
        filings=build_premium_provider("filings", "disabled"),
        events=build_premium_provider("events", "disabled"),
    )
    snapshot = CompositeMarketDataProvider(BaseProvider(), provider_set).fetch("GME")
    result = score_snapshot(snapshot)

    assert snapshot.borrow_fee_pct is None
    assert snapshot.field_quality["borrow_fee_pct"] == "provider-error"
    assert snapshot.field_sources["borrow_fee_pct"] == "premiumborrowco_borrow"
    assert snapshot.source_quality["premiumborrowco_borrow"] == 0.0
    assert any("Optional borrow provider" in warning for warning in snapshot.source_warnings)
    assert result.model_components["classical_short_squeeze"]["borrow_fee"] == 0.0
    assert result.model_components["hybrid"]["borrow_fee"] == 0.0
    assert any(flag["key"] == "source_warnings" for flag in result.risk_flags)


def test_event_feed_risk_fields_create_flags_without_changing_scores():
    base_snapshot = TickerSnapshot(
        symbol="NEWS",
        price=10.0,
        volume=2_000_000,
        avg_volume_20d=1_000_000,
        short_percent_float=30.0,
        short_ratio=5.0,
        float_shares=20_000_000,
        market_cap=200_000_000,
        borrow_fee_pct=0.0,
        recent_reverse_split=False,
        call_volume=1_000,
        put_volume=500,
        call_open_interest=10_000,
        dealer_gamma_exposure_proxy=0.0,
        change_5d_pct=10.0,
        change_20d_pct=20.0,
        source_fetched_at=datetime.now(timezone.utc).isoformat(),
    )
    event_snapshot = replace(
        base_snapshot,
        active_trading_halt=True,
        material_news_event=True,
        event_risk=True,
        field_sources={
            "active_trading_halt": "premium_events",
            "material_news_event": "premium_events",
            "event_risk": "premium_events",
        },
        field_quality={
            "active_trading_halt": "present",
            "material_news_event": "present",
            "event_risk": "present",
        },
        source_quality={"premium_events": 90.0},
    )
    base_result = score_snapshot(base_snapshot)
    event_result = score_snapshot(event_snapshot)

    assert event_result.model_scores == base_result.model_scores
    flag_keys = {flag["key"] for flag in event_result.risk_flags}
    assert {"active_trading_halt", "event_risk", "material_news_event"} <= flag_keys


def test_provider_status_endpoint_lists_default_and_optional_feeds(monkeypatch):
    monkeypatch.setenv("SQUEEZE_SCANNER_BORROW_PROVIDER", "PremiumBorrowCo")
    get_settings.cache_clear()
    try:
        app = create_app()
        payload = asyncio.run(_route_endpoint(app, "/api/providers")())
    finally:
        get_settings.cache_clear()

    assert payload["default_provider"]["provider"] == "yahoo_finance"
    borrow_status = next(
        provider
        for provider in payload["premium_providers"]
        if provider["feed"] == "borrow"
    )
    assert borrow_status["status"] == "unconfigured"
    assert borrow_status["configured"] is False
    assert borrow_status["capability"]["affects_scores"] is True


def _route_endpoint(app, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route not found: {path}")
