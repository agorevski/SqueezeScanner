from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from .domain import OptionChainRecord, OptionChainSnapshot, TickerSnapshot

CONTRACT_MULTIPLIER = 100.0
GAMMA_EXPOSURE_PERCENT_MOVE = 0.01
YAHOO_OPTION_PROXY_SOURCE = "yahoo_finance_options_proxy"

TRUE_GAMMA_FIELDS = (
    "call_gamma_exposure",
    "put_gamma_exposure",
    "net_gamma_exposure",
    "absolute_gamma_exposure",
    "gamma_exposure_pct_market_cap",
    "gamma_flip_price",
    "gamma_flip_distance_pct",
    "max_gamma_strike",
    "call_wall_strike",
    "put_wall_strike",
    "largest_gamma_expiration",
    "gamma_strike_concentration_pct",
    "gamma_expiration_concentration_pct",
)

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("symbol", "underlying", "underlying_symbol", "underlyingSymbol"),
    "expiration": (
        "expiration",
        "expiration_date",
        "expirationDate",
        "expiry",
        "expiry_date",
        "expiryDate",
    ),
    "strike": ("strike", "strike_price", "strikePrice"),
    "side": ("side", "type", "option_type", "optionType", "put_call", "putCall"),
    "contract_symbol": ("contract_symbol", "contractSymbol", "symbol_contract", "contract"),
    "days_to_expiration": (
        "days_to_expiration",
        "days_to_expiry",
        "daysToExpiration",
        "daysToExpiry",
        "dte",
    ),
    "days_to_expiry": (
        "days_to_expiry",
        "days_to_expiration",
        "daysToExpiry",
        "daysToExpiration",
        "dte",
    ),
    "bid": ("bid",),
    "ask": ("ask",),
    "last_price": ("last_price", "lastPrice", "last", "mark"),
    "volume": ("volume", "vol"),
    "open_interest": ("open_interest", "openInterest", "oi"),
    "open_interest_change": (
        "open_interest_change",
        "openInterestChange",
        "oi_change",
        "oiChange",
    ),
    "implied_volatility": ("implied_volatility", "impliedVolatility", "iv"),
    "delta": ("delta",),
    "gamma": ("gamma",),
    "timestamp": ("timestamp", "updated_at", "updatedAt", "quote_time", "quoteTime", "lastTradeDate"),
    "provider": ("provider",),
    "source": ("source",),
}
_OCC_CONTRACT_PATTERN = re.compile(r"^([A-Z0-9.\- ]+?)\d{6}([CP])\d{8}$", re.IGNORECASE)


@dataclass(frozen=True)
class GammaExposureAggregation:
    """Aggregated true gamma exposure derived from contract-level greeks.

    Contract exposure uses `abs(gamma) * open_interest * 100 * spot^2 * 0.01`.
    Calls are signed positive and puts negative, while absolute exposure keeps
    the total magnitude across both sides.
    """

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
    valid_contract_count: int = 0
    skipped_contract_count: int = 0
    missing_fields: dict[str, int] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def metric_fields(self) -> dict[str, float | str | None]:
        return {field_name: getattr(self, field_name) for field_name in TRUE_GAMMA_FIELDS}


def normalize_option_chain_records(
    records: object,
    *,
    fallback_symbol: str | None = None,
    provider: str | None = None,
    source: str | None = None,
) -> list[OptionChainRecord]:
    """Return normalized `OptionChainRecord` objects from provider rows."""

    if isinstance(records, OptionChainSnapshot):
        fallback_symbol = fallback_symbol or records.symbol
        provider = provider or records.provider
        source = source or records.source
        records = records.records

    if records is None:
        return []
    if isinstance(records, (OptionChainRecord, Mapping)):
        items: Iterable[object] = (records,)
    elif isinstance(records, Iterable) and not isinstance(records, (str, bytes)):
        items = records
    else:
        return []

    normalized: list[OptionChainRecord] = []
    for item in items:
        record = normalize_option_chain_record(
            item,
            fallback_symbol=fallback_symbol,
            provider=provider,
            source=source,
        )
        if record is not None:
            normalized.append(record)
    return normalized


def normalize_option_chain_record(
    record: object,
    *,
    fallback_symbol: str | None = None,
    provider: str | None = None,
    source: str | None = None,
) -> OptionChainRecord | None:
    if isinstance(record, OptionChainRecord):
        payload: dict[str, object] = asdict(record)
    elif isinstance(record, Mapping):
        payload = {
            field_name: _first_present(record, aliases)
            for field_name, aliases in _FIELD_ALIASES.items()
        }
    else:
        return None

    raw_symbol = _text(payload.get("symbol"))
    raw_contract_symbol = _text(payload.get("contract_symbol"))
    contract_match = _OCC_CONTRACT_PATTERN.search(raw_symbol or "")
    if raw_contract_symbol is None and contract_match is not None:
        raw_contract_symbol = raw_symbol

    symbol = raw_symbol or _text(fallback_symbol)
    if contract_match is not None:
        symbol = _text(fallback_symbol) or _text(contract_match.group(1))
    payload["symbol"] = symbol.upper() if symbol is not None else None
    payload["expiration"] = _date_text(payload.get("expiration"))
    payload["contract_symbol"] = raw_contract_symbol
    payload["side"] = _option_side(payload.get("side")) or _side_from_contract(payload.get("contract_symbol"))
    payload["provider"] = _text(payload.get("provider")) or _text(provider)
    payload["source"] = _text(payload.get("source")) or _text(source)
    payload["timestamp"] = _timestamp_text(payload.get("timestamp"))

    for field_name in (
        "strike",
        "days_to_expiration",
        "days_to_expiry",
        "bid",
        "ask",
        "last_price",
        "volume",
        "open_interest",
        "open_interest_change",
        "delta",
        "gamma",
    ):
        payload[field_name] = _to_float(payload.get(field_name))
    payload["implied_volatility"] = _to_float(payload.get("implied_volatility"), percent=True)

    if payload.get("days_to_expiration") is None:
        payload["days_to_expiration"] = payload.get("days_to_expiry")
    if payload.get("days_to_expiry") is None:
        payload["days_to_expiry"] = payload.get("days_to_expiration")

    if not payload["symbol"] or not payload["expiration"]:
        return None
    if payload["side"] is None or payload["strike"] is None:
        return None

    allowed = {field_info.name for field_info in fields(OptionChainRecord)}
    return OptionChainRecord(**{key: payload.get(key) for key in allowed})


def normalize_option_chain_snapshot(
    value: object,
    *,
    symbol: str | None = None,
    provider: str | None = None,
    source: str | None = None,
    fetched_at: str | None = None,
    freshness_seconds: float | None = None,
    stale_after_seconds: float | None = None,
    capabilities: Mapping[str, object] | None = None,
    warnings: Iterable[object] | None = None,
) -> OptionChainSnapshot | None:
    if isinstance(value, OptionChainSnapshot):
        symbol = symbol or value.symbol
        provider = provider or value.provider
        source = source or value.source
        fetched_at = fetched_at or value.fetched_at
        freshness_seconds = freshness_seconds if freshness_seconds is not None else value.freshness_seconds
        stale_after_seconds = stale_after_seconds if stale_after_seconds is not None else value.stale_after_seconds
        capabilities = capabilities or value.capabilities
        warnings = warnings or value.warnings
        records = value.records
    elif isinstance(value, Mapping):
        symbol = symbol or _text(value.get("symbol"))
        provider = provider or _text(value.get("provider"))
        source = source or _text(value.get("source"))
        fetched_at = fetched_at or _timestamp_text(value.get("fetched_at"))
        freshness_seconds = (
            freshness_seconds
            if freshness_seconds is not None
            else _to_float(value.get("freshness_seconds"))
        )
        stale_after_seconds = (
            stale_after_seconds
            if stale_after_seconds is not None
            else _to_float(value.get("stale_after_seconds"))
        )
        raw_capabilities = value.get("capabilities")
        capabilities = capabilities or (raw_capabilities if isinstance(raw_capabilities, Mapping) else None)
        raw_warnings = value.get("warnings")
        warnings = warnings or (
            raw_warnings
            if isinstance(raw_warnings, Iterable) and not isinstance(raw_warnings, (str, bytes))
            else None
        )
        records = value.get("records")
    else:
        records = value

    normalized_symbol = _text(symbol)
    if normalized_symbol is not None:
        normalized_symbol = normalized_symbol.upper()
    if normalized_symbol is None:
        normalized_records = normalize_option_chain_records(records, provider=provider, source=source)
        if not normalized_records:
            return None
        normalized_symbol = normalized_records[0].symbol
    else:
        normalized_records = normalize_option_chain_records(
            records,
            fallback_symbol=normalized_symbol,
            provider=provider,
            source=source,
        )

    return OptionChainSnapshot(
        symbol=normalized_symbol,
        records=normalized_records,
        provider=_text(provider),
        source=_text(source),
        fetched_at=fetched_at,
        freshness_seconds=freshness_seconds,
        stale_after_seconds=stale_after_seconds,
        capabilities=_bool_mapping(capabilities),
        warnings=[str(item) for item in (warnings or []) if str(item).strip()],
    )


def aggregate_true_gamma_exposure(
    records: object,
    *,
    spot: float | None,
    market_cap: float | None = None,
    contract_multiplier: float = CONTRACT_MULTIPLIER,
    percent_move: float = GAMMA_EXPOSURE_PERCENT_MOVE,
) -> GammaExposureAggregation:
    normalized_records = normalize_option_chain_records(records)
    spot_value = _to_float(spot)
    missing_fields: dict[str, int] = {}
    warnings: list[str] = []

    if spot_value is None or spot_value <= 0:
        missing_fields["spot"] = max(1, len(normalized_records))
        warnings.append("Underlying spot price is required for true gamma exposure aggregation.")
        return GammaExposureAggregation(
            skipped_contract_count=len(normalized_records),
            missing_fields=missing_fields,
            warnings=tuple(warnings),
        )

    multiplier = _positive_float(contract_multiplier)
    move = _positive_float(percent_move)
    if multiplier is None or move is None:
        warnings.append("Contract multiplier and percent move must be positive.")
        return GammaExposureAggregation(
            skipped_contract_count=len(normalized_records),
            warnings=tuple(warnings),
        )

    call_total = 0.0
    put_total = 0.0
    valid_count = 0
    skipped_count = 0
    call_by_strike: defaultdict[float, float] = defaultdict(float)
    put_by_strike: defaultdict[float, float] = defaultdict(float)
    abs_by_strike: defaultdict[float, float] = defaultdict(float)
    net_by_strike: defaultdict[float, float] = defaultdict(float)
    abs_by_expiration: defaultdict[str, float] = defaultdict(float)

    for record in normalized_records:
        missing = _missing_contract_fields(record)
        if missing:
            skipped_count += 1
            for field_name in missing:
                missing_fields[field_name] = missing_fields.get(field_name, 0) + 1
            continue

        gamma = abs(float(record.gamma))
        open_interest = float(record.open_interest)
        if open_interest < 0:
            skipped_count += 1
            missing_fields["open_interest"] = missing_fields.get("open_interest", 0) + 1
            continue

        exposure_magnitude = gamma * open_interest * multiplier * spot_value * spot_value * move
        exposure = exposure_magnitude if record.side == "call" else -exposure_magnitude
        valid_count += 1

        if record.side == "call":
            call_total += exposure
            call_by_strike[float(record.strike)] += exposure_magnitude
        else:
            put_total += exposure
            put_by_strike[float(record.strike)] += exposure_magnitude

        abs_by_strike[float(record.strike)] += exposure_magnitude
        net_by_strike[float(record.strike)] += exposure
        abs_by_expiration[str(record.expiration)] += exposure_magnitude

    if valid_count == 0:
        if normalized_records and not warnings:
            warnings.append("No option-chain records had gamma, open interest, side, strike, and expiration.")
        return GammaExposureAggregation(
            skipped_contract_count=skipped_count,
            missing_fields=missing_fields,
            warnings=tuple(warnings),
        )

    net_total = call_total + put_total
    absolute_total = call_total + abs(put_total)
    gamma_pct_market_cap = _pct_of_market_cap(absolute_total, market_cap)
    gamma_flip_price = _gamma_flip_price(net_by_strike)
    gamma_flip_distance_pct = (
        ((gamma_flip_price / spot_value) - 1.0) * 100.0
        if gamma_flip_price is not None and spot_value > 0
        else None
    )

    return GammaExposureAggregation(
        call_gamma_exposure=call_total,
        put_gamma_exposure=put_total,
        net_gamma_exposure=net_total,
        absolute_gamma_exposure=absolute_total,
        gamma_exposure_pct_market_cap=gamma_pct_market_cap,
        gamma_flip_price=gamma_flip_price,
        gamma_flip_distance_pct=gamma_flip_distance_pct,
        max_gamma_strike=_max_numeric_key(abs_by_strike),
        call_wall_strike=_max_numeric_key(call_by_strike),
        put_wall_strike=_max_numeric_key(put_by_strike),
        largest_gamma_expiration=_max_text_key(abs_by_expiration),
        gamma_strike_concentration_pct=_concentration_pct(abs_by_strike, absolute_total),
        gamma_expiration_concentration_pct=_concentration_pct(abs_by_expiration, absolute_total),
        valid_contract_count=valid_count,
        skipped_contract_count=skipped_count,
        missing_fields=missing_fields,
        warnings=tuple(warnings),
    )


def snapshot_with_true_gamma_metrics(snapshot: TickerSnapshot) -> TickerSnapshot:
    records = normalize_option_chain_records(
        snapshot.option_chain_records,
        fallback_symbol=snapshot.symbol,
        provider=snapshot.option_chain_provider,
        source=snapshot.option_chain_source,
    )
    if not records:
        return snapshot

    if _has_all_true_gamma_fields(snapshot) or _is_yahoo_proxy_only(snapshot):
        if records == snapshot.option_chain_records:
            return snapshot
        return replace(snapshot, option_chain_records=records)

    aggregation = aggregate_true_gamma_exposure(
        records,
        spot=snapshot.price,
        market_cap=snapshot.market_cap,
    )
    if aggregation.valid_contract_count == 0:
        if records == snapshot.option_chain_records:
            return snapshot
        return replace(snapshot, option_chain_records=records)

    updates: dict[str, Any] = {"option_chain_records": records}
    populated_fields: list[str] = []
    for field_name, value in aggregation.metric_fields().items():
        if getattr(snapshot, field_name) is None and value is not None:
            updates[field_name] = value
            populated_fields.append(field_name)

    if populated_fields:
        source_name = _gamma_source_name(snapshot)
        updates["field_sources"] = {
            **dict(snapshot.field_sources),
            **{field_name: source_name for field_name in populated_fields},
        }
        updates["field_quality"] = {
            **dict(snapshot.field_quality),
            **{field_name: "present" for field_name in populated_fields},
        }
        updates["option_chain_capabilities"] = {
            **dict(snapshot.option_chain_capabilities),
            "open_interest": True,
            "gamma": True,
            "true_gamma_exposure": True,
        }

    return replace(snapshot, **updates)


def _missing_contract_fields(record: OptionChainRecord) -> list[str]:
    missing: list[str] = []
    for field_name in ("gamma", "open_interest", "strike", "expiration", "side"):
        value = getattr(record, field_name)
        if value is None or value == "":
            missing.append(field_name)
    return missing


def _gamma_flip_price(net_by_strike: Mapping[float, float]) -> float | None:
    if len(net_by_strike) < 2:
        return None

    cumulative = 0.0
    previous_strike: float | None = None
    previous_cumulative: float | None = None
    for strike in sorted(net_by_strike):
        cumulative += net_by_strike[strike]
        if cumulative == 0:
            return strike
        if previous_cumulative is not None and previous_strike is not None:
            if previous_cumulative == 0:
                return previous_strike
            if (previous_cumulative < 0 < cumulative) or (previous_cumulative > 0 > cumulative):
                denominator = abs(previous_cumulative) + abs(cumulative)
                if denominator <= 0:
                    return strike
                return previous_strike + (strike - previous_strike) * (abs(previous_cumulative) / denominator)
        previous_strike = strike
        previous_cumulative = cumulative
    return None


def _has_all_true_gamma_fields(snapshot: TickerSnapshot) -> bool:
    return all(getattr(snapshot, field_name) is not None for field_name in TRUE_GAMMA_FIELDS)


def _is_yahoo_proxy_only(snapshot: TickerSnapshot) -> bool:
    capabilities = snapshot.option_chain_capabilities if isinstance(snapshot.option_chain_capabilities, dict) else {}
    if capabilities.get("true_gamma_exposure") is True:
        return False
    return snapshot.option_chain_source == YAHOO_OPTION_PROXY_SOURCE


def _gamma_source_name(snapshot: TickerSnapshot) -> str:
    return (
        _text(snapshot.option_chain_source)
        or _text(snapshot.option_chain_provider)
        or "option_chain_records"
    )


def _first_present(data: Mapping[str, Any], aliases: tuple[str, ...]) -> object:
    for alias in aliases:
        if alias in data:
            return data[alias]
    return None


def _option_side(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    normalized = text.lower()
    if normalized in {"call", "calls", "c"}:
        return "call"
    if normalized in {"put", "puts", "p"}:
        return "put"
    return None


def _side_from_contract(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    match = _OCC_CONTRACT_PATTERN.search(text)
    if match is None:
        return None
    return "call" if match.group(2).upper() == "C" else "put"


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if not isinstance(value, str):
        return str(value).strip() or None
    text = value.strip()
    return text or None


def _date_text(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    if "T" in text:
        return text.split("T", 1)[0]
    return text


def _timestamp_text(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return _text(value)


def _to_float(value: object, *, percent: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    had_percent = False
    if isinstance(value, str):
        had_percent = "%" in value
        value = value.replace(",", "").replace("%", "").strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if percent and had_percent:
        return number / 100.0
    return number


def _positive_float(value: object) -> float | None:
    number = _to_float(value)
    if number is None or number <= 0:
        return None
    return number


def _bool_mapping(value: Mapping[str, object] | None) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): bool(item) for key, item in value.items()}


def _pct_of_market_cap(value: float, market_cap: float | None) -> float | None:
    market_cap_value = _to_float(market_cap)
    if market_cap_value is None or market_cap_value <= 0:
        return None
    return value / market_cap_value * 100.0


def _max_numeric_key(values: Mapping[float, float]) -> float | None:
    if not values:
        return None
    return max(sorted(values), key=lambda key: values[key])


def _max_text_key(values: Mapping[str, float]) -> str | None:
    if not values:
        return None
    return max(sorted(values), key=lambda key: values[key])


def _concentration_pct(values: Mapping[object, float], total: float) -> float | None:
    if not values or total <= 0:
        return None
    return max(values.values()) / total * 100.0
