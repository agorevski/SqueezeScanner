from datetime import datetime, timezone

import pytest

from squeeze_scanner.domain import OptionChainRecord, TickerSnapshot
from squeeze_scanner.options import (
    YAHOO_OPTION_PROXY_SOURCE,
    aggregate_true_gamma_exposure,
    normalize_option_chain_records,
)
from squeeze_scanner.scoring import score_snapshot


def _record(
    *,
    side: str,
    strike: float,
    gamma: float | None,
    open_interest: float | None,
    expiration: str = "2026-01-16",
) -> OptionChainRecord:
    return OptionChainRecord(
        symbol="GEX",
        expiration=expiration,
        strike=strike,
        side=side,
        gamma=gamma,
        open_interest=open_interest,
        provider="test_options",
        source="provider_chain",
    )


def test_normalize_option_chain_records_accepts_provider_aliases():
    records = normalize_option_chain_records(
        [
            {
                "underlyingSymbol": "norm",
                "contractSymbol": "NORM260116C00010000",
                "expirationDate": "2026-01-16T00:00:00+00:00",
                "strikePrice": "10.00",
                "openInterest": "1,250",
                "impliedVolatility": "125%",
                "gamma": "0.04",
                "dte": "45",
            }
        ],
        provider="test_options",
        source="provider_chain",
    )

    assert len(records) == 1
    record = records[0]
    assert record.symbol == "NORM"
    assert record.expiration == "2026-01-16"
    assert record.side == "call"
    assert record.strike == 10.0
    assert record.open_interest == 1_250
    assert record.implied_volatility == 1.25
    assert record.days_to_expiration == 45
    assert record.provider == "test_options"


def test_true_gamma_aggregation_math_call_put_signs_and_concentration():
    aggregation = aggregate_true_gamma_exposure(
        [
            _record(side="call", strike=11.0, gamma=0.12, open_interest=100),
            _record(side="put", strike=9.0, gamma=0.06, open_interest=100),
        ],
        spot=10.0,
        market_cap=100_000,
    )

    assert aggregation.call_gamma_exposure == pytest.approx(1_200.0)
    assert aggregation.put_gamma_exposure == pytest.approx(-600.0)
    assert aggregation.net_gamma_exposure == pytest.approx(600.0)
    assert aggregation.absolute_gamma_exposure == pytest.approx(1_800.0)
    assert aggregation.gamma_exposure_pct_market_cap == pytest.approx(1.8)
    assert aggregation.max_gamma_strike == 11.0
    assert aggregation.call_wall_strike == 11.0
    assert aggregation.put_wall_strike == 9.0
    assert aggregation.largest_gamma_expiration == "2026-01-16"
    assert aggregation.gamma_strike_concentration_pct == pytest.approx(66.6666667)
    assert aggregation.gamma_expiration_concentration_pct == pytest.approx(100.0)
    assert aggregation.gamma_flip_price == pytest.approx(10.0)
    assert aggregation.gamma_flip_distance_pct == pytest.approx(0.0)


def test_true_gamma_aggregation_surfaces_missing_inputs_as_none():
    no_spot = aggregate_true_gamma_exposure(
        [_record(side="call", strike=10.0, gamma=0.05, open_interest=100)],
        spot=None,
        market_cap=100_000,
    )

    assert no_spot.call_gamma_exposure is None
    assert no_spot.absolute_gamma_exposure is None
    assert no_spot.missing_fields["spot"] == 1

    missing_contract_fields = aggregate_true_gamma_exposure(
        [
            _record(side="call", strike=10.0, gamma=None, open_interest=100),
            _record(side="put", strike=9.0, gamma=0.02, open_interest=None),
        ],
        spot=10.0,
        market_cap=100_000,
    )

    assert missing_contract_fields.valid_contract_count == 0
    assert missing_contract_fields.call_gamma_exposure is None
    assert missing_contract_fields.put_gamma_exposure is None
    assert missing_contract_fields.missing_fields == {"gamma": 1, "open_interest": 1}


def test_score_snapshot_derives_true_gamma_metrics_from_option_chain_records():
    result = score_snapshot(
        TickerSnapshot(
            symbol="GEX",
            price=10.0,
            volume=1_000_000,
            avg_volume_20d=1_000_000,
            call_volume=1_000,
            put_volume=500,
            call_open_interest=100,
            put_open_interest=100,
            market_cap=100_000,
            option_chain_provider="test_options",
            option_chain_source="provider_chain",
            option_chain_records=[
                _record(side="call", strike=11.0, gamma=0.12, open_interest=100),
                _record(side="put", strike=9.0, gamma=0.06, open_interest=100),
            ],
            source_fetched_at=datetime.now(timezone.utc).isoformat(),
        )
    )

    assert result.metrics["call_gamma_exposure"] == 1_200.0
    assert result.metrics["put_gamma_exposure"] == -600.0
    assert result.metrics["net_gamma_exposure"] == 600.0
    assert result.metrics["absolute_gamma_exposure"] == 1_800.0
    assert result.metrics["gamma_exposure_pct_market_cap"] == 1.8
    assert result.metrics["gamma_flip_price"] == 10.0
    assert result.metrics["max_gamma_strike"] == 11.0
    assert result.metrics["option_chain_contract_count"] == 2
    assert result.metrics["option_chain_capabilities"]["true_gamma_exposure"] is True
    assert result.field_sources["absolute_gamma_exposure"] == "provider_chain"
    assert result.field_quality["absolute_gamma_exposure"] == "present"


def test_yahoo_proxy_exposure_is_not_promoted_to_true_gex():
    result = score_snapshot(
        TickerSnapshot(
            symbol="YHOO",
            price=10.0,
            volume=1_000_000,
            avg_volume_20d=1_000_000,
            call_volume=1_000,
            put_volume=500,
            call_open_interest=100,
            put_open_interest=100,
            market_cap=100_000_000,
            dealer_gamma_exposure_proxy=5_000_000,
            option_chain_source=YAHOO_OPTION_PROXY_SOURCE,
            option_chain_provider="yahoo_finance",
            option_chain_capabilities={"open_interest": True},
            option_chain_records=[
                _record(side="call", strike=11.0, gamma=0.12, open_interest=100),
            ],
            field_quality={"dealer_gamma_exposure_proxy": "estimated"},
            source_fetched_at=datetime.now(timezone.utc).isoformat(),
        )
    )

    assert result.metrics["dealer_gamma_exposure_proxy"] == 5_000_000
    assert result.metrics["dealer_gamma_exposure_pct_market_cap"] == 5.0
    assert result.metrics["call_gamma_exposure"] is None
    assert result.metrics["absolute_gamma_exposure"] is None
    assert result.metrics["gamma_exposure_pct_market_cap"] is None
    assert result.metrics["option_chain_capabilities"] == {"open_interest": True}
