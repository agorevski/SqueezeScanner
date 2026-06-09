from __future__ import annotations

from typing import Any, Sequence

from .domain import ScanResult, TickerSnapshot

SCORING_MODEL_VERSION = "squeeze-v2"
SCORING_SIGNALS: list[dict[str, str | float]] = [
    {
        "key": "short_interest",
        "label": "Short interest",
        "weight": 35.0,
        "means": "Percent of tradable float currently sold short.",
        "calculation": (
            "Yahoo shortPercentOfFloat converted to percent; no points below 10%, "
            "then ramps through 20%, 40%, and 60%."
        ),
        "favorable": "Higher is more favorable; 40%+ is extreme squeeze fuel.",
    },
    {
        "key": "days_to_cover",
        "label": "Days cover",
        "weight": 20.0,
        "means": "Estimated trading days shorts may need to cover based on normal volume.",
        "calculation": "Yahoo shortRatio; no points below 2 days, then ramps through 5, 10, and 15 days.",
        "favorable": "Higher is more favorable because exits may be crowded.",
    },
    {
        "key": "float_pressure",
        "label": "Float pressure",
        "weight": 15.0,
        "means": "How constrained the tradable share supply is.",
        "calculation": (
            "Float shares bucketed from <=10M to >200M; market cap is used as a weaker fallback if "
            "float is missing."
        ),
        "favorable": "Lower float is more favorable because less supply can move faster.",
    },
    {
        "key": "momentum",
        "label": "Momentum",
        "weight": 15.0,
        "means": "Recent positive price movement that can pressure shorts.",
        "calculation": "Blend of 1-day, 5-day, 20-day price changes plus proximity to 52-week high.",
        "favorable": "Higher is more favorable because price strength can force covering.",
    },
    {
        "key": "relative_volume",
        "label": "Relative vol",
        "weight": 10.0,
        "means": "Current volume compared with recent average volume.",
        "calculation": "Current volume divided by 20-day average volume; points start above 1x and max near 5x.",
        "favorable": "Higher is more favorable because it suggests active demand/liquidity.",
    },
    {
        "key": "short_interest_trend",
        "label": "Short trend",
        "weight": 5.0,
        "means": "Change in shares short versus the prior month.",
        "calculation": "(sharesShort / sharesShortPriorMonth - 1) * 100; positive changes score up to 5 points.",
        "favorable": "Higher positive change is more favorable, but this is lightly weighted because short data is delayed.",
    },
]
SCORING_WEIGHTS = {str(signal["key"]): float(signal["weight"]) for signal in SCORING_SIGNALS}


def scoring_model_metadata() -> dict[str, Any]:
    return {
        "version": SCORING_MODEL_VERSION,
        "total_weight": sum(SCORING_WEIGHTS.values()),
        "weights": SCORING_WEIGHTS,
        "signals": SCORING_SIGNALS,
        "favorability_scale": [
            {"class": "signal-red", "label": "Red", "meaning": "Not favorable", "minimum_ratio": 0.0},
            {"class": "signal-orange", "label": "Orange", "meaning": "Somewhat favorable", "minimum_ratio": 0.25},
            {"class": "signal-yellow", "label": "Yellow", "meaning": "Favorable", "minimum_ratio": 0.5},
            {"class": "signal-green", "label": "Green", "meaning": "Very favorable", "minimum_ratio": 0.75},
        ],
    }


def score_snapshot(snapshot: TickerSnapshot) -> ScanResult:
    components = {
        "short_interest": _score_short_interest(snapshot.short_percent_float),
        "days_to_cover": _score_days_to_cover(snapshot.short_ratio),
        "relative_volume": _score_relative_volume(_relative_volume(snapshot)),
        "momentum": _score_momentum(snapshot),
        "float_pressure": _score_float_pressure(snapshot),
        "short_interest_trend": _score_short_trend(snapshot),
    }
    score = round(sum(components.values()), 1)
    data_quality = _data_quality(snapshot)

    warnings = list(snapshot.source_warnings)
    missing = _missing_core_fields(snapshot)
    if missing:
        warnings.append(f"Missing fields reduced confidence: {', '.join(missing)}")

    metrics = {
        "price": snapshot.price,
        "change_1d_pct": snapshot.change_1d_pct,
        "change_5d_pct": snapshot.change_5d_pct,
        "change_20d_pct": snapshot.change_20d_pct,
        "volume": snapshot.volume,
        "avg_volume_20d": snapshot.avg_volume_20d,
        "relative_volume": _relative_volume(snapshot),
        "short_percent_float": snapshot.short_percent_float,
        "short_ratio": snapshot.short_ratio,
        "shares_short": snapshot.shares_short,
        "shares_short_prior_month": snapshot.shares_short_prior_month,
        "short_interest_change_pct": _short_interest_change(snapshot),
        "float_shares": snapshot.float_shares,
        "market_cap": snapshot.market_cap,
        "distance_from_52_week_high_pct": snapshot.distance_from_52_week_high_pct,
    }

    return ScanResult(
        symbol=snapshot.symbol,
        company_name=snapshot.company_name,
        score=score,
        risk_level=_risk_level(score, data_quality),
        data_quality=data_quality,
        metrics={key: _round_optional(value) for key, value in metrics.items()},
        components={key: round(value, 1) for key, value in components.items()},
        rationale=_build_rationale(snapshot, components),
        warnings=warnings,
    )


def _score_short_interest(short_percent_float: float | None) -> float:
    return _piecewise_score(
        short_percent_float,
        (
            (0.0, 0.0),
            (10.0, 0.0),
            (20.0, 15.0),
            (40.0, 30.0),
            (60.0, SCORING_WEIGHTS["short_interest"]),
        ),
    )


def _score_days_to_cover(short_ratio: float | None) -> float:
    return _piecewise_score(
        short_ratio,
        (
            (0.0, 0.0),
            (2.0, 0.0),
            (5.0, 8.0),
            (10.0, 16.0),
            (15.0, SCORING_WEIGHTS["days_to_cover"]),
        ),
    )


def _score_relative_volume(relative_volume: float | None) -> float:
    return _piecewise_score(
        relative_volume,
        (
            (0.0, 0.0),
            (1.0, 0.0),
            (1.5, 3.0),
            (3.0, 8.0),
            (5.0, SCORING_WEIGHTS["relative_volume"]),
        ),
    )


def _score_momentum(snapshot: TickerSnapshot) -> float:
    score = 0.0
    if snapshot.change_1d_pct is not None:
        score += _piecewise_score(snapshot.change_1d_pct, ((0.0, 0.0), (3.0, 1.5), (10.0, 3.0)))
    if snapshot.change_5d_pct is not None:
        score += _piecewise_score(snapshot.change_5d_pct, ((0.0, 0.0), (5.0, 2.0), (15.0, 6.0), (30.0, 8.0)))
    if snapshot.change_20d_pct is not None:
        score += _piecewise_score(snapshot.change_20d_pct, ((0.0, 0.0), (10.0, 1.0), (30.0, 2.5), (60.0, 3.0)))
    if snapshot.distance_from_52_week_high_pct is not None:
        distance = abs(snapshot.distance_from_52_week_high_pct)
        if distance <= 5:
            score += 1.0
        elif distance <= 15:
            score += 0.5
    return min(score, SCORING_WEIGHTS["momentum"])


def _score_float_pressure(snapshot: TickerSnapshot) -> float:
    if snapshot.float_shares is not None:
        if snapshot.float_shares <= 10_000_000:
            return SCORING_WEIGHTS["float_pressure"]
        if snapshot.float_shares <= 25_000_000:
            return 12.0
        if snapshot.float_shares <= 50_000_000:
            return 9.0
        if snapshot.float_shares <= 100_000_000:
            return 6.0
        if snapshot.float_shares <= 200_000_000:
            return 3.0
        return 0.0

    if snapshot.market_cap is not None:
        if snapshot.market_cap <= 500_000_000:
            return 8.0
        if snapshot.market_cap <= 2_000_000_000:
            return 5.0
        if snapshot.market_cap <= 10_000_000_000:
            return 2.0
    return 0.0


def _score_short_trend(snapshot: TickerSnapshot) -> float:
    return _piecewise_score(
        _short_interest_change(snapshot),
        (
            (0.0, 0.0),
            (10.0, 2.0),
            (25.0, 4.0),
            (50.0, SCORING_WEIGHTS["short_interest_trend"]),
        ),
    )


def _risk_level(score: float, data_quality: float) -> str:
    if score >= 70 and data_quality >= 60:
        return "High squeeze setup"
    if score >= 50:
        return "Watchlist"
    if score >= 30:
        return "Emerging"
    return "Low"


def _data_quality(snapshot: TickerSnapshot) -> float:
    fields = [
        snapshot.price,
        snapshot.volume,
        snapshot.avg_volume_20d,
        snapshot.short_percent_float,
        snapshot.short_ratio,
        snapshot.float_shares if snapshot.float_shares is not None else snapshot.market_cap,
        snapshot.change_5d_pct,
        snapshot.change_20d_pct,
    ]
    return round(sum(1 for value in fields if value is not None) / len(fields) * 100.0, 1)


def _missing_core_fields(snapshot: TickerSnapshot) -> list[str]:
    field_labels = {
        "price": snapshot.price,
        "volume": snapshot.volume,
        "20-day average volume": snapshot.avg_volume_20d,
        "short % of float": snapshot.short_percent_float,
        "days to cover": snapshot.short_ratio,
        "float shares/market cap": snapshot.float_shares if snapshot.float_shares is not None else snapshot.market_cap,
        "5-day momentum": snapshot.change_5d_pct,
        "20-day momentum": snapshot.change_20d_pct,
    }
    return [label for label, value in field_labels.items() if value is None]


def _build_rationale(snapshot: TickerSnapshot, components: dict[str, float]) -> list[str]:
    rationale: list[str] = []
    if snapshot.short_percent_float is not None:
        rationale.append(f"{snapshot.short_percent_float:.1f}% of float sold short")
    if snapshot.short_ratio is not None:
        rationale.append(f"{snapshot.short_ratio:.1f} days to cover")

    relative_volume = _relative_volume(snapshot)
    if relative_volume is not None:
        rationale.append(f"{relative_volume:.1f}x relative volume")

    if snapshot.change_5d_pct is not None:
        direction = "up" if snapshot.change_5d_pct >= 0 else "down"
        rationale.append(f"{direction} {abs(snapshot.change_5d_pct):.1f}% over 5 trading days")

    short_change = _short_interest_change(snapshot)
    if short_change is not None:
        direction = "increased" if short_change >= 0 else "decreased"
        rationale.append(f"short interest {direction} {abs(short_change):.1f}% vs prior month")

    strongest = max(components.items(), key=lambda item: item[1])
    if strongest[1] > 0:
        rationale.append(f"largest score contributor: {strongest[0].replace('_', ' ')}")

    return rationale


def _relative_volume(snapshot: TickerSnapshot) -> float | None:
    if snapshot.volume is None or snapshot.avg_volume_20d is None or snapshot.avg_volume_20d <= 0:
        return None
    return snapshot.volume / snapshot.avg_volume_20d


def _short_interest_change(snapshot: TickerSnapshot) -> float | None:
    if (
        snapshot.shares_short is None
        or snapshot.shares_short_prior_month is None
        or snapshot.shares_short_prior_month <= 0
    ):
        return None
    return ((snapshot.shares_short / snapshot.shares_short_prior_month) - 1.0) * 100.0


def _round_optional(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


def _piecewise_score(value: float | None, curve: Sequence[tuple[float, float]]) -> float:
    if value is None:
        return 0.0
    if value <= curve[0][0]:
        return curve[0][1]

    for (lower_value, lower_score), (upper_value, upper_score) in zip(curve, curve[1:]):
        if value <= upper_value:
            span = upper_value - lower_value
            if span <= 0:
                return upper_score
            position = (value - lower_value) / span
            return lower_score + (upper_score - lower_score) * position

    return curve[-1][1]

