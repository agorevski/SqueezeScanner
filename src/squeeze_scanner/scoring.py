from __future__ import annotations

from typing import Any, Sequence

from .domain import ScanResult, TickerSnapshot

SCORING_MODEL_VERSION = "squeeze-v3"
SCORE_MAX = 100.0

SCORING_MODELS: list[dict[str, Any]] = [
    {
        "key": "classical_short_squeeze",
        "category": "Category 1",
        "label": "Classical Short Squeeze",
        "definition": (
            "Names with a large short base, expensive borrow when a borrow-fee feed is available, "
            "and crowded short exits measured by days to cover."
        ),
        "signals": [
            {
                "key": "short_interest",
                "label": "Short interest",
                "weight": 40.0,
                "means": "Percent of tradable float currently sold short.",
                "calculation": (
                    "Yahoo shortPercentOfFloat converted to percent; no points below 10%, "
                    "then ramps through 20%, 40%, and 60%."
                ),
                "favorable": "Higher is more favorable; 40%+ is extreme squeeze fuel.",
            },
            {
                "key": "borrow_fee",
                "label": "Borrow fee",
                "weight": 25.0,
                "means": "Annualized cost to borrow shares for shorting.",
                "calculation": (
                    "Uses borrow_fee_pct when populated by a securities-lending data source; "
                    "Yahoo Finance does not provide this field, so missing values score zero."
                ),
                "favorable": "Higher is more favorable because expensive borrow pressures shorts.",
            },
            {
                "key": "days_to_cover",
                "label": "Days to cover",
                "weight": 35.0,
                "means": "Estimated trading days shorts may need to cover based on normal volume.",
                "calculation": "Yahoo shortRatio; no points below 2 days, then ramps through 5, 10, and 15 days.",
                "favorable": "Higher is more favorable because exits may be crowded.",
            },
        ],
    },
    {
        "key": "float_compression",
        "category": "Category 2",
        "label": "Float Compression",
        "definition": (
            "Tiny-float names where a recent reverse split or similar supply compression can combine "
            "with rapidly rising volume."
        ),
        "signals": [
            {
                "key": "tiny_float",
                "label": "Tiny float",
                "weight": 45.0,
                "means": "How constrained the tradable share supply is.",
                "calculation": (
                    "Float shares bucketed from <=5M to >100M; market cap is used as a weaker fallback "
                    "if float is missing."
                ),
                "favorable": "Lower float is more favorable because less supply can move faster.",
            },
            {
                "key": "recent_reverse_split",
                "label": "Recent reverse split",
                "weight": 25.0,
                "means": "Whether the stock recently reduced its share count through a reverse split.",
                "calculation": (
                    "Scores reverse splits detected within the last 180 days, with the strongest score "
                    "inside 30 days."
                ),
                "favorable": "More recent reverse splits are more favorable for float-compression setups.",
            },
            {
                "key": "relative_volume",
                "label": "Rapid volume increase",
                "weight": 30.0,
                "means": "Current volume compared with recent average volume.",
                "calculation": "Current volume divided by 20-day average volume; points start above 1x and max near 10x.",
                "favorable": "Higher is more favorable because it suggests active demand/liquidity.",
            },
        ],
    },
    {
        "key": "gamma_candidate",
        "category": "Category 3",
        "label": "Gamma Candidate",
        "definition": (
            "Options-driven names where heavy call buying and large public-options gamma exposure may "
            "force dealer hedging flows."
        ),
        "signals": [
            {
                "key": "call_buying",
                "label": "Heavy call buying",
                "weight": 50.0,
                "means": "Call-option volume and call/put volume skew from the available option chain.",
                "calculation": (
                    "Scores total call volume and adds credit for call volume that materially exceeds put volume."
                ),
                "favorable": "Higher call volume and stronger call/put skew are more favorable.",
            },
            {
                "key": "dealer_gamma_exposure",
                "label": "Dealer gamma exposure",
                "weight": 50.0,
                "means": "A public-data proxy for option exposure that may require dealer hedging.",
                "calculation": (
                    "Uses dealer_gamma_exposure_proxy when available; the Yahoo adapter estimates this from "
                    "near-the-money option open interest, while true dealer positioning requires specialist data."
                ),
                "favorable": "Higher exposure relative to market cap is more favorable.",
            },
        ],
    },
    {
        "key": "hybrid",
        "category": "Category 4",
        "label": "Hybrid",
        "definition": (
            "Rare names that combine tiny float, high short interest, expensive borrow, and elevated "
            "options activity."
        ),
        "signals": [
            {
                "key": "tiny_float",
                "label": "Tiny float",
                "weight": 25.0,
                "means": "How constrained the tradable share supply is.",
                "calculation": "Float-share buckets from the Float Compression model, scaled to this model's weight.",
                "favorable": "Lower float is more favorable.",
            },
            {
                "key": "short_interest",
                "label": "Short interest",
                "weight": 25.0,
                "means": "Percent of tradable float currently sold short.",
                "calculation": "Short-interest curve from the Classical Short Squeeze model, scaled to this model's weight.",
                "favorable": "Higher is more favorable.",
            },
            {
                "key": "borrow_fee",
                "label": "Borrow fee",
                "weight": 25.0,
                "means": "Annualized cost to borrow shares for shorting.",
                "calculation": (
                    "Uses borrow_fee_pct when populated by a securities-lending data source; "
                    "missing values score zero."
                ),
                "favorable": "Higher is more favorable.",
            },
            {
                "key": "options_activity",
                "label": "Options activity",
                "weight": 25.0,
                "means": "Call volume, call/put skew, and call open interest.",
                "calculation": "Blends the call-buying score with total call open interest.",
                "favorable": "Higher call activity and open interest are more favorable.",
            },
        ],
    },
]

SCORING_MODEL_WEIGHTS: dict[str, dict[str, float]] = {
    str(model["key"]): {str(signal["key"]): float(signal["weight"]) for signal in model["signals"]}
    for model in SCORING_MODELS
}
SCORING_SIGNALS: list[dict[str, Any]] = [
    {
        **signal,
        "model_key": model["key"],
        "model_label": model["label"],
        "model_category": model["category"],
    }
    for model in SCORING_MODELS
    for signal in model["signals"]
]


def scoring_model_metadata() -> dict[str, Any]:
    return {
        "version": SCORING_MODEL_VERSION,
        "score_range": {"minimum": 0.0, "maximum": SCORE_MAX},
        "total_weight": SCORE_MAX,
        "models": SCORING_MODELS,
        "model_weights": SCORING_MODEL_WEIGHTS,
        "signals": SCORING_SIGNALS,
        "favorability_scale": [
            {"class": "signal-red", "label": "Red", "meaning": "Not favorable", "minimum_ratio": 0.0},
            {"class": "signal-orange", "label": "Orange", "meaning": "Somewhat favorable", "minimum_ratio": 0.25},
            {"class": "signal-yellow", "label": "Yellow", "meaning": "Favorable", "minimum_ratio": 0.5},
            {"class": "signal-green", "label": "Green", "meaning": "Very favorable", "minimum_ratio": 0.75},
        ],
    }


def score_snapshot(snapshot: TickerSnapshot) -> ScanResult:
    model_components = _score_models(snapshot)
    model_scores = {
        model_key: round(sum(components.values()), 1)
        for model_key, components in model_components.items()
    }
    primary_model = max(model_scores, key=lambda model_key: model_scores[model_key])
    score = model_scores[primary_model]
    data_quality = _data_quality(snapshot)

    warnings = list(snapshot.source_warnings)
    missing = _missing_core_fields(snapshot)
    if missing:
        warnings.append(f"Missing fields reduced confidence: {', '.join(missing)}")

    metrics: dict[str, float | bool | None] = {
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
        "borrow_fee_pct": snapshot.borrow_fee_pct,
        "recent_reverse_split": snapshot.recent_reverse_split,
        "days_since_reverse_split": snapshot.days_since_reverse_split,
        "reverse_split_ratio": snapshot.reverse_split_ratio,
        "call_volume": snapshot.call_volume,
        "put_volume": snapshot.put_volume,
        "call_put_volume_ratio": _call_put_volume_ratio(snapshot),
        "call_open_interest": snapshot.call_open_interest,
        "put_open_interest": snapshot.put_open_interest,
        "dealer_gamma_exposure_proxy": snapshot.dealer_gamma_exposure_proxy,
        "dealer_gamma_exposure_pct_market_cap": _dealer_gamma_pct_market_cap(snapshot),
        "distance_from_52_week_high_pct": snapshot.distance_from_52_week_high_pct,
    }
    rounded_components = {
        model_key: {key: round(value, 1) for key, value in components.items()}
        for model_key, components in model_components.items()
    }
    model_rationales = {
        str(model["key"]): _build_model_rationale(snapshot, str(model["key"]))
        for model in SCORING_MODELS
    }

    return ScanResult(
        symbol=snapshot.symbol,
        company_name=snapshot.company_name,
        score=score,
        risk_level=_risk_level(score, data_quality),
        data_quality=data_quality,
        primary_model=primary_model,
        model_scores=model_scores,
        model_components=rounded_components,
        model_rationales=model_rationales,
        metrics={key: _round_metric(value) for key, value in metrics.items()},
        components=rounded_components[primary_model],
        rationale=_build_summary_rationale(snapshot, rounded_components[primary_model]),
        warnings=warnings,
    )


def _score_models(snapshot: TickerSnapshot) -> dict[str, dict[str, float]]:
    classical_weights = SCORING_MODEL_WEIGHTS["classical_short_squeeze"]
    float_weights = SCORING_MODEL_WEIGHTS["float_compression"]
    gamma_weights = SCORING_MODEL_WEIGHTS["gamma_candidate"]
    hybrid_weights = SCORING_MODEL_WEIGHTS["hybrid"]

    return {
        "classical_short_squeeze": {
            "short_interest": _score_short_interest(snapshot.short_percent_float, classical_weights["short_interest"]),
            "borrow_fee": _score_borrow_fee(snapshot.borrow_fee_pct, classical_weights["borrow_fee"]),
            "days_to_cover": _score_days_to_cover(snapshot.short_ratio, classical_weights["days_to_cover"]),
        },
        "float_compression": {
            "tiny_float": _score_tiny_float(snapshot, float_weights["tiny_float"]),
            "recent_reverse_split": _score_recent_reverse_split(
                snapshot,
                float_weights["recent_reverse_split"],
            ),
            "relative_volume": _score_relative_volume(
                _relative_volume(snapshot),
                float_weights["relative_volume"],
            ),
        },
        "gamma_candidate": {
            "call_buying": _score_call_buying(snapshot, gamma_weights["call_buying"]),
            "dealer_gamma_exposure": _score_dealer_gamma_exposure(
                snapshot,
                gamma_weights["dealer_gamma_exposure"],
            ),
        },
        "hybrid": {
            "tiny_float": _score_tiny_float(snapshot, hybrid_weights["tiny_float"]),
            "short_interest": _score_short_interest(snapshot.short_percent_float, hybrid_weights["short_interest"]),
            "borrow_fee": _score_borrow_fee(snapshot.borrow_fee_pct, hybrid_weights["borrow_fee"]),
            "options_activity": _score_options_activity(snapshot, hybrid_weights["options_activity"]),
        },
    }


def _score_short_interest(short_percent_float: float | None, weight: float) -> float:
    return _piecewise_score(
        short_percent_float,
        (
            (0.0, 0.0),
            (10.0, 0.0),
            (20.0, weight * 0.35),
            (40.0, weight * 0.8),
            (60.0, weight),
        ),
    )


def _score_borrow_fee(borrow_fee_pct: float | None, weight: float) -> float:
    return _piecewise_score(
        borrow_fee_pct,
        (
            (0.0, 0.0),
            (5.0, 0.0),
            (20.0, weight * 0.4),
            (50.0, weight * 0.8),
            (100.0, weight),
        ),
    )


def _score_days_to_cover(short_ratio: float | None, weight: float) -> float:
    return _piecewise_score(
        short_ratio,
        (
            (0.0, 0.0),
            (2.0, 0.0),
            (5.0, weight * 0.35),
            (10.0, weight * 0.75),
            (15.0, weight),
        ),
    )


def _score_tiny_float(snapshot: TickerSnapshot, weight: float) -> float:
    if snapshot.float_shares is not None:
        if snapshot.float_shares <= 5_000_000:
            return weight
        if snapshot.float_shares <= 10_000_000:
            return weight * 0.85
        if snapshot.float_shares <= 25_000_000:
            return weight * 0.65
        if snapshot.float_shares <= 50_000_000:
            return weight * 0.4
        if snapshot.float_shares <= 100_000_000:
            return weight * 0.2
        return 0.0

    if snapshot.market_cap is not None:
        if snapshot.market_cap <= 100_000_000:
            return weight * 0.55
        if snapshot.market_cap <= 500_000_000:
            return weight * 0.35
        if snapshot.market_cap <= 2_000_000_000:
            return weight * 0.2
    return 0.0


def _score_recent_reverse_split(snapshot: TickerSnapshot, weight: float) -> float:
    if snapshot.recent_reverse_split is not True:
        return 0.0
    if snapshot.days_since_reverse_split is None:
        return weight * 0.7
    if snapshot.days_since_reverse_split <= 30:
        return weight
    if snapshot.days_since_reverse_split <= 90:
        return weight * 0.75
    if snapshot.days_since_reverse_split <= 180:
        return weight * 0.4
    return 0.0


def _score_relative_volume(relative_volume: float | None, weight: float) -> float:
    return _piecewise_score(
        relative_volume,
        (
            (0.0, 0.0),
            (1.0, 0.0),
            (2.0, weight * 0.35),
            (5.0, weight * 0.75),
            (10.0, weight),
        ),
    )


def _score_call_buying(snapshot: TickerSnapshot, weight: float) -> float:
    if snapshot.call_volume is None:
        return 0.0

    volume_score = _piecewise_score(
        snapshot.call_volume,
        (
            (0.0, 0.0),
            (100.0, weight * 0.15),
            (1_000.0, weight * 0.4),
            (5_000.0, weight * 0.7),
            (20_000.0, weight * 0.85),
        ),
    )
    ratio_score = _piecewise_score(
        _call_put_volume_ratio(snapshot),
        (
            (0.0, 0.0),
            (1.0, 0.0),
            (2.0, weight * 0.04),
            (5.0, weight * 0.1),
            (10.0, weight * 0.15),
        ),
    )
    return min(weight, volume_score + ratio_score)


def _score_dealer_gamma_exposure(snapshot: TickerSnapshot, weight: float) -> float:
    if snapshot.dealer_gamma_exposure_proxy is None:
        return 0.0

    exposure_pct_market_cap = _dealer_gamma_pct_market_cap(snapshot)
    if exposure_pct_market_cap is not None:
        return _piecewise_score(
            exposure_pct_market_cap,
            (
                (0.0, 0.0),
                (1.0, weight * 0.2),
                (3.0, weight * 0.45),
                (7.0, weight * 0.75),
                (12.0, weight),
            ),
        )

    return _piecewise_score(
        snapshot.dealer_gamma_exposure_proxy,
        (
            (0.0, 0.0),
            (10_000_000.0, weight * 0.2),
            (50_000_000.0, weight * 0.45),
            (250_000_000.0, weight * 0.75),
            (1_000_000_000.0, weight),
        ),
    )


def _score_options_activity(snapshot: TickerSnapshot, weight: float) -> float:
    call_score = _score_call_buying(snapshot, weight * 0.7)
    open_interest_score = _piecewise_score(
        snapshot.call_open_interest,
        (
            (0.0, 0.0),
            (1_000.0, weight * 0.05),
            (10_000.0, weight * 0.15),
            (50_000.0, weight * 0.25),
            (100_000.0, weight * 0.3),
        ),
    )
    return min(weight, call_score + open_interest_score)


def _risk_level(score: float, data_quality: float) -> str:
    if score >= 70 and data_quality >= 60:
        return "High setup"
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


def _build_model_rationale(snapshot: TickerSnapshot, model_key: str) -> list[str]:
    return [_signal_rationale(snapshot, str(signal["key"])) for signal in _signals_for_model(model_key)]


def _build_summary_rationale(snapshot: TickerSnapshot, components: dict[str, float]) -> list[str]:
    rationale = [
        _signal_rationale(snapshot, key)
        for key in components
    ]

    strongest = max(components.items(), key=lambda item: item[1])
    if strongest[1] > 0:
        rationale.append(f"largest score contributor: {strongest[0].replace('_', ' ')}")

    return rationale


def _signal_rationale(snapshot: TickerSnapshot, key: str) -> str:
    if key == "short_interest":
        if snapshot.short_percent_float is None:
            return "short interest unavailable"
        return f"{snapshot.short_percent_float:.1f}% of float sold short"

    if key == "borrow_fee":
        if snapshot.borrow_fee_pct is None:
            return "borrow fee unavailable from current Yahoo data"
        return f"{snapshot.borrow_fee_pct:.1f}% annualized borrow fee"

    if key == "days_to_cover":
        if snapshot.short_ratio is None:
            return "days to cover unavailable"
        return f"{snapshot.short_ratio:.1f} days to cover"

    if key == "tiny_float":
        if snapshot.float_shares is not None:
            return f"{_format_large_number(snapshot.float_shares)} float shares"
        if snapshot.market_cap is not None:
            return f"{_format_large_number(snapshot.market_cap)} market-cap fallback"
        return "float shares and market cap unavailable"

    if key == "recent_reverse_split":
        if snapshot.recent_reverse_split is True:
            if snapshot.days_since_reverse_split is None:
                return "recent reverse split detected"
            ratio_text = (
                f" at {snapshot.reverse_split_ratio:.3g} split ratio"
                if snapshot.reverse_split_ratio is not None
                else ""
            )
            return f"reverse split detected {snapshot.days_since_reverse_split:.0f} days ago{ratio_text}"
        if snapshot.recent_reverse_split is False:
            return "no recent reverse split detected"
        return "reverse split history unavailable"

    if key == "relative_volume":
        relative_volume = _relative_volume(snapshot)
        if relative_volume is None:
            return "relative volume unavailable"
        return f"{relative_volume:.1f}x relative volume"

    if key == "call_buying":
        if snapshot.call_volume is None:
            return "call-option volume unavailable"
        ratio = _call_put_volume_ratio(snapshot)
        ratio_text = f", {ratio:.1f}x call/put volume" if ratio is not None else ""
        return f"{_format_large_number(snapshot.call_volume)} call volume{ratio_text}"

    if key == "dealer_gamma_exposure":
        if snapshot.dealer_gamma_exposure_proxy is None:
            return "dealer gamma exposure proxy unavailable"
        pct_market_cap = _dealer_gamma_pct_market_cap(snapshot)
        pct_text = f" ({pct_market_cap:.1f}% of market cap)" if pct_market_cap is not None else ""
        return f"{_format_large_number(snapshot.dealer_gamma_exposure_proxy)} public-options exposure proxy{pct_text}"

    if key == "options_activity":
        if snapshot.call_volume is None and snapshot.call_open_interest is None:
            return "options activity unavailable"
        call_volume = (
            _format_large_number(snapshot.call_volume)
            if snapshot.call_volume is not None
            else "N/A"
        )
        call_open_interest = (
            _format_large_number(snapshot.call_open_interest)
            if snapshot.call_open_interest is not None
            else "N/A"
        )
        return f"{call_volume} call volume and {call_open_interest} call open interest"

    return "signal unavailable"


def _signals_for_model(model_key: str) -> list[dict[str, Any]]:
    for model in SCORING_MODELS:
        if model["key"] == model_key:
            return list(model["signals"])
    return []


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


def _call_put_volume_ratio(snapshot: TickerSnapshot) -> float | None:
    if snapshot.call_volume is None:
        return None
    if snapshot.put_volume is None:
        return None
    if snapshot.put_volume <= 0:
        return snapshot.call_volume if snapshot.call_volume > 0 else None
    return snapshot.call_volume / snapshot.put_volume


def _dealer_gamma_pct_market_cap(snapshot: TickerSnapshot) -> float | None:
    if (
        snapshot.dealer_gamma_exposure_proxy is None
        or snapshot.market_cap is None
        or snapshot.market_cap <= 0
    ):
        return None
    return snapshot.dealer_gamma_exposure_proxy / snapshot.market_cap * 100.0


def _round_metric(value: float | bool | None) -> float | bool | None:
    if value is None or isinstance(value, bool):
        return value
    return round(value, 2)


def _format_large_number(value: float | None) -> str:
    if value is None:
        return "N/A"
    abs_value = abs(value)
    if abs_value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if abs_value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if abs_value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:.0f}"


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
