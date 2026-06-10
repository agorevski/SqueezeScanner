from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import Iterable

from squeeze_scanner.config import Settings
from squeeze_scanner.domain import (
    DataProviderError,
    MarketDataProvider,
    PremiumProviderNotConfigured,
    ProviderCapability,
    ProviderHealth,
    TickerSnapshot,
)
from squeeze_scanner.providers.yahoo import YahooFinanceProvider

DISABLED_PROVIDER_NAMES = {"", "disabled", "none", "off", "false", "0"}
YAHOO_PROVIDER_NAMES = {"yahoo", "yahoo_finance"}
DEFAULT_SOURCE_QUALITY = 0.0

QUOTE_CAPABILITY = ProviderCapability(
    feed="quote",
    fields=(
        "price",
        "previous_close",
        "volume",
        "avg_volume_20d",
        "avg_volume_90d",
        "float_shares",
        "market_cap",
    ),
    affects_scores=True,
    creates_risk_flags=True,
    description="Yahoo Finance remains the built-in quote, liquidity, public short-interest, split, and options-proxy source.",
)

PREMIUM_CAPABILITIES: dict[str, ProviderCapability] = {
    "borrow": ProviderCapability(
        feed="borrow",
        fields=(
            "borrow_fee_pct",
            "borrow_available_shares",
            "borrow_utilization_pct",
            "borrow_rebate_rate_pct",
            "borrow_fetched_at",
        ),
        affects_scores=True,
        creates_risk_flags=False,
        description="Optional securities-lending feed for borrow fee, availability, utilization, and rebate rate.",
    ),
    "short_interest": ProviderCapability(
        feed="short_interest",
        fields=(
            "short_percent_float",
            "short_ratio",
            "shares_short",
            "shares_short_prior_month",
            "short_interest_settlement_date",
            "short_interest_reported_at",
            "short_interest_revised_at",
            "short_interest_revision_id",
        ),
        affects_scores=True,
        creates_risk_flags=False,
        description="Optional fresher short-interest feed with settlement, report, and revision metadata.",
    ),
    "corporate_actions": ProviderCapability(
        feed="corporate_actions",
        fields=(
            "recent_reverse_split",
            "days_since_reverse_split",
            "reverse_split_ratio",
            "dilution_risk",
            "offering_risk",
            "warrant_overhang_risk",
            "atm_program_risk",
        ),
        affects_scores=True,
        creates_risk_flags=True,
        description="Optional corporate-action feed for split, dilution, offering, warrant, and ATM-program risk.",
    ),
    "filings": ProviderCapability(
        feed="filings",
        fields=(
            "float_shares",
            "dilution_risk",
            "offering_risk",
            "warrant_overhang_risk",
            "atm_program_risk",
        ),
        affects_scores=True,
        creates_risk_flags=True,
        description="Optional filing-derived feed for float and dilution-risk signals.",
    ),
    "events": ProviderCapability(
        feed="events",
        fields=(
            "active_trading_halt",
            "halt_risk",
            "material_news_event",
            "event_risk",
        ),
        affects_scores=False,
        creates_risk_flags=True,
        description="Optional halt, news, and catalyst feed that creates warnings or risk flags without changing scores.",
    ),
}

_SNAPSHOT_FIELD_NAMES = {field.name for field in fields(TickerSnapshot)}


@dataclass(frozen=True)
class PremiumProviderSet:
    borrow: "PremiumDataFeedProvider"
    short_interest: "PremiumDataFeedProvider"
    corporate_actions: "PremiumDataFeedProvider"
    filings: "PremiumDataFeedProvider"
    events: "PremiumDataFeedProvider"

    def all(self) -> tuple["PremiumDataFeedProvider", ...]:
        return (
            self.borrow,
            self.short_interest,
            self.corporate_actions,
            self.filings,
            self.events,
        )

    def enabled(self) -> tuple["PremiumDataFeedProvider", ...]:
        return tuple(provider for provider in self.all() if provider.enabled)

    def any_enabled(self) -> bool:
        return any(provider.enabled for provider in self.all())

    def statuses(self) -> list[dict[str, object]]:
        return [provider.health().to_dict() for provider in self.all()]


class PremiumDataFeedProvider:
    def __init__(
        self,
        capability: ProviderCapability,
        provider_name: str,
        *,
        enabled: bool,
        configured: bool,
        status: str,
        message: str,
        source_quality: float = DEFAULT_SOURCE_QUALITY,
    ) -> None:
        self.capability = capability
        self.provider_name = provider_name
        self.enabled = enabled
        self.configured = configured
        self.status = status
        self.message = message
        self.source_quality = source_quality

    @property
    def feed(self) -> str:
        return self.capability.feed

    @property
    def source_name(self) -> str:
        provider = _normalize_provider_name(self.provider_name) or "disabled"
        return f"{provider}_{self.feed}"

    def fetch(self, symbol: str) -> TickerSnapshot:
        if not self.enabled:
            raise PremiumProviderNotConfigured(f"{self.feed} provider is disabled.")
        if not self.configured:
            raise PremiumProviderNotConfigured(self.message)
        raise PremiumProviderNotConfigured(
            f"{self.feed} provider '{self.provider_name}' has no adapter implementation."
        )

    def fetch_borrow(self, symbol: str) -> TickerSnapshot:
        return self.fetch(symbol)

    def fetch_short_interest(self, symbol: str) -> TickerSnapshot:
        return self.fetch(symbol)

    def fetch_corporate_actions(self, symbol: str) -> TickerSnapshot:
        return self.fetch(symbol)

    def fetch_filings(self, symbol: str) -> TickerSnapshot:
        return self.fetch(symbol)

    def fetch_events(self, symbol: str) -> TickerSnapshot:
        return self.fetch(symbol)

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            feed=self.feed,
            provider=self.provider_name,
            enabled=self.enabled,
            configured=self.configured,
            status=self.status,
            message=self.message,
            capability=self.capability,
        )


class CompositeMarketDataProvider:
    """Merges optional premium feed patches on top of the default Yahoo snapshot."""

    def __init__(
        self,
        base_provider: MarketDataProvider,
        premium_providers: PremiumProviderSet,
        *,
        base_provider_name: str = "yahoo_finance",
    ) -> None:
        self.base_provider = base_provider
        self.premium_providers = premium_providers
        self.base_provider_name = base_provider_name
        self.provider_name = "composite_market_data" if premium_providers.any_enabled() else base_provider_name

    def fetch(self, symbol: str) -> TickerSnapshot:
        snapshot = self.base_provider.fetch(symbol)
        for provider in self.premium_providers.enabled():
            try:
                patch = provider.fetch(symbol)
            except DataProviderError as exc:
                snapshot = snapshot_with_provider_error(snapshot, provider, str(exc))
                continue
            except Exception as exc:
                snapshot = snapshot_with_provider_error(
                    snapshot,
                    provider,
                    f"unexpected {provider.feed} provider error ({exc})",
                )
                continue
            snapshot = merge_ticker_snapshots(snapshot, patch)
        return snapshot


def build_market_data_provider(settings: Settings) -> tuple[CompositeMarketDataProvider, PremiumProviderSet]:
    quote_provider = _normalize_provider_name(settings.quote_provider)
    if quote_provider not in YAHOO_PROVIDER_NAMES:
        raise ValueError("Only the built-in Yahoo quote provider is currently implemented.")

    premium_providers = build_premium_provider_set(settings)
    return CompositeMarketDataProvider(YahooFinanceProvider(), premium_providers), premium_providers


def build_premium_provider_set(settings: Settings) -> PremiumProviderSet:
    return PremiumProviderSet(
        borrow=build_premium_provider("borrow", settings.borrow_provider),
        short_interest=build_premium_provider("short_interest", settings.short_interest_provider),
        corporate_actions=build_premium_provider("corporate_actions", settings.corporate_actions_provider),
        filings=build_premium_provider("filings", settings.filings_provider),
        events=build_premium_provider("events", settings.event_provider),
    )


def build_premium_provider(feed: str, provider_name: str | None) -> PremiumDataFeedProvider:
    capability = PREMIUM_CAPABILITIES[feed]
    normalized = _normalize_provider_name(provider_name)
    if normalized in DISABLED_PROVIDER_NAMES or normalized in YAHOO_PROVIDER_NAMES:
        return PremiumDataFeedProvider(
            capability,
            "disabled",
            enabled=False,
            configured=False,
            status="disabled",
            message=f"Optional {feed} provider is disabled; Yahoo-only behavior is unchanged.",
        )

    return PremiumDataFeedProvider(
        capability,
        normalized,
        enabled=True,
        configured=False,
        status="unconfigured",
        message=(
            f"Optional {feed} provider '{normalized}' is selected, but no adapter credentials or "
            "implementation are configured."
        ),
    )


def provider_status_payload(settings: Settings, premium_providers: PremiumProviderSet) -> dict[str, object]:
    quote_provider = _normalize_provider_name(settings.quote_provider) or "yahoo"
    default_provider = ProviderHealth(
        feed="quote",
        provider="yahoo_finance" if quote_provider in YAHOO_PROVIDER_NAMES else quote_provider,
        enabled=True,
        configured=quote_provider in YAHOO_PROVIDER_NAMES,
        status="available" if quote_provider in YAHOO_PROVIDER_NAMES else "unsupported",
        message="Yahoo Finance is the default market-data provider.",
        capability=QUOTE_CAPABILITY,
    )
    return {
        "default_provider": default_provider.to_dict(),
        "premium_providers": premium_providers.statuses(),
    }


def merge_ticker_snapshots(base: TickerSnapshot, patch: TickerSnapshot) -> TickerSnapshot:
    payload = _snapshot_payload(base)
    patch_payload = _snapshot_payload(patch)
    metadata_fields = {
        "source_fetched_at",
        "field_sources",
        "field_quality",
        "source_quality",
        "source_warnings",
    }

    for name, value in patch_payload.items():
        if name == "symbol" or name in metadata_fields:
            continue
        if value is not None:
            payload[name] = value

    payload["field_sources"] = {
        **dict(base.field_sources),
        **_filtered_string_mapping(patch.field_sources),
    }
    payload["field_quality"] = {
        **dict(base.field_quality),
        **_filtered_string_mapping(patch.field_quality),
    }
    payload["source_quality"] = {
        **dict(base.source_quality),
        **_filtered_float_mapping(patch.source_quality),
    }
    payload["source_warnings"] = _unique_strings([*base.source_warnings, *patch.source_warnings])
    payload["source_fetched_at"] = _latest_iso_timestamp(base.source_fetched_at, patch.source_fetched_at)
    return TickerSnapshot(**payload)


def snapshot_with_provider_error(
    snapshot: TickerSnapshot,
    provider: PremiumDataFeedProvider,
    message: str,
) -> TickerSnapshot:
    payload = _snapshot_payload(snapshot)
    field_sources = dict(snapshot.field_sources)
    field_quality = dict(snapshot.field_quality)
    source_quality = dict(snapshot.source_quality)
    source_quality[provider.source_name] = provider.source_quality

    for field_name in provider.capability.fields:
        if field_name not in _SNAPSHOT_FIELD_NAMES:
            continue
        if getattr(snapshot, field_name) is None:
            field_sources.setdefault(field_name, provider.source_name)
            field_quality[field_name] = "provider-error"

    warning = f"Optional {provider.feed} provider '{provider.provider_name}' unavailable: {message}"
    payload["field_sources"] = field_sources
    payload["field_quality"] = field_quality
    payload["source_quality"] = source_quality
    payload["source_warnings"] = _unique_strings([*snapshot.source_warnings, warning])
    return TickerSnapshot(**payload)


def _normalize_provider_name(provider_name: str | None) -> str:
    return (provider_name or "").strip().lower().replace("-", "_")


def _snapshot_payload(snapshot: TickerSnapshot) -> dict[str, object]:
    return {field.name: getattr(snapshot, field.name) for field in fields(TickerSnapshot)}


def _filtered_string_mapping(value: dict[str, str]) -> dict[str, str]:
    return {
        str(key): str(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str) and key in _SNAPSHOT_FIELD_NAMES
    }


def _filtered_float_mapping(value: dict[str, float]) -> dict[str, float]:
    filtered: dict[str, float] = {}
    for key, item in value.items():
        try:
            filtered[str(key)] = float(item)
        except (TypeError, ValueError):
            continue
    return filtered


def _unique_strings(values: Iterable[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        if value not in seen:
            unique.append(value)
            seen.add(value)
    return unique


def _latest_iso_timestamp(first: str | None, second: str | None) -> str | None:
    if first is None:
        return second
    if second is None:
        return first

    first_dt = _parse_iso_timestamp(first)
    second_dt = _parse_iso_timestamp(second)
    if first_dt is None or second_dt is None:
        return first
    return second if second_dt > first_dt else first


def _parse_iso_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
