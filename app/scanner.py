from __future__ import annotations

import concurrent.futures
import math
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol, Sequence

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")
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


class ScannerError(Exception):
    """Base error for scanner failures."""


class InvalidSymbolError(ScannerError, ValueError):
    """Raised when ticker input cannot be normalized."""


class DataProviderError(ScannerError, RuntimeError):
    """Raised when market data cannot be retrieved for a ticker."""


class MarketDataProvider(Protocol):
    def fetch(self, symbol: str) -> "TickerSnapshot":
        """Return normalized market data for a symbol."""


@dataclass(frozen=True)
class TickerSnapshot:
    symbol: str
    company_name: str | None = None
    price: float | None = None
    previous_close: float | None = None
    volume: float | None = None
    avg_volume_20d: float | None = None
    avg_volume_90d: float | None = None
    short_percent_float: float | None = None
    short_ratio: float | None = None
    shares_short: float | None = None
    shares_short_prior_month: float | None = None
    float_shares: float | None = None
    market_cap: float | None = None
    change_1d_pct: float | None = None
    change_5d_pct: float | None = None
    change_20d_pct: float | None = None
    distance_from_52_week_high_pct: float | None = None
    source_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScanResult:
    symbol: str
    company_name: str | None
    score: float
    risk_level: str
    data_quality: float
    metrics: dict[str, float | None]
    components: dict[str, float]
    rationale: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class YahooFinanceProvider:
    """Fetches market data from Yahoo Finance through yfinance."""

    def fetch(self, symbol: str) -> TickerSnapshot:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise DataProviderError(
                "The yfinance package is required. Install dependencies with `uv sync`."
            ) from exc

        ticker = yf.Ticker(symbol)
        warnings: list[str] = []

        try:
            info = ticker.get_info() if hasattr(ticker, "get_info") else ticker.info
        except Exception as exc:  # yfinance raises transport/provider-specific exceptions.
            raise DataProviderError(f"{symbol}: Yahoo Finance quote data unavailable ({exc})") from exc

        if not isinstance(info, Mapping) or not info:
            raise DataProviderError(f"{symbol}: Yahoo Finance returned no quote data")

        history = None
        try:
            history = ticker.history(period="3mo", interval="1d", auto_adjust=False)
        except Exception as exc:  # Keep quote data but make the missing history visible.
            warnings.append(f"Price history unavailable: {exc}")

        close_values = _series_values(history, "Close")
        volume_values = _series_values(history, "Volume")

        price = _first_number(
            info,
            "currentPrice",
            "regularMarketPrice",
            "postMarketPrice",
            "preMarketPrice",
        ) or _last(close_values)
        previous_close = _first_number(info, "previousClose", "regularMarketPreviousClose") or _previous(
            close_values
        )
        volume = _first_number(info, "volume", "regularMarketVolume") or _last(volume_values)
        avg_volume_20d = _mean_tail(volume_values, 20) or _first_number(
            info,
            "averageDailyVolume10Day",
            "averageVolume10days",
            "averageVolume",
        )
        avg_volume_90d = _mean_tail(volume_values, 90) or _first_number(
            info,
            "averageVolume",
            "averageDailyVolume3Month",
        )

        if price is None and volume is None and _first_number(info, "shortPercentOfFloat") is None:
            raise DataProviderError(f"{symbol}: no usable price, volume, or short interest data returned")

        fifty_two_week_high = _first_number(info, "fiftyTwoWeekHigh", "52WeekChange")
        distance_from_high = None
        if price is not None and fifty_two_week_high and fifty_two_week_high > 0:
            distance_from_high = ((price / fifty_two_week_high) - 1.0) * 100.0

        return TickerSnapshot(
            symbol=symbol,
            company_name=_first_text(info, "shortName", "longName", "displayName"),
            price=price,
            previous_close=previous_close,
            volume=volume,
            avg_volume_20d=avg_volume_20d,
            avg_volume_90d=avg_volume_90d,
            short_percent_float=_as_percent(_first_number(info, "shortPercentOfFloat")),
            short_ratio=_first_number(info, "shortRatio"),
            shares_short=_first_number(info, "sharesShort"),
            shares_short_prior_month=_first_number(info, "sharesShortPriorMonth"),
            float_shares=_first_number(info, "floatShares", "sharesFloat"),
            market_cap=_first_number(info, "marketCap"),
            change_1d_pct=_pct_change(price, previous_close),
            change_5d_pct=_history_pct_change(close_values, 5),
            change_20d_pct=_history_pct_change(close_values, 20),
            distance_from_52_week_high_pct=distance_from_high,
            source_warnings=warnings,
        )


class ScannerService:
    def __init__(self, provider: MarketDataProvider, max_workers: int = 5) -> None:
        self.provider = provider
        self.max_workers = max_workers

    def scan(self, raw_symbols: str | Sequence[str], max_symbols: int = 25) -> dict[str, Any]:
        symbols = normalize_symbols(raw_symbols, max_symbols=max_symbols)
        results: list[ScanResult] = []
        errors: list[dict[str, str]] = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(self.max_workers, len(symbols))) as executor:
            futures = {executor.submit(self.provider.fetch, symbol): symbol for symbol in symbols}
            for future in concurrent.futures.as_completed(futures):
                symbol = futures[future]
                try:
                    snapshot = future.result()
                except DataProviderError as exc:
                    errors.append({"symbol": symbol, "message": str(exc)})
                    continue
                except Exception as exc:
                    errors.append({"symbol": symbol, "message": f"{symbol}: unexpected scanner error ({exc})"})
                    continue

                results.append(score_snapshot(snapshot))

        return build_scan_response(results, errors, scan_times=_scan_times(self.provider, results))


def normalize_symbols(raw_symbols: str | Sequence[str], max_symbols: int = 25) -> list[str]:
    if isinstance(raw_symbols, str):
        candidates = _split_symbols([raw_symbols])
    elif isinstance(raw_symbols, Iterable):
        candidates = _split_symbols(str(item) for item in raw_symbols)
    else:
        raise InvalidSymbolError("Enter one or more ticker symbols.")

    symbols: list[str] = []
    seen: set[str] = set()
    invalid: list[str] = []

    for candidate in candidates:
        symbol = candidate.upper()
        if not SYMBOL_PATTERN.match(symbol):
            invalid.append(candidate)
            continue
        if symbol not in seen:
            symbols.append(symbol)
            seen.add(symbol)

    if invalid:
        raise InvalidSymbolError(f"Invalid ticker symbol(s): {', '.join(invalid)}")
    if not symbols:
        raise InvalidSymbolError("Enter one or more ticker symbols.")
    if len(symbols) > max_symbols:
        raise InvalidSymbolError(f"Scan up to {max_symbols} symbols at a time.")
    return symbols


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

    rationale = _build_rationale(snapshot, components)
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
        rationale=rationale,
        warnings=warnings,
    )


def build_scan_response(
    results: Sequence[ScanResult],
    errors: Sequence[dict[str, str]] | None = None,
    scan_times: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    ranked_results = sorted(results, key=lambda result: (result.score, result.data_quality), reverse=True)
    generated_at = datetime.now(timezone.utc)
    return {
        "model": scoring_model_metadata(),
        "generated_at": generated_at.isoformat(),
        "count": len(ranked_results),
        "results": [_result_with_scan_time(result, scan_times or {}, generated_at) for result in ranked_results],
        "errors": sorted(errors or [], key=lambda error: error["symbol"]),
    }


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


def _scan_times(provider: MarketDataProvider, results: Sequence[ScanResult]) -> dict[str, float]:
    get_scan_times = getattr(provider, "scan_times", None)
    if not callable(get_scan_times):
        return {}
    return get_scan_times([result.symbol for result in results])


def _result_with_scan_time(
    result: ScanResult,
    scan_times: Mapping[str, float],
    generated_at: datetime,
) -> dict[str, Any]:
    result_payload = result.to_dict()
    scanned_at = scan_times.get(result.symbol)
    if scanned_at is None:
        result_payload["scanned_at"] = None
        result_payload["minutes_since_scan"] = None
        return result_payload

    scanned_at_datetime = datetime.fromtimestamp(scanned_at, timezone.utc)
    result_payload["scanned_at"] = scanned_at_datetime.isoformat()
    result_payload["minutes_since_scan"] = max(0, int((generated_at - scanned_at_datetime).total_seconds() // 60))
    return result_payload


def _split_symbols(values: Iterable[str]) -> list[str]:
    symbols: list[str] = []
    for value in values:
        symbols.extend(part.strip() for part in re.split(r"[\s,;]+", value) if part.strip())
    return symbols


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
    change = _short_interest_change(snapshot)
    return _piecewise_score(
        change,
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
    present = sum(1 for value in fields if value is not None)
    return round(present / len(fields) * 100.0, 1)


def _missing_core_fields(snapshot: TickerSnapshot) -> list[str]:
    missing: list[str] = []
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
    for label, value in field_labels.items():
        if value is None:
            missing.append(label)
    return missing


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


def _pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return ((current / previous) - 1.0) * 100.0


def _history_pct_change(values: Sequence[float], periods: int) -> float | None:
    if len(values) <= periods:
        return None
    return _pct_change(values[-1], values[-periods - 1])


def _series_values(history: Any, column: str) -> list[float]:
    if history is None or getattr(history, "empty", True) or column not in history:
        return []
    values: list[float] = []
    for value in history[column].dropna().tolist():
        number = _to_float(value)
        if number is not None:
            values.append(number)
    return values


def _mean_tail(values: Sequence[float], count: int) -> float | None:
    if not values:
        return None
    tail = values[-count:]
    return sum(tail) / len(tail)


def _last(values: Sequence[float]) -> float | None:
    return values[-1] if values else None


def _previous(values: Sequence[float]) -> float | None:
    return values[-2] if len(values) >= 2 else None


def _first_number(data: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _to_float(data.get(key))
        if value is not None:
            return value
    return None


def _first_text(data: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("%", "").strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _as_percent(value: float | None) -> float | None:
    if value is None:
        return None
    if -1.0 <= value <= 1.0:
        return value * 100.0
    return value


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
