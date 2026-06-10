from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from squeeze_scanner.domain import DataProviderError, OptionProviderCapabilities, ScreenerError, TickerSnapshot

MOST_SHORTED_SCREENER = "most_shorted_stocks"
MAX_SCREENER_COUNT = 250
YAHOO_OPTION_SOURCE = "yahoo_finance_options_proxy"
YAHOO_OPTION_STALE_AFTER_SECONDS = 3_600.0
YAHOO_OPTION_CAPABILITIES = OptionProviderCapabilities(
    expiration_listing=True,
    strike_listing=True,
    bid_ask=True,
    last_price=True,
    volume=True,
    open_interest=True,
    implied_volatility=True,
).to_dict()


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
        except Exception as exc:
            raise DataProviderError(f"{symbol}: Yahoo Finance quote data unavailable ({exc})") from exc

        if not isinstance(info, Mapping) or not info:
            raise DataProviderError(f"{symbol}: Yahoo Finance returned no quote data")

        history = None
        try:
            history = ticker.history(period="3mo", interval="1d", auto_adjust=False)
        except Exception as exc:
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

        fetched_at = datetime.now(timezone.utc).isoformat()
        reverse_split_metrics, reverse_split_warnings = _reverse_split_metrics(ticker)
        warnings.extend(reverse_split_warnings)
        options_metrics, options_warnings = _options_metrics(ticker, price, fetched_at=fetched_at)
        warnings.extend(options_warnings)

        data_fields = {
            "price": price,
            "previous_close": previous_close,
            "volume": volume,
            "avg_volume_20d": avg_volume_20d,
            "avg_volume_90d": avg_volume_90d,
            "short_percent_float": _as_percent(_first_number(info, "shortPercentOfFloat")),
            "short_ratio": _first_number(info, "shortRatio"),
            "shares_short": _first_number(info, "sharesShort"),
            "shares_short_prior_month": _first_number(info, "sharesShortPriorMonth"),
            "float_shares": _first_number(info, "floatShares", "sharesFloat"),
            "market_cap": _first_number(info, "marketCap"),
            "borrow_fee_pct": None,
            "recent_reverse_split": reverse_split_metrics["recent_reverse_split"],
            "days_since_reverse_split": reverse_split_metrics["days_since_reverse_split"],
            "reverse_split_ratio": reverse_split_metrics["reverse_split_ratio"],
            "call_volume": options_metrics["call_volume"],
            "put_volume": options_metrics["put_volume"],
            "call_open_interest": options_metrics["call_open_interest"],
            "put_open_interest": options_metrics["put_open_interest"],
            "dealer_gamma_exposure_proxy": options_metrics["dealer_gamma_exposure_proxy"],
            "option_chain_source": options_metrics["option_chain_source"],
            "option_chain_provider": options_metrics["option_chain_provider"],
            "option_chain_fetched_at": options_metrics["option_chain_fetched_at"],
            "option_chain_freshness_seconds": options_metrics["option_chain_freshness_seconds"],
            "option_chain_stale_after_seconds": options_metrics["option_chain_stale_after_seconds"],
            "option_chain_capabilities": options_metrics["option_chain_capabilities"],
            "change_1d_pct": _pct_change(price, previous_close),
            "change_5d_pct": _history_pct_change(close_values, 5),
            "change_20d_pct": _history_pct_change(close_values, 20),
            "distance_from_52_week_high_pct": distance_from_high,
        }

        return TickerSnapshot(
            symbol=symbol,
            company_name=_first_text(info, "shortName", "longName", "displayName"),
            **data_fields,
            source_fetched_at=fetched_at,
            field_sources=_field_sources(data_fields),
            field_quality=_field_quality(data_fields),
            source_quality={
                "yahoo_finance": 70.0,
                "yahoo_finance_options_proxy": 55.0,
            },
            source_warnings=warnings,
        )


class YahooFinanceScreener:
    """Fetches Yahoo Finance predefined screener universes through yfinance."""

    def __init__(self, screen: Callable[[str, int], Mapping[str, Any]] | None = None) -> None:
        self._screen = screen

    def most_shorted_symbols(self, count: int = 100) -> list[str]:
        if count < 1 or count > MAX_SCREENER_COUNT:
            raise ScreenerError(f"Yahoo screener count must be between 1 and {MAX_SCREENER_COUNT}.")

        try:
            result = self._run_screen(MOST_SHORTED_SCREENER, count)
        except Exception as exc:
            raise ScreenerError(f"Yahoo most-shorted screener unavailable ({exc})") from exc

        quotes = result.get("quotes")
        if not isinstance(quotes, list):
            raise ScreenerError("Yahoo most-shorted screener returned no quotes.")

        symbols = _symbols_from_quotes(quotes)
        if not symbols:
            raise ScreenerError("Yahoo most-shorted screener returned no usable symbols.")
        return symbols

    def _run_screen(self, query: str, count: int) -> Mapping[str, Any]:
        if self._screen is not None:
            return self._screen(query, count)

        try:
            import yfinance as yf
        except ImportError as exc:
            raise ScreenerError(
                "The yfinance package is required. Install dependencies with `uv sync`."
            ) from exc
        return yf.screen(query, count=count)


def _symbols_from_quotes(quotes: Sequence[Mapping[str, Any]]) -> list[str]:
    symbols: list[str] = []
    seen: set[str] = set()
    for quote in quotes:
        symbol = quote.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            continue
        normalized = symbol.strip().upper()
        if normalized not in seen:
            symbols.append(normalized)
            seen.add(normalized)
    return symbols


def _field_sources(data_fields: Mapping[str, Any]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for field_name, value in data_fields.items():
        if not _has_value(value):
            continue
        sources[field_name] = (
            YAHOO_OPTION_SOURCE
            if field_name in _option_proxy_fields()
            else "yahoo_finance"
        )
    return sources


def _field_quality(data_fields: Mapping[str, Any]) -> dict[str, str]:
    estimated_fields = {"dealer_gamma_exposure_proxy"}
    quality: dict[str, str] = {}
    for field_name, value in data_fields.items():
        if not _has_value(value):
            quality[field_name] = "missing"
        elif field_name in estimated_fields:
            quality[field_name] = "estimated"
        else:
            quality[field_name] = "present"
    return quality


def _option_proxy_fields() -> set[str]:
    return {
        "dealer_gamma_exposure_proxy",
        "option_chain_source",
        "option_chain_provider",
        "option_chain_fetched_at",
        "option_chain_freshness_seconds",
        "option_chain_stale_after_seconds",
        "option_chain_capabilities",
    }


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (dict, list, tuple, set)):
        return bool(value)
    return True


def _reverse_split_metrics(ticker: Any, lookback_days: int = 180) -> tuple[dict[str, float | bool | None], list[str]]:
    empty = {
        "recent_reverse_split": None,
        "days_since_reverse_split": None,
        "reverse_split_ratio": None,
    }
    try:
        splits = ticker.splits
    except Exception as exc:
        return empty, [f"Split history unavailable: {exc}"]

    if splits is None or getattr(splits, "empty", True):
        return {**empty, "recent_reverse_split": False}, []

    now = datetime.now(timezone.utc)
    latest_reverse_split: tuple[float | None, float] | None = None
    for index, value in splits.items():
        ratio = _to_float(value)
        if ratio is None or ratio >= 1.0:
            continue
        split_datetime = _to_datetime(index)
        days_since_split = (now - split_datetime).days if split_datetime is not None else None
        if latest_reverse_split is None:
            latest_reverse_split = (days_since_split, ratio)
            continue
        current_days = latest_reverse_split[0]
        if days_since_split is not None and (current_days is None or days_since_split < current_days):
            latest_reverse_split = (days_since_split, ratio)

    if latest_reverse_split is None:
        return {**empty, "recent_reverse_split": False}, []

    days_since_split, ratio = latest_reverse_split
    is_recent = days_since_split is None or days_since_split <= lookback_days
    return {
        "recent_reverse_split": is_recent,
        "days_since_reverse_split": float(days_since_split) if days_since_split is not None else None,
        "reverse_split_ratio": ratio,
    }, []


def _options_metrics(
    ticker: Any,
    price: float | None,
    *,
    fetched_at: str | None = None,
    max_expirations: int = 2,
) -> tuple[dict[str, Any], list[str]]:
    empty = {
        "call_volume": None,
        "put_volume": None,
        "call_open_interest": None,
        "put_open_interest": None,
        "dealer_gamma_exposure_proxy": None,
        "option_chain_source": None,
        "option_chain_provider": None,
        "option_chain_fetched_at": None,
        "option_chain_freshness_seconds": None,
        "option_chain_stale_after_seconds": None,
        "option_chain_capabilities": {},
    }
    try:
        expirations = list(getattr(ticker, "options", ()) or ())
    except Exception as exc:
        return empty, [f"Options expirations unavailable: {exc}"]

    if not expirations:
        return empty, []

    call_volume = 0.0
    put_volume = 0.0
    call_open_interest = 0.0
    put_open_interest = 0.0
    dealer_gamma_exposure_proxy = 0.0
    populated = False
    warnings: list[str] = []

    for expiration in expirations[:max_expirations]:
        try:
            chain = ticker.option_chain(expiration)
        except Exception as exc:
            warnings.append(f"Options chain unavailable for {expiration}: {exc}")
            continue

        calls = getattr(chain, "calls", None)
        puts = getattr(chain, "puts", None)
        call_volume += _sum_column(calls, "volume")
        put_volume += _sum_column(puts, "volume")
        call_open_interest += _sum_column(calls, "openInterest")
        put_open_interest += _sum_column(puts, "openInterest")
        dealer_gamma_exposure_proxy += _near_money_exposure_proxy(calls, price)
        dealer_gamma_exposure_proxy += _near_money_exposure_proxy(puts, price)
        populated = True

    if not populated:
        return empty, warnings

    return {
        "call_volume": call_volume,
        "put_volume": put_volume,
        "call_open_interest": call_open_interest,
        "put_open_interest": put_open_interest,
        "dealer_gamma_exposure_proxy": dealer_gamma_exposure_proxy if price is not None else None,
        "option_chain_source": YAHOO_OPTION_SOURCE,
        "option_chain_provider": "yahoo_finance",
        "option_chain_fetched_at": fetched_at,
        "option_chain_freshness_seconds": 0.0 if fetched_at is not None else None,
        "option_chain_stale_after_seconds": YAHOO_OPTION_STALE_AFTER_SECONDS,
        "option_chain_capabilities": dict(YAHOO_OPTION_CAPABILITIES),
    }, warnings


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


def _sum_column(frame: Any, column: str) -> float:
    if frame is None or getattr(frame, "empty", True) or column not in frame:
        return 0.0
    total = 0.0
    for value in frame[column].dropna().tolist():
        number = _to_float(value)
        if number is not None:
            total += number
    return total


def _near_money_exposure_proxy(frame: Any, price: float | None, strike_window: float = 0.15) -> float:
    if price is None or price <= 0 or frame is None or getattr(frame, "empty", True):
        return 0.0
    if "strike" not in frame or "openInterest" not in frame:
        return 0.0

    lower_strike = price * (1.0 - strike_window)
    upper_strike = price * (1.0 + strike_window)
    exposure = 0.0
    for row in frame[["strike", "openInterest"]].dropna().to_dict("records"):
        strike = _to_float(row.get("strike"))
        open_interest = _to_float(row.get("openInterest"))
        if strike is None or open_interest is None or not lower_strike <= strike <= upper_strike:
            continue
        exposure += open_interest * 100.0 * price
    return exposure


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


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        datetime_value = value
    elif hasattr(value, "to_pydatetime"):
        datetime_value = value.to_pydatetime()
    else:
        return None

    if datetime_value.tzinfo is None:
        return datetime_value.replace(tzinfo=timezone.utc)
    return datetime_value.astimezone(timezone.utc)


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
