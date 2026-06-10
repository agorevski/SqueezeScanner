from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

OptionSide = Literal["call", "put"]


class ScannerError(Exception):
    """Base error for scanner failures."""


class InvalidSymbolError(ScannerError, ValueError):
    """Raised when ticker input cannot be normalized."""


class InvalidRankingModeError(ScannerError, ValueError):
    """Raised when scan result ranking options are invalid."""


class DataProviderError(ScannerError, RuntimeError):
    """Raised when market data cannot be retrieved for a ticker."""


class PremiumProviderNotConfigured(DataProviderError):
    """Raised when an optional premium provider seam has no configured adapter."""


class ScreenerError(ScannerError, RuntimeError):
    """Raised when a market screener cannot return a usable symbol universe."""


@dataclass(frozen=True)
class OptionProviderCapabilities:
    expiration_listing: bool = False
    strike_listing: bool = False
    bid_ask: bool = False
    last_price: bool = False
    volume: bool = False
    open_interest: bool = False
    open_interest_change: bool = False
    implied_volatility: bool = False
    delta: bool = False
    gamma: bool = False
    contract_timestamp: bool = False
    true_gamma_exposure: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {key: bool(value) for key, value in asdict(self).items()}


@dataclass(frozen=True)
class OptionChainRecord:
    symbol: str
    expiration: str
    strike: float
    side: OptionSide | str
    contract_symbol: str | None = None
    days_to_expiration: float | None = None
    days_to_expiry: float | None = None
    bid: float | None = None
    ask: float | None = None
    last_price: float | None = None
    volume: float | None = None
    open_interest: float | None = None
    open_interest_change: float | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    timestamp: str | None = None
    provider: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class OptionChainSnapshot:
    symbol: str
    records: list[OptionChainRecord] = field(default_factory=list)
    provider: str | None = None
    source: str | None = None
    fetched_at: str | None = None
    freshness_seconds: float | None = None
    stale_after_seconds: float | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class MarketDataProvider(Protocol):
    def fetch(self, symbol: str) -> "TickerSnapshot":
        """Return normalized market data for a symbol."""


class QuoteProvider(Protocol):
    def fetch_quote(self, symbol: str) -> "TickerSnapshot":
        """Return normalized quote, price, volume, and liquidity fields."""


class ShortInterestProvider(Protocol):
    def fetch_short_interest(self, symbol: str) -> "TickerSnapshot":
        """Return normalized short-interest fields."""


class BorrowProvider(Protocol):
    def fetch_borrow(self, symbol: str) -> "TickerSnapshot":
        """Return normalized borrow availability and borrow-fee fields."""


class OptionsProvider(Protocol):
    def fetch_options(self, symbol: str) -> "TickerSnapshot":
        """Return normalized options activity and exposure fields."""


class OptionChainProvider(Protocol):
    def fetch_option_chain(self, symbol: str) -> OptionChainSnapshot:
        """Return normalized option-chain records and provider capability metadata."""


class CorporateActionsProvider(Protocol):
    def fetch_corporate_actions(self, symbol: str) -> "TickerSnapshot":
        """Return normalized split, dilution, and corporate-action fields."""


class FilingsProvider(Protocol):
    def fetch_filings(self, symbol: str) -> "TickerSnapshot":
        """Return normalized filing-derived float and risk-signal fields."""


class EventProvider(Protocol):
    def fetch_events(self, symbol: str) -> "TickerSnapshot":
        """Return normalized halt, news, and event-risk fields."""


@dataclass(frozen=True)
class ProviderCapability:
    feed: str
    fields: tuple[str, ...]
    affects_scores: bool
    creates_risk_flags: bool
    description: str

    def to_dict(self) -> dict[str, object]:
        return {
            "feed": self.feed,
            "fields": list(self.fields),
            "affects_scores": self.affects_scores,
            "creates_risk_flags": self.creates_risk_flags,
            "description": self.description,
        }


@dataclass(frozen=True)
class ProviderHealth:
    feed: str
    provider: str
    enabled: bool
    configured: bool
    status: str
    message: str
    capability: ProviderCapability

    def to_dict(self) -> dict[str, object]:
        return {
            "feed": self.feed,
            "provider": self.provider,
            "enabled": self.enabled,
            "configured": self.configured,
            "status": self.status,
            "message": self.message,
            "capability": self.capability.to_dict(),
        }


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
    short_interest_settlement_date: str | None = None
    short_interest_reported_at: str | None = None
    short_interest_revised_at: str | None = None
    short_interest_revision_id: str | None = None
    float_shares: float | None = None
    market_cap: float | None = None
    borrow_fee_pct: float | None = None
    borrow_available_shares: float | None = None
    borrow_utilization_pct: float | None = None
    borrow_rebate_rate_pct: float | None = None
    borrow_fetched_at: str | None = None
    recent_reverse_split: bool | None = None
    days_since_reverse_split: float | None = None
    reverse_split_ratio: float | None = None
    dilution_risk: bool | None = None
    offering_risk: bool | None = None
    warrant_overhang_risk: bool | None = None
    atm_program_risk: bool | None = None
    active_trading_halt: bool | None = None
    halt_risk: bool | None = None
    material_news_event: bool | None = None
    event_risk: bool | None = None
    call_volume: float | None = None
    put_volume: float | None = None
    call_open_interest: float | None = None
    put_open_interest: float | None = None
    dealer_gamma_exposure_proxy: float | None = None
    call_gamma_exposure: float | None = None
    put_gamma_exposure: float | None = None
    net_gamma_exposure: float | None = None
    absolute_gamma_exposure: float | None = None
    gamma_exposure_pct_market_cap: float | None = None
    gamma_flip_price: float | None = None
    gamma_flip_distance_pct: float | None = None
    max_gamma_strike: float | None = None
    call_wall_strike: float | None = None
    put_wall_strike: float | None = None
    largest_gamma_expiration: str | None = None
    gamma_strike_concentration_pct: float | None = None
    gamma_expiration_concentration_pct: float | None = None
    option_chain_source: str | None = None
    option_chain_provider: str | None = None
    option_chain_fetched_at: str | None = None
    option_chain_freshness_seconds: float | None = None
    option_chain_stale_after_seconds: float | None = None
    option_chain_capabilities: dict[str, bool] = field(default_factory=dict)
    option_chain_records: list[OptionChainRecord] = field(default_factory=list)
    change_1d_pct: float | None = None
    change_5d_pct: float | None = None
    change_20d_pct: float | None = None
    distance_from_52_week_high_pct: float | None = None
    source_fetched_at: str | None = None
    field_sources: dict[str, str] = field(default_factory=dict)
    field_quality: dict[str, str] = field(default_factory=dict)
    source_quality: dict[str, float] = field(default_factory=dict)
    source_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GuardrailConfig:
    min_price: float = 1.0
    max_price: float = 250.0
    min_dollar_volume: float = 1_000_000.0
    min_avg_volume_20d: float = 250_000.0
    min_avg_dollar_volume_20d: float = 1_000_000.0
    min_market_cap: float = 25_000_000.0
    max_squeeze_market_cap: float = 10_000_000_000.0
    max_missing_core_fields: int = 4
    recent_reverse_split_days: float = 180.0


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
    metrics: dict[str, Any]
    components: dict[str, float]
    rationale: list[str]
    warnings: list[str]
    field_sources: dict[str, str] = field(default_factory=dict)
    field_quality: dict[str, str] = field(default_factory=dict)
    source_quality: dict[str, float] = field(default_factory=dict)
    model_confidence: dict[str, float] = field(default_factory=dict)
    confidence_rationales: dict[str, list[str]] = field(default_factory=dict)
    risk_flags: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
