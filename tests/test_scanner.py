import json
import sqlite3
from datetime import datetime, timezone

import pytest

from squeeze_scanner.cache import CachedMarketDataProvider
from squeeze_scanner.domain import DataProviderError, InvalidSymbolError, ScreenerError, TickerSnapshot
from squeeze_scanner.providers.yahoo import YahooFinanceScreener
from squeeze_scanner.scoring import (
    SCORING_MODEL_VERSION,
    SCORING_SIGNALS,
    SCORING_WEIGHTS,
    score_snapshot,
    scoring_model_metadata,
)
from squeeze_scanner.service import ScannerService, build_scan_response, normalize_symbols


def test_normalize_symbols_splits_dedupes_and_uppercases():
    assert normalize_symbols("gme, amc; cvna GME") == ["GME", "AMC", "CVNA"]


def test_normalize_symbols_rejects_invalid_symbols():
    with pytest.raises(InvalidSymbolError):
        normalize_symbols("GME, BAD/SYMBOL")


def test_scoring_weights_sum_to_100():
    assert SCORING_MODEL_VERSION == "squeeze-v2"
    assert sum(SCORING_WEIGHTS.values()) == 100
    assert SCORING_WEIGHTS == {signal["key"]: signal["weight"] for signal in SCORING_SIGNALS}


def test_scoring_model_metadata_exposes_signal_legend_from_python_model():
    metadata = scoring_model_metadata()

    assert metadata["version"] == SCORING_MODEL_VERSION
    assert metadata["signals"] == SCORING_SIGNALS
    assert metadata["weights"] == SCORING_WEIGHTS
    assert {signal["key"] for signal in metadata["signals"]} == set(SCORING_WEIGHTS)
    assert all(signal["label"] and signal["calculation"] and signal["favorable"] for signal in metadata["signals"])


def test_yahoo_most_shorted_screener_extracts_unique_symbols():
    screener = YahooFinanceScreener(
        screen=lambda query, count: {
            "quotes": [
                {"symbol": "hubc"},
                {"symbol": "HUBC"},
                {"symbol": " WOLF "},
                {"symbol": ""},
                {"not_symbol": "IGNORED"},
            ]
        }
    )

    assert screener.most_shorted_symbols(count=100) == ["HUBC", "WOLF"]


def test_yahoo_most_shorted_screener_rejects_empty_results():
    screener = YahooFinanceScreener(screen=lambda query, count: {"quotes": []})

    with pytest.raises(ScreenerError):
        screener.most_shorted_symbols(count=100)


def test_score_snapshot_identifies_high_quality_squeeze_setup():
    result = score_snapshot(
        TickerSnapshot(
            symbol="TEST",
            company_name="Test Co",
            price=10.0,
            previous_close=8.9,
            volume=15_000_000,
            avg_volume_20d=3_000_000,
            short_percent_float=60.0,
            short_ratio=15.0,
            shares_short=20_000_000,
            shares_short_prior_month=10_000_000,
            float_shares=8_000_000,
            market_cap=200_000_000,
            change_1d_pct=12.36,
            change_5d_pct=35.0,
            change_20d_pct=70.0,
            distance_from_52_week_high_pct=-2.0,
        )
    )

    assert result.score >= 95
    assert result.risk_level == "High squeeze setup"
    assert result.data_quality == 100
    assert result.metrics["relative_volume"] == 5.0


def test_score_snapshot_does_not_flag_low_short_interest_as_high_setup():
    result = score_snapshot(
        TickerSnapshot(
            symbol="MOMO",
            company_name="Momentum Only Co",
            price=10.0,
            previous_close=9.0,
            volume=10_000_000,
            avg_volume_20d=2_000_000,
            short_percent_float=5.0,
            short_ratio=1.0,
            shares_short=500_000,
            shares_short_prior_month=450_000,
            float_shares=8_000_000,
            market_cap=80_000_000,
            change_1d_pct=11.11,
            change_5d_pct=35.0,
            change_20d_pct=70.0,
            distance_from_52_week_high_pct=-2.0,
        )
    )

    assert result.components["short_interest"] == 0.0
    assert result.score < 50
    assert result.risk_level != "High squeeze setup"


def test_cached_provider_reuses_raw_data_for_one_hour(tmp_path):
    class CountingProvider:
        def __init__(self):
            self.calls = 0

        def fetch(self, symbol):
            self.calls += 1
            return TickerSnapshot(
                symbol=symbol,
                price=float(self.calls),
                volume=1_000_000,
                avg_volume_20d=1_000_000,
                short_percent_float=30.0,
                short_ratio=5.0,
                float_shares=50_000_000,
                change_5d_pct=5.0,
                change_20d_pct=10.0,
            )

    now = [1_000.0]
    provider = CountingProvider()
    cached_provider = CachedMarketDataProvider(provider, tmp_path / "market.sqlite3", clock=lambda: now[0])

    first = cached_provider.fetch("BYND")
    second = cached_provider.fetch("BYND")
    now[0] += 3_601
    third = cached_provider.fetch("BYND")

    assert provider.calls == 2
    assert first.price == 1.0
    assert second.price == 1.0
    assert third.price == 2.0


def test_cached_provider_updates_scan_time_without_refreshing_fresh_raw_data(tmp_path):
    class CountingProvider:
        def __init__(self):
            self.calls = 0

        def fetch(self, symbol):
            self.calls += 1
            return TickerSnapshot(symbol=symbol, price=float(self.calls))

    now = [1_000.0]
    db_path = tmp_path / "market.sqlite3"
    provider = CountingProvider()
    cached_provider = CachedMarketDataProvider(provider, db_path, clock=lambda: now[0])

    cached_provider.fetch("BYND")
    now[0] += 120
    cached_provider.fetch("BYND")

    with sqlite3.connect(db_path) as connection:
        fetched_at, scanned_at = connection.execute(
            "SELECT fetched_at, scanned_at FROM market_data_cache WHERE symbol = 'BYND'"
        ).fetchone()

    assert provider.calls == 1
    assert fetched_at == 1_000.0
    assert scanned_at == 1_120.0


def test_cached_provider_deletes_symbol_from_local_cache(tmp_path):
    class StaticProvider:
        def fetch(self, symbol):
            return TickerSnapshot(symbol=symbol, price=10.0)

    cached_provider = CachedMarketDataProvider(StaticProvider(), tmp_path / "market.sqlite3")
    cached_provider.fetch("BYND")

    assert cached_provider.delete("BYND") is True
    assert cached_provider.delete("BYND") is False
    assert cached_provider.recent_snapshots() == []


def test_cached_provider_stores_only_raw_market_snapshot_not_scores(tmp_path):
    class StaticProvider:
        def fetch(self, symbol):
            return TickerSnapshot(
                symbol=symbol,
                price=10.0,
                volume=1_000_000,
                avg_volume_20d=1_000_000,
                short_percent_float=30.0,
                short_ratio=5.0,
                float_shares=50_000_000,
                change_5d_pct=5.0,
                change_20d_pct=10.0,
            )

    db_path = tmp_path / "market.sqlite3"
    cached_provider = CachedMarketDataProvider(StaticProvider(), db_path)
    ScannerService(cached_provider).scan("BYND")

    with sqlite3.connect(db_path) as connection:
        payload_json = connection.execute("SELECT payload_json FROM market_data_cache").fetchone()[0]

    payload = json.loads(payload_json)
    assert payload["symbol"] == "BYND"
    assert payload["float_shares"] == 50_000_000
    assert payload["short_percent_float"] == 30.0
    assert "score" not in payload
    assert "risk_level" not in payload
    assert "components" not in payload
    assert "metrics" not in payload
    assert "rationale" not in payload


def test_build_scan_response_includes_minutes_since_scan():
    result = score_snapshot(TickerSnapshot(symbol="BYND", price=10.0))
    scanned_at = datetime.now(timezone.utc).timestamp() - 125

    payload = build_scan_response([result], scan_times={"BYND": scanned_at})

    assert payload["model"]["signals"] == SCORING_SIGNALS
    assert payload["results"][0]["scanned_at"] is not None
    assert payload["results"][0]["minutes_since_scan"] == 2


def test_scanner_service_sorts_results_and_reports_provider_errors():
    class StaticProvider:
        def fetch(self, symbol):
            if symbol == "BAD":
                raise DataProviderError("BAD: no data")
            return TickerSnapshot(
                symbol=symbol,
                price=10.0,
                volume=2_000_000 if symbol == "HIGH" else 1_000_000,
                avg_volume_20d=1_000_000,
                short_percent_float=40.0 if symbol == "HIGH" else 10.0,
                short_ratio=10.0 if symbol == "HIGH" else 1.0,
                float_shares=15_000_000 if symbol == "HIGH" else 500_000_000,
                change_5d_pct=20.0 if symbol == "HIGH" else -5.0,
                change_20d_pct=20.0 if symbol == "HIGH" else -10.0,
            )

    payload = ScannerService(StaticProvider(), max_workers=2).scan(["LOW", "BAD", "HIGH"])

    assert [result["symbol"] for result in payload["results"]] == ["HIGH", "LOW"]
    assert payload["errors"] == [{"symbol": "BAD", "message": "BAD: no data"}]
