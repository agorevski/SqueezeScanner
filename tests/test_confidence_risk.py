import json
from datetime import datetime, timezone

from squeeze_scanner.cache import snapshot_from_json
from squeeze_scanner.domain import TickerSnapshot
from squeeze_scanner.scoring import score_snapshot
from squeeze_scanner.service import build_scan_response


def _complete_snapshot(**overrides):
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

    payload = score_snapshot(snapshot).to_dict()
    assert "model_confidence" in payload
    assert "confidence_rationales" in payload
    assert "risk_flags" in payload
