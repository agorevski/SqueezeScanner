import json
from dataclasses import asdict
from datetime import datetime, timezone

from squeeze_scanner.cache import snapshot_from_json
from squeeze_scanner.domain import OptionChainRecord, OptionProviderCapabilities, TickerSnapshot
from squeeze_scanner.options import YAHOO_OPTION_PROXY_SOURCE
from squeeze_scanner.scoring import score_snapshot
from squeeze_scanner.service import build_scan_response


def _complete_snapshot(**overrides):
    field_quality_overrides = overrides.pop("field_quality_overrides", {})
    values = {
        "symbol": "TEST",
        "price": 20.0,
        "previous_close": 20.0,
        "volume": 2_000_000,
        "avg_volume_20d": 2_000_000,
        "avg_volume_90d": 1_800_000,
        "short_percent_float": 1.0,
        "short_ratio": 0.5,
        "shares_short": 100_000,
        "shares_short_prior_month": 100_000,
        "float_shares": 100_000_000,
        "market_cap": 1_000_000_000,
        "borrow_fee_pct": 0.0,
        "recent_reverse_split": False,
        "days_since_reverse_split": None,
        "reverse_split_ratio": None,
        "call_volume": 0.0,
        "put_volume": 0.0,
        "call_open_interest": 0.0,
        "put_open_interest": 0.0,
        "dealer_gamma_exposure_proxy": 0.0,
        "change_1d_pct": 0.0,
        "change_5d_pct": 0.0,
        "change_20d_pct": 0.0,
    }
    values.update(overrides)
    field_quality = {
        field: "present"
        for field, value in values.items()
        if field != "symbol" and value is not None
    }
    field_quality.update(field_quality_overrides)
    return TickerSnapshot(
        **values,
        source_fetched_at=datetime.now(timezone.utc).isoformat(),
        field_quality=field_quality,
    )


def test_missing_fields_reduce_model_confidence_without_crashing():
    complete = score_snapshot(_complete_snapshot())
    missing = score_snapshot(
        TickerSnapshot(
            symbol="MISS",
            price=5.0,
            volume=2_000_000,
            avg_volume_20d=1_000_000,
            source_fetched_at=datetime.now(timezone.utc).isoformat(),
        )
    )

    assert missing.model_confidence["classical_short_squeeze"] < complete.model_confidence["classical_short_squeeze"]
    assert missing.model_confidence["gamma_candidate"] < complete.model_confidence["gamma_candidate"]
    assert any("short interest missing" in item for item in missing.confidence_rationales["classical_short_squeeze"])


def test_low_liquidity_and_high_risk_names_are_flagged_not_silently_removed():
    result = score_snapshot(
        _complete_snapshot(
            symbol="RISK",
            price=0.50,
            volume=20_000,
            avg_volume_20d=30_000,
            market_cap=10_000_000,
            recent_reverse_split=True,
            days_since_reverse_split=12,
        )
    )

    flag_keys = {flag["key"] for flag in result.risk_flags}
    assert {"price_below_min", "low_dollar_volume", "low_liquidity", "low_market_cap", "recent_reverse_split"} <= flag_keys
    assert build_scan_response([result])["count"] == 1
    assert build_scan_response([result], guardrail_filter={"exclude_high_risk": True})["count"] == 0


def test_low_score_is_distinct_from_low_confidence():
    result = score_snapshot(_complete_snapshot())

    assert result.score < 30
    assert result.model_confidence["classical_short_squeeze"] >= 90
    assert result.risk_level == "Low"


def test_cached_older_json_remains_readable_with_new_fields():
    snapshot = snapshot_from_json(json.dumps({"symbol": "OLD", "price": 12.5}))

    assert snapshot.symbol == "OLD"
    assert snapshot.field_sources == {}
    assert snapshot.field_quality == {}
    assert snapshot.source_quality == {}
    assert snapshot.source_warnings == []
    assert snapshot.call_gamma_exposure is None
    assert snapshot.absolute_gamma_exposure is None
    assert snapshot.gamma_flip_price is None
    assert snapshot.option_chain_capabilities == {}
    assert snapshot.option_chain_records == []

    payload = score_snapshot(snapshot).to_dict()
    assert "model_confidence" in payload
    assert "confidence_rationales" in payload
    assert "risk_flags" in payload


def test_option_chain_contract_records_round_trip_through_cache_json():
    record = OptionChainRecord(
        symbol="OPT",
        contract_symbol="OPT260116C00010000",
        expiration="2026-01-16",
        days_to_expiration=45,
        days_to_expiry=45,
        strike=10.0,
        side="call",
        bid=1.1,
        ask=1.2,
        last_price=1.15,
        volume=100,
        open_interest=250,
        open_interest_change=25,
        implied_volatility=1.2,
        delta=0.55,
        gamma=0.04,
        timestamp="2026-06-10T14:00:00+00:00",
        provider="test_options",
        source="provider_chain",
    )
    snapshot = TickerSnapshot(
        symbol="OPT",
        option_chain_source="provider_chain",
        option_chain_provider="test_options",
        option_chain_fetched_at="2026-06-10T14:00:00+00:00",
        option_chain_freshness_seconds=30,
        option_chain_capabilities=OptionProviderCapabilities(
            open_interest=True,
            open_interest_change=True,
            implied_volatility=True,
            delta=True,
            gamma=True,
            true_gamma_exposure=True,
        ).to_dict(),
        option_chain_records=[record],
        call_gamma_exposure=1_500_000,
        put_gamma_exposure=-500_000,
        net_gamma_exposure=1_000_000,
        absolute_gamma_exposure=2_000_000,
        gamma_exposure_pct_market_cap=2.0,
        gamma_flip_price=9.5,
        gamma_flip_distance_pct=-5.0,
        max_gamma_strike=10.0,
        call_wall_strike=12.5,
        put_wall_strike=7.5,
        largest_gamma_expiration="2026-01-16",
        gamma_strike_concentration_pct=40.0,
        gamma_expiration_concentration_pct=55.0,
    )

    loaded = snapshot_from_json(json.dumps(asdict(snapshot)))

    assert loaded.option_chain_capabilities["gamma"] is True
    assert loaded.option_chain_records == [record]
    assert loaded.dealer_gamma_exposure_proxy is None
    assert loaded.absolute_gamma_exposure == 2_000_000


def test_score_metrics_keep_true_gamma_exposure_separate_from_yahoo_proxy():
    result = score_snapshot(
        _complete_snapshot(
            dealer_gamma_exposure_proxy=5_000_000,
            absolute_gamma_exposure=20_000_000,
            gamma_exposure_pct_market_cap=2.0,
            option_chain_source="provider_chain",
            option_chain_provider="test_options",
            option_chain_capabilities={"gamma": True, "true_gamma_exposure": True},
        )
    )

    assert result.metrics["dealer_gamma_exposure_proxy"] == 5_000_000
    assert result.metrics["dealer_gamma_exposure_pct_market_cap"] == 0.5
    assert result.metrics["absolute_gamma_exposure"] == 20_000_000
    assert result.metrics["gamma_exposure_pct_market_cap"] == 2.0
    assert result.metrics["option_chain_capabilities"] == {"gamma": True, "true_gamma_exposure": True}


def test_gamma_candidate_blends_true_gex_flip_walls_concentration_and_oi_change():
    result = score_snapshot(
        _complete_snapshot(
            price=20.0,
            market_cap=5_000_000,
            call_volume=25_000,
            put_volume=2_500,
            call_open_interest=30_000,
            put_open_interest=5_000,
            call_gamma_exposure=480_000,
            put_gamma_exposure=-60_000,
            net_gamma_exposure=420_000,
            absolute_gamma_exposure=540_000,
            gamma_exposure_pct_market_cap=10.8,
            gamma_flip_price=20.4,
            gamma_flip_distance_pct=2.0,
            max_gamma_strike=21.0,
            call_wall_strike=21.0,
            put_wall_strike=18.0,
            largest_gamma_expiration="2026-07-17",
            gamma_strike_concentration_pct=74.0,
            gamma_expiration_concentration_pct=82.0,
            option_chain_source="provider_chain",
            option_chain_provider="test_options",
            option_chain_capabilities={
                "gamma": True,
                "open_interest": True,
                "open_interest_change": True,
                "true_gamma_exposure": True,
            },
            option_chain_records=[
                OptionChainRecord(
                    symbol="TEST",
                    expiration="2026-07-17",
                    days_to_expiration=7,
                    strike=21.0,
                    side="call",
                    gamma=0.5,
                    open_interest=2_000,
                    open_interest_change=500,
                ),
                OptionChainRecord(
                    symbol="TEST",
                    expiration="2026-07-17",
                    days_to_expiration=7,
                    strike=24.0,
                    side="call",
                    gamma=0.2,
                    open_interest=1_000,
                    open_interest_change=200,
                ),
                OptionChainRecord(
                    symbol="TEST",
                    expiration="2026-07-17",
                    days_to_expiration=7,
                    strike=18.0,
                    side="put",
                    gamma=0.15,
                    open_interest=1_000,
                    open_interest_change=50,
                ),
            ],
        )
    )

    gamma_components = result.model_components["gamma_candidate"]
    assert gamma_components["dealer_gamma_exposure"] > 30
    assert gamma_components["gamma_flip_proximity"] > 0
    assert gamma_components["call_put_gamma_skew"] > 0
    assert gamma_components["gamma_concentration_walls"] > 0
    assert gamma_components["open_interest_change"] > 0
    assert result.metrics["gamma_exposure_source_type"] == "true_greeks"
    assert result.metrics["near_gamma_exposure"] == 540_000
    assert result.metrics["near_gamma_exposure_pct_market_cap"] == 10.8
    assert result.metrics["call_gamma_share_pct"] == 88.89
    assert result.metrics["call_wall_distance_pct"] == 5.0
    assert result.metrics["net_open_interest_change"] == 650
    assert any("provider-backed true gamma exposure" in item for item in result.model_rationales["gamma_candidate"])
    assert any("gamma flip" in item for item in result.model_rationales["gamma_candidate"])
    assert any("call wall" in item for item in result.model_rationales["gamma_candidate"])
    assert any("call OI change" in item for item in result.model_rationales["gamma_candidate"])


def test_yahoo_proxy_gamma_fallback_scores_but_has_lower_confidence_than_true_greeks():
    true_result = score_snapshot(
        _complete_snapshot(
            call_volume=25_000,
            put_volume=2_500,
            call_open_interest=30_000,
            put_open_interest=5_000,
            call_gamma_exposure=480_000,
            put_gamma_exposure=-60_000,
            net_gamma_exposure=420_000,
            absolute_gamma_exposure=540_000,
            gamma_exposure_pct_market_cap=10.8,
            gamma_flip_price=20.4,
            gamma_flip_distance_pct=2.0,
            max_gamma_strike=21.0,
            call_wall_strike=21.0,
            put_wall_strike=18.0,
            largest_gamma_expiration="2026-07-17",
            gamma_strike_concentration_pct=74.0,
            gamma_expiration_concentration_pct=82.0,
            option_chain_source="provider_chain",
            option_chain_provider="test_options",
            option_chain_capabilities={"gamma": True, "open_interest": True, "true_gamma_exposure": True},
        )
    )
    proxy_result = score_snapshot(
        _complete_snapshot(
            market_cap=200_000_000,
            call_volume=30_000,
            put_volume=2_000,
            call_open_interest=150_000,
            put_open_interest=25_000,
            dealer_gamma_exposure_proxy=30_000_000,
            option_chain_source=YAHOO_OPTION_PROXY_SOURCE,
            option_chain_provider="yahoo_finance",
            option_chain_capabilities={"open_interest": True},
            field_quality_overrides={"dealer_gamma_exposure_proxy": "estimated"},
        )
    )

    assert proxy_result.model_scores["gamma_candidate"] >= 75
    assert proxy_result.metrics["gamma_exposure_source_type"] == "proxy"
    assert proxy_result.metrics["gamma_exposure_is_proxy"] is True
    assert proxy_result.metrics["gamma_exposure_is_true"] is False
    assert proxy_result.metrics["gamma_exposure_pct_market_cap"] is None
    assert proxy_result.metrics["dealer_gamma_exposure_pct_market_cap"] == 15.0
    assert proxy_result.model_confidence["gamma_candidate"] < true_result.model_confidence["gamma_candidate"]
    assert any("not provider-backed true GEX" in item for item in proxy_result.confidence_rationales["gamma_candidate"])
    assert any("not true GEX" in item for item in proxy_result.model_rationales["gamma_candidate"])
