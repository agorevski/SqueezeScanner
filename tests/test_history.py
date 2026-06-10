import json
import sqlite3

from squeeze_scanner.cache import CachedMarketDataProvider
from squeeze_scanner.domain import TickerSnapshot
from squeeze_scanner.history import ScoreHistoryStore
from squeeze_scanner.scoring import SCORING_MODEL_VERSION
from squeeze_scanner.service import ScannerService, recompute_score_history


class ChangingProvider:
    def __init__(self):
        self.calls = 0

    def fetch(self, symbol):
        self.calls += 1
        if self.calls == 1:
            short_percent_float = 12.0
            short_ratio = 2.0
        else:
            short_percent_float = 60.0
            short_ratio = 15.0
        return TickerSnapshot(
            symbol=symbol,
            company_name=f"{symbol} Co",
            price=10.0,
            volume=2_000_000,
            avg_volume_20d=1_000_000,
            short_percent_float=short_percent_float,
            short_ratio=short_ratio,
            float_shares=20_000_000,
            change_5d_pct=8.0,
            change_20d_pct=12.0,
        )


def test_score_history_is_written_once_per_refreshed_raw_snapshot(tmp_path):
    now = [1_000.0]
    db_path = tmp_path / "market.sqlite3"
    provider = ChangingProvider()
    cached_provider = CachedMarketDataProvider(provider, db_path, clock=lambda: now[0])
    history_store = ScoreHistoryStore(db_path, clock=lambda: now[0])
    scanner = ScannerService(cached_provider, history_store=history_store)

    first_payload = scanner.scan("BYND")
    repeated_payload = scanner.scan("BYND")

    assert provider.calls == 1
    assert first_payload["results"][0]["previous_scan_delta"] is None
    assert first_payload["results"][0]["delta_24h"] is None
    assert first_payload["results"][0]["score_delta_status"] == "not_enough_history"
    assert repeated_payload["results"][0]["previous_scan_delta"] is None

    with sqlite3.connect(db_path) as connection:
        raw_count = connection.execute("SELECT COUNT(*) FROM market_data_history").fetchone()[0]
        score_rows = connection.execute(
            """
            SELECT scoring_model_version, scan_run_id, model_confidence_json, risk_flags_json
            FROM scan_score_history
            ORDER BY id
            """
        ).fetchall()

    assert raw_count == 1
    assert [(row[0], row[1]) for row in score_rows] == [(SCORING_MODEL_VERSION, "live")]
    assert "gamma_candidate" in json.loads(score_rows[0][2])
    assert json.loads(score_rows[0][3])

    previous_score = first_payload["results"][0]["score"]
    now[0] += 86_401
    refreshed_payload = scanner.scan("BYND")
    refreshed_result = refreshed_payload["results"][0]

    with sqlite3.connect(db_path) as connection:
        raw_count = connection.execute("SELECT COUNT(*) FROM market_data_history").fetchone()[0]
        score_count = connection.execute("SELECT COUNT(*) FROM scan_score_history").fetchone()[0]

    assert provider.calls == 2
    assert raw_count == 2
    assert score_count == 2
    assert refreshed_result["previous_scan_delta"] == round(refreshed_result["score"] - previous_score, 1)
    assert refreshed_result["delta_24h"] == round(refreshed_result["score"] - previous_score, 1)


def test_history_queries_and_recompute_return_expected_rows(tmp_path):
    now = [1_000.0]
    db_path = tmp_path / "market.sqlite3"
    cached_provider = CachedMarketDataProvider(ChangingProvider(), db_path, clock=lambda: now[0])
    history_store = ScoreHistoryStore(db_path, clock=lambda: now[0])
    scanner = ScannerService(cached_provider, history_store=history_store)

    scanner.scan("BYND")
    now[0] += 3_601
    scanner.scan("BYND")

    symbol_rows = history_store.history_for_symbol("BYND", limit=10)
    assert [row["symbol"] for row in symbol_rows] == ["BYND", "BYND"]
    assert {row["scoring_model_version"] for row in symbol_rows} == {SCORING_MODEL_VERSION}
    assert all(row["raw_history_id"] is not None for row in symbol_rows)
    assert all("hybrid" in row["model_confidence"] for row in symbol_rows)
    assert all(isinstance(row["risk_flags"], list) for row in symbol_rows)

    filtered_rows = history_store.query_history(
        from_timestamp=4_000,
        min_score=symbol_rows[0]["score"],
        primary_model=symbol_rows[0]["primary_model"],
    )
    assert len(filtered_rows) == 1
    assert filtered_rows[0]["id"] == symbol_rows[0]["id"]

    recomputed = recompute_score_history(cached_provider, history_store, symbols=["BYND"], limit=1)

    assert recomputed["count"] == 1
    assert recomputed["scan_run_id"].startswith("recompute:")
    assert recomputed["results"][0]["symbol"] == "BYND"
    with sqlite3.connect(db_path) as connection:
        score_count = connection.execute("SELECT COUNT(*) FROM scan_score_history").fetchone()[0]
    assert score_count == 3
