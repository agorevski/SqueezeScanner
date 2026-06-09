from __future__ import annotations

import concurrent.futures
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from .domain import DataProviderError, InvalidSymbolError, MarketDataProvider, ScanResult
from .scoring import score_snapshot, scoring_model_metadata

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


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

