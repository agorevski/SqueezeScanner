import csv
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest

from squeeze_scanner.analytics import AnalyticsStore, PriceBar
from squeeze_scanner.config import get_settings
from squeeze_scanner.web import create_app


BASE = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)


@pytest.fixture
def store():
    db_path = Path("data") / f".test-analytics-{uuid4().hex}.sqlite3"
    try:
        yield AnalyticsStore(db_path)
    finally:
        for path in (db_path, db_path.with_name(f"{db_path.name}-wal"), db_path.with_name(f"{db_path.name}-shm")):
            path.unlink(missing_ok=True)


def add_score(
    store: AnalyticsStore,
    symbol: str,
    score: float,
    created_at: datetime,
    *,
    model: str = "hybrid",
    metrics: dict | None = None,
    components: dict | None = None,
    risk_flags: list[str] | None = None,
) -> int:
    return store.insert_score_history(
        {
            "symbol": symbol,
            "company_name": f"{symbol} Inc",
            "score": score,
            "risk_level": "High setup" if score >= 70 else "Watchlist",
            "data_quality": 100,
            "primary_model": model,
            "model_scores": {model: score},
            "model_components": components or {model: {"relative_volume": score / 2}},
            "metrics": metrics or {"price": 10.0, "avg_volume_20d": 1_000_000},
            "risk_flags": risk_flags or [],
        },
        created_at=created_at,
        scoring_model_version="test-v1",
    )


def test_outcomes_wait_for_elapsed_horizon_and_use_stored_scan_score(store):
    score_id = add_score(store, "WAIT", 80, BASE, model="classical_short_squeeze")
    store.insert_price_history(
        [
            PriceBar("WAIT", BASE + timedelta(minutes=30), close=50, high=55, low=9),
            PriceBar("WAIT", BASE + timedelta(hours=1), close=12, high=13, low=11),
        ]
    )

    assert store.compute_due_outcomes(as_of=BASE + timedelta(minutes=59), horizons={"1h": 3600}) == []

    outcomes = store.compute_due_outcomes(as_of=BASE + timedelta(hours=1), horizons={"1h": 3600})

    assert len(outcomes) == 1
    assert outcomes[0]["scan_score_history_id"] == score_id
    assert outcomes[0]["score_at_scan"] == 80
    assert outcomes[0]["forward_return_pct"] == 20

    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_outcomes").fetchone()[0] == 1


def test_calibration_report_buckets_returns_and_win_rate(store):
    for symbol, score, exit_price in [
        ("WIN", 82, 11),
        ("LOSS", 88, 9),
        ("STRONG", 95, 12),
    ]:
        add_score(store, symbol, score, BASE, model="hybrid")
        store.insert_price_bar(PriceBar(symbol, BASE + timedelta(days=1), close=exit_price))
    store.compute_due_outcomes(as_of=BASE + timedelta(days=1), horizons={"1d": 86_400})

    report = store.calibration_report(model="hybrid", horizon="1d")

    assert report == [
        {
            "model": "hybrid",
            "horizon": "1d",
            "horizon_seconds": 86_400,
            "score_bucket": "80-90",
            "bucket_start": 80,
            "bucket_end": 90,
            "count": 2,
            "avg_return_pct": 0,
            "win_rate": 0.5,
            "avg_max_favorable_excursion_pct": 0,
            "avg_max_adverse_excursion_pct": 0,
            "worst_max_adverse_excursion_pct": -10,
        },
        {
            "model": "hybrid",
            "horizon": "1d",
            "horizon_seconds": 86_400,
            "score_bucket": "90-100",
            "bucket_start": 90,
            "bucket_end": 100,
            "count": 1,
            "avg_return_pct": 20,
            "win_rate": 1,
            "avg_max_favorable_excursion_pct": 20,
            "avg_max_adverse_excursion_pct": 20,
            "worst_max_adverse_excursion_pct": 20,
        },
    ]


def test_delta_explanations_are_structured_and_deterministic(store):
    add_score(
        store,
        "MOVE",
        40,
        BASE,
        model="float_compression",
        metrics={"price": 10, "relative_volume": 2, "short_percent_float": 30},
        components={"float_compression": {"relative_volume": 10, "tiny_float": 20}},
    )
    add_score(
        store,
        "MOVE",
        75,
        BASE + timedelta(hours=1),
        model="float_compression",
        metrics={"price": 12, "relative_volume": 5, "short_percent_float": 35},
        components={"float_compression": {"relative_volume": 30, "tiny_float": 20}},
        risk_flags=["halt_risk"],
    )

    first = store.explain_score_deltas("MOVE", windows=("previous",), driver_limit=4)
    second = store.explain_score_deltas("MOVE", windows=("previous",), driver_limit=4)

    assert first == second
    window = first["windows"][0]
    assert window["status"] == "ok"
    assert window["score_delta"] == 35
    assert [(driver["type"], driver.get("model"), driver["name"], driver["delta"]) for driver in window["drivers"]] == [
        ("model_score", "float_compression", "float_compression", 35),
        ("component", "float_compression", "relative_volume", 20),
        ("metric", None, "short_percent_float", 5),
        ("metric", None, "relative_volume", 3),
    ]


def test_delta_explanations_return_not_enough_history(store):
    add_score(store, "SOLO", 50, BASE)

    payload = store.explain_score_deltas("SOLO", windows=("previous", "1h"))

    assert payload["status"] == "ok"
    assert [window["status"] for window in payload["windows"]] == [
        "not_enough_history",
        "not_enough_history",
    ]
    assert all(window["drivers"] == [] for window in payload["windows"])
    assert "No" in payload["windows"][0]["reason"]


def test_reports_use_history_and_csv_export_matches_visible_rows(store):
    add_score(store, "OLD", 80, BASE - timedelta(days=1))
    add_score(store, "OLD", 95, BASE + timedelta(hours=1))
    add_score(store, "NEW", 90, BASE + timedelta(hours=2))
    add_score(store, "LOW", 60, BASE + timedelta(hours=2))
    add_score(store, "JUMP", 20, BASE)
    add_score(store, "JUMP", 65, BASE + timedelta(hours=1))
    add_score(store, "REPEAT", 72, BASE + timedelta(hours=3))
    add_score(store, "REPEAT", 74, BASE + timedelta(hours=4))
    add_score(store, "DROP", 90, BASE)
    add_score(store, "DROP", 40, BASE + timedelta(days=1))

    visible_rows = store.report_top_new_high_setups(
        start_at=BASE,
        end_at=BASE + timedelta(days=1),
        min_score=70,
    )
    assert [row["symbol"] for row in visible_rows] == ["DROP", "NEW", "REPEAT"]

    increases = store.report_biggest_1h_increases(
        start_at=BASE + timedelta(hours=1),
        end_at=BASE + timedelta(hours=1),
    )
    assert increases[0]["symbol"] == "JUMP"
    assert increases[0]["score_delta"] == 45

    repeated = store.report_repeated_high_setups(
        start_at=BASE,
        end_at=BASE + timedelta(days=1),
        min_score=70,
        min_count=2,
    )
    assert repeated[0]["symbol"] == "REPEAT"
    assert repeated[0]["setup_count"] == 2

    deterioration = store.report_deterioration(
        start_at=BASE + timedelta(days=1),
        end_at=BASE + timedelta(days=1),
    )
    assert deterioration[0]["symbol"] == "DROP"
    assert deterioration[0]["score_drop"] == 50

    columns = ["rank", "symbol", "score"]
    csv_text = store.rows_to_csv(visible_rows, columns=columns)
    parsed_rows = list(csv.DictReader(StringIO(csv_text)))

    assert parsed_rows == [{column: str(row[column]) for column in columns} for row in visible_rows]


def test_reports_api_uses_persisted_history_and_delta_explanations(tmp_path, monkeypatch):
    db_path = tmp_path / "scanner.sqlite3"
    monkeypatch.setenv("SQUEEZE_SCANNER_CACHE_DB", str(db_path))
    get_settings.cache_clear()
    try:
        store = AnalyticsStore(db_path)
        add_score(store, "API", 50, BASE, model="hybrid")
        add_score(store, "API", 82, BASE + timedelta(hours=1), model="hybrid")

        app = create_app()
        catalog = asyncio.run(_route_endpoint(app, "/api/reports")())
        assert any(report["path"] == "/api/reports/top-new-high-setups" for report in catalog["reports"])

        report_payload = asyncio.run(
            _route_endpoint(app, "/api/reports/top-new-high-setups")(
                from_time=BASE.isoformat(),
                to_time=(BASE + timedelta(hours=2)).isoformat(),
                min_score=70,
                limit=50,
                offset=0,
            )
        )
        assert report_payload["report"] == "top_new_high_setups"
        assert [row["symbol"] for row in report_payload["rows"]] == ["API"]

        deltas = asyncio.run(_route_endpoint(app, "/api/scans/{symbol}/deltas")(symbol="API"))
        previous = deltas["windows"][0]
        assert previous["status"] == "ok"
        assert previous["score_delta"] == 32
    finally:
        get_settings.cache_clear()


def _route_endpoint(app, path: str):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route not found: {path}")
