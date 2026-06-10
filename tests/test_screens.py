from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from squeeze_scanner.screens import ScreenStore, scan_watchlist
from squeeze_scanner.service import build_scan_response


class DummyResult:
    def __init__(
        self,
        symbol: str,
        *,
        score: float,
        model_scores: dict[str, float] | None = None,
        metrics: dict[str, float] | None = None,
        data_quality: float = 100.0,
        **extra: Any,
    ) -> None:
        self.symbol = symbol
        self.company_name = None
        self.score = score
        self.risk_level = "Low"
        self.data_quality = data_quality
        self.primary_model = "hybrid"
        self.model_scores = model_scores or {}
        self.model_components: dict[str, dict[str, float]] = {}
        self.model_rationales: dict[str, list[str]] = {}
        self.metrics = metrics or {}
        self.components: dict[str, float] = {}
        self.rationale: list[str] = []
        self.warnings: list[str] = []
        self._extra = extra
        for key, value in extra.items():
            setattr(self, key, value)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "symbol": self.symbol,
            "company_name": self.company_name,
            "score": self.score,
            "risk_level": self.risk_level,
            "data_quality": self.data_quality,
            "primary_model": self.primary_model,
            "model_scores": self.model_scores,
            "model_components": self.model_components,
            "model_rationales": self.model_rationales,
            "metrics": self.metrics,
            "components": self.components,
            "rationale": self.rationale,
            "warnings": self.warnings,
        }
        payload.update(self._extra)
        return payload


def test_saved_screen_crud_persists_structured_filters(tmp_path):
    db_path = tmp_path / "scanner.sqlite3"
    store = ScreenStore(db_path)
    filters = {
        "filters": {"min_score": 50, "risk": ["High setup"]},
        "ranking": {"mode": "selected_model_score", "selected_model": "gamma_candidate"},
    }

    created = store.create_screen("  Gamma screen  ", filters)

    assert created["name"] == "Gamma screen"
    assert created["filters"] == filters
    assert ScreenStore(db_path).list_screens()[0]["filters"]["ranking"]["selected_model"] == "gamma_candidate"

    updated = ScreenStore(db_path).update_screen(
        created["id"],
        name="Hybrid screen",
        filters={"ranking": {"mode": "hybrid_only", "sort_direction": "desc"}},
    )

    assert updated is not None
    assert updated["name"] == "Hybrid screen"
    assert updated["filters"] == {"ranking": {"mode": "hybrid_only", "sort_direction": "desc"}}

    with sqlite3.connect(db_path) as connection:
        stored_json = connection.execute("SELECT filters_json FROM saved_screens").fetchone()[0]
    assert '"mode":"hybrid_only"' in stored_json

    assert store.delete_screen(created["id"]) is True
    assert ScreenStore(db_path).list_screens() == []


def test_watchlist_crud_and_scan_uses_persisted_symbols(tmp_path):
    class RecordingScanner:
        def __init__(self) -> None:
            self.calls: list[tuple[list[str], int, dict[str, str | None]]] = []

        def scan(
            self,
            symbols,
            max_symbols=25,
            ranking_mode=None,
            selected_model=None,
            sort_direction=None,
        ):
            self.calls.append(
                (
                    list(symbols),
                    max_symbols,
                    {
                        "ranking_mode": ranking_mode,
                        "selected_model": selected_model,
                        "sort_direction": sort_direction,
                    },
                )
            )
            return {"count": len(symbols), "results": [{"symbol": symbol} for symbol in symbols], "errors": []}

    store = ScreenStore(tmp_path / "scanner.sqlite3")
    watchlist = store.create_watchlist("  Favorites  ", "gme, amc, GME")

    assert watchlist["name"] == "Favorites"
    assert watchlist["symbols"] == ["AMC", "GME"]

    updated = store.add_symbols(watchlist["id"], ["bynd", "GME"])
    assert updated is not None
    assert updated["symbols"] == ["AMC", "BYND", "GME"]
    assert store.remove_symbol(watchlist["id"], "amc") is True

    scanner = RecordingScanner()
    payload = scan_watchlist(
        store,
        scanner,
        watchlist["id"],
        ranking_mode="relative_volume",
        selected_model="hybrid",
        sort_direction="desc",
    )

    assert scanner.calls == [
        (
            ["BYND", "GME"],
            2,
            {"ranking_mode": "relative_volume", "selected_model": "hybrid", "sort_direction": "desc"},
        )
    ]
    assert payload is not None
    assert payload["watchlist_id"] == watchlist["id"]
    assert payload["symbols"] == ["BYND", "GME"]

    renamed = store.update_watchlist(watchlist["id"], name="Active trades")
    assert renamed is not None
    assert renamed["name"] == "Active trades"
    assert store.delete_watchlist(watchlist["id"]) is True
    assert store.list_watchlists() == []


def test_empty_watchlist_scan_returns_empty_response_without_scanner_call(tmp_path):
    class FailingScanner:
        def scan(self, *args, **kwargs):
            raise AssertionError("empty watchlists should not call the scanner")

    store = ScreenStore(tmp_path / "scanner.sqlite3")
    watchlist = store.create_watchlist("Empty")

    payload = scan_watchlist(store, FailingScanner(), watchlist["id"])

    assert payload is not None
    assert payload["count"] == 0
    assert payload["watchlist_id"] == watchlist["id"]
    assert payload["symbols"] == []


def _ranking_results() -> list[DummyResult]:
    return [
        DummyResult(
            "TOP",
            score=100,
            model_scores={"gamma_candidate": 10, "hybrid": 20},
            metrics={"relative_volume": 1, "short_percent_float": 10, "float_shares": 50_000_000},
            model_confidences={"hybrid": 0.10},
            score_delta_1h=1,
            score_delta_24h=1,
        ),
        DummyResult(
            "MODEL",
            score=60,
            model_scores={"gamma_candidate": 95, "hybrid": 30},
            metrics={"relative_volume": 2, "short_percent_float": 20, "float_shares": 40_000_000},
            model_confidences={"gamma_candidate": 0.20},
            score_delta_1h=2,
            score_delta_24h=2,
        ),
        DummyResult(
            "CONF",
            score=50,
            model_scores={"gamma_candidate": 40, "hybrid": 40},
            metrics={"relative_volume": 3, "short_percent_float": 30, "float_shares": 30_000_000},
            model_confidences={"hybrid": 0.95},
            score_delta_1h=3,
            score_delta_24h=3,
        ),
        DummyResult(
            "HOUR",
            score=40,
            model_scores={"gamma_candidate": 30, "hybrid": 50},
            metrics={
                "relative_volume": 4,
                "short_percent_float": 40,
                "float_shares": 20_000_000,
                "score_delta_1h": 12,
            },
            model_confidences={"hybrid": 0.30},
            score_delta_24h=4,
        ),
        DummyResult(
            "DAY",
            score=30,
            model_scores={"gamma_candidate": 20, "hybrid": 60},
            metrics={
                "relative_volume": 5,
                "short_percent_float": 50,
                "float_shares": 10_000_000,
                "score_delta_24h": 15,
            },
            model_confidences={"hybrid": 0.40},
            score_delta_1h=4,
        ),
        DummyResult(
            "RVOL",
            score=20,
            model_scores={"gamma_candidate": 15, "hybrid": 70},
            metrics={"relative_volume": 10, "short_percent_float": 60, "float_shares": 5_000_000},
            model_confidences={"hybrid": 0.50},
            score_delta_1h=5,
            score_delta_24h=5,
        ),
        DummyResult(
            "SHORT",
            score=10,
            model_scores={"gamma_candidate": 5, "hybrid": 80},
            metrics={"relative_volume": 6, "short_percent_float": 99, "float_shares": 4_000_000},
            model_confidences={"hybrid": 0.60},
            score_delta_1h=6,
            score_delta_24h=6,
        ),
        DummyResult(
            "FLOAT",
            score=5,
            model_scores={"gamma_candidate": 4, "hybrid": 90},
            metrics={"relative_volume": 7, "short_percent_float": 70, "float_shares": 1_000_000},
            model_confidences={"hybrid": 0.70},
            score_delta_1h=7,
            score_delta_24h=7,
        ),
    ]


@pytest.mark.parametrize(
    ("ranking_mode", "kwargs", "expected_symbol"),
    [
        ("top_score", {}, "TOP"),
        ("selected_model_score", {"selected_model": "gamma_candidate"}, "MODEL"),
        ("highest_model_confidence", {}, "CONF"),
        ("score_increase_1h", {}, "HOUR"),
        ("score_increase_24h", {}, "DAY"),
        ("relative_volume", {}, "RVOL"),
        ("short_interest", {}, "SHORT"),
        ("smallest_float", {}, "FLOAT"),
        ("hybrid_only", {}, "FLOAT"),
        ("gamma_candidate_only", {}, "MODEL"),
    ],
)
def test_scan_response_supports_ranking_modes(ranking_mode, kwargs, expected_symbol):
    payload = build_scan_response(_ranking_results(), ranking_mode=ranking_mode, **kwargs)

    assert payload["ranking"]["mode"] == ranking_mode
    assert payload["results"][0]["symbol"] == expected_symbol


def test_ranking_modes_are_safe_when_optional_fields_are_absent():
    results = [
        DummyResult("LOW", score=10, model_scores={}, metrics={}),
        DummyResult("HIGH", score=90, model_scores={}, metrics={}),
    ]

    confidence_payload = build_scan_response(results, ranking_mode="highest_model_confidence")
    delta_payload = build_scan_response(results, ranking_mode="score_increase_24h")

    assert [result["symbol"] for result in confidence_payload["results"]] == ["HIGH", "LOW"]
    assert [result["symbol"] for result in delta_payload["results"]] == ["HIGH", "LOW"]
