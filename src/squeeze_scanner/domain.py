from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


class ScannerError(Exception):
    """Base error for scanner failures."""


class InvalidSymbolError(ScannerError, ValueError):
    """Raised when ticker input cannot be normalized."""


class DataProviderError(ScannerError, RuntimeError):
    """Raised when market data cannot be retrieved for a ticker."""


class ScreenerError(ScannerError, RuntimeError):
    """Raised when a market screener cannot return a usable symbol universe."""


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
    borrow_fee_pct: float | None = None
    recent_reverse_split: bool | None = None
    days_since_reverse_split: float | None = None
    reverse_split_ratio: float | None = None
    call_volume: float | None = None
    put_volume: float | None = None
    call_open_interest: float | None = None
    put_open_interest: float | None = None
    dealer_gamma_exposure_proxy: float | None = None
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
    primary_model: str
    model_scores: dict[str, float]
    model_components: dict[str, dict[str, float]]
    model_rationales: dict[str, list[str]]
    metrics: dict[str, float | bool | None]
    components: dict[str, float]
    rationale: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
