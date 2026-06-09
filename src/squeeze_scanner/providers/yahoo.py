from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any, Mapping, Sequence

from squeeze_scanner.domain import DataProviderError, ScreenerError, TickerSnapshot

MOST_SHORTED_SCREENER = "most_shorted_stocks"
MAX_SCREENER_COUNT = 250


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
