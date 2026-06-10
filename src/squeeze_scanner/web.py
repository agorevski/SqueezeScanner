from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .analytics import AnalyticsStore
from .automation import AlertNotFoundError, AutomationError, AutomationScheduler, AutomationService, ScheduleNotFoundError
from .cache import CachedMarketDataProvider
from .config import PACKAGE_ROOT, get_settings
from .domain import InvalidRankingModeError, InvalidSymbolError, ScreenerError
from .history import ScoreHistoryStore, parse_history_timestamp
from .providers.yahoo import YahooFinanceProvider, YahooFinanceScreener
from .scoring import score_snapshot, scoring_model_metadata
from .screens import ScreenStore, ScreenStoreError, scan_watchlist
from .service import ScannerService, build_scan_response, normalize_symbols, recompute_score_history


class ScanRequest(BaseModel):
    symbols: str | list[str]
    ranking_mode: str | None = None
    selected_model: str | None = None
    sort_direction: str | None = None


class SavedScreenRequest(BaseModel):
    name: str
    filters: dict[str, Any] | None = None
    filters_json: dict[str, Any] | None = None


class SavedScreenUpdateRequest(BaseModel):
    name: str | None = None
    filters: dict[str, Any] | None = None
    filters_json: dict[str, Any] | None = None


class WatchlistRequest(BaseModel):
    name: str
    symbols: str | list[str] | None = None


class WatchlistUpdateRequest(BaseModel):
    name: str | None = None


class WatchlistSymbolsRequest(BaseModel):
    symbols: str | list[str]


class RankingRequest(BaseModel):
    ranking_mode: str | None = None
    selected_model: str | None = None
    sort_direction: str | None = None


class RecomputeRequest(BaseModel):
    symbols: str | list[str] | None = None
    from_time: str | None = Field(default=None, alias="from")
    to: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class ScheduledScanRequest(BaseModel):
    name: str
    target_type: str
    target: dict[str, Any] = Field(default_factory=dict)
    interval_seconds: int
    enabled: bool = True
    next_run_at: str | None = None


class ScheduledScanUpdateRequest(BaseModel):
    name: str | None = None
    target_type: str | None = None
    target: dict[str, Any] | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None
    next_run_at: str | None = None


class AlertRequest(BaseModel):
    name: str
    rule: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class AlertUpdateRequest(BaseModel):
    name: str | None = None
    rule: dict[str, Any] | None = None
    enabled: bool | None = None


def create_app() -> FastAPI:
    settings = get_settings()
    market_data_provider = CachedMarketDataProvider(
        YahooFinanceProvider(),
        db_path=settings.cache_db_path,
        ttl_seconds=settings.cache_ttl_seconds,
    )
    history_store = ScoreHistoryStore(settings.cache_db_path)
    analytics_store = AnalyticsStore(settings.cache_db_path)
    scanner = ScannerService(market_data_provider, history_store=history_store)
    screen_store = ScreenStore(settings.cache_db_path)
    yahoo_screener = YahooFinanceScreener()
    automation = AutomationService(settings.cache_db_path, scanner=scanner, yahoo_screener=yahoo_screener)
    automation_scheduler = AutomationScheduler(automation)

    app = FastAPI(
        title="Squeeze Scanner",
        description="Ranks equities across independent squeeze setup models using public market data.",
        version="0.1.0",
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))

    @app.on_event("startup")
    async def start_automation_scheduler() -> None:
        automation_scheduler.start()

    @app.on_event("shutdown")
    async def stop_automation_scheduler() -> None:
        automation_scheduler.stop()

    @app.middleware("http")
    async def add_no_cache_headers(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith(("/static/", "/api/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/model")
    async def model() -> dict:
        return scoring_model_metadata()

    @app.get("/api/scans/recent")
    async def recent_scans(
        ranking_mode: str | None = None,
        selected_model: str | None = None,
        sort_direction: str | None = None,
    ) -> dict:
        snapshots = await asyncio.to_thread(market_data_provider.recent_snapshots)
        results = [score_snapshot(snapshot) for snapshot in snapshots]
        scan_times = await asyncio.to_thread(market_data_provider.scan_times, [result.symbol for result in results])
        score_deltas = await asyncio.to_thread(scanner.deltas_for_results, results)
        try:
            return build_scan_response(
                results,
                scan_times=scan_times,
                score_deltas=score_deltas,
                ranking_mode=ranking_mode,
                selected_model=selected_model,
                sort_direction=sort_direction,
            )
        except InvalidRankingModeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/scans/history")
    async def scan_history(
        symbol: str | None = None,
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        limit: int = Query(100, ge=1, le=1000),
        primary_model: str | None = None,
        model: str | None = None,
        min_score: float | None = Query(None, ge=0),
        max_score: float | None = Query(None, le=100),
        risk_level: str | None = None,
        scoring_model_version: str | None = None,
    ) -> dict[str, Any]:
        try:
            normalized_symbol = normalize_symbols(symbol, max_symbols=1)[0] if symbol else None
            rows = await asyncio.to_thread(
                history_store.query_history,
                symbol=normalized_symbol,
                from_timestamp=_parse_history_time_param(from_time),
                to_timestamp=_parse_history_time_param(to_time),
                limit=limit,
                primary_model=primary_model or model,
                min_score=min_score,
                max_score=max_score,
                risk_level=risk_level,
                scoring_model_version=scoring_model_version,
            )
        except (InvalidSymbolError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"count": len(rows), "results": rows}

    @app.get("/api/scans/{symbol}/history")
    async def scan_symbol_history(
        symbol: str,
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        limit: int = Query(100, ge=1, le=1000),
        primary_model: str | None = None,
        model: str | None = None,
        min_score: float | None = Query(None, ge=0),
        max_score: float | None = Query(None, le=100),
        risk_level: str | None = None,
        scoring_model_version: str | None = None,
    ) -> dict[str, Any]:
        try:
            normalized_symbol = normalize_symbols(symbol, max_symbols=1)[0]
            rows = await asyncio.to_thread(
                history_store.history_for_symbol,
                normalized_symbol,
                from_timestamp=_parse_history_time_param(from_time),
                to_timestamp=_parse_history_time_param(to_time),
                limit=limit,
                primary_model=primary_model or model,
                min_score=min_score,
                max_score=max_score,
                risk_level=risk_level,
                scoring_model_version=scoring_model_version,
            )
        except (InvalidSymbolError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"symbol": normalized_symbol, "count": len(rows), "results": rows}

    @app.get("/api/scans/{symbol}/deltas")
    async def scan_symbol_deltas(symbol: str) -> dict[str, Any]:
        try:
            normalized_symbol = normalize_symbols(symbol, max_symbols=1)[0]
            return await asyncio.to_thread(analytics_store.explain_score_deltas, normalized_symbol)
        except (InvalidSymbolError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/scans/recompute")
    async def recompute_scans(request: RecomputeRequest | None = None) -> dict[str, Any]:
        request = request or RecomputeRequest()
        try:
            symbols = normalize_symbols(request.symbols, max_symbols=250) if request.symbols else None
            return await asyncio.to_thread(
                recompute_score_history,
                market_data_provider,
                history_store,
                symbols=symbols,
                from_timestamp=_parse_history_time_param(request.from_time),
                to_timestamp=_parse_history_time_param(request.to),
                limit=request.limit,
            )
        except (InvalidSymbolError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/scans/{symbol}")
    async def delete_scan(symbol: str) -> dict[str, str | bool]:
        try:
            normalized_symbol = normalize_symbols(symbol, max_symbols=1)[0]
        except InvalidSymbolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        deleted = await asyncio.to_thread(market_data_provider.delete, normalized_symbol)
        return {"symbol": normalized_symbol, "deleted": deleted}

    @app.post("/api/scan")
    async def scan(request: ScanRequest) -> dict:
        try:
            return await asyncio.to_thread(
                scanner.scan,
                request.symbols,
                25,
                request.ranking_mode,
                request.selected_model,
                request.sort_direction,
            )
        except InvalidSymbolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except InvalidRankingModeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/scan/most-shorted")
    async def scan_most_shorted(
        count: int = Query(100, ge=1, le=250),
        ranking_mode: str | None = None,
        selected_model: str | None = None,
        sort_direction: str | None = None,
    ) -> dict:
        try:
            symbols = await asyncio.to_thread(yahoo_screener.most_shorted_symbols, count)
            return await asyncio.to_thread(
                scanner.scan,
                symbols,
                count,
                ranking_mode,
                selected_model,
                sort_direction,
            )
        except ScreenerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except InvalidSymbolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except InvalidRankingModeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/reports")
    async def list_reports() -> dict[str, list[dict[str, str]]]:
        return {
            "reports": [
                {
                    "name": "Top new high setups",
                    "path": "/api/reports/top-new-high-setups",
                    "description": "Symbols reaching high setup scores in the selected historical window.",
                },
                {
                    "name": "Biggest 1h score increases",
                    "path": "/api/reports/biggest-1h-increases",
                    "description": "Largest stored score increases versus the prior 1-hour baseline.",
                },
                {
                    "name": "Biggest 24h score increases",
                    "path": "/api/reports/biggest-24h-increases",
                    "description": "Largest stored score increases versus the prior 24-hour baseline.",
                },
                {
                    "name": "Repeated high setups",
                    "path": "/api/reports/repeated-high-setups",
                    "description": "Symbols repeatedly meeting the high setup threshold.",
                },
                {
                    "name": "Score deterioration",
                    "path": "/api/reports/deterioration",
                    "description": "Symbols with the largest score drops over a comparison window.",
                },
                {
                    "name": "Calibration buckets",
                    "path": "/api/reports/calibration",
                    "description": "Outcome statistics grouped by model, horizon, and score bucket.",
                },
            ]
        }

    @app.get("/api/reports/top-new-high-setups")
    async def report_top_new_high_setups(
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        model: str | None = None,
        min_score: float = Query(70.0, ge=0, le=100),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        try:
            start_at, end_at = _report_window(from_time, to_time)
            rows = await asyncio.to_thread(
                analytics_store.report_top_new_high_setups,
                start_at=start_at,
                end_at=end_at,
                model=model,
                min_score=min_score,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report_payload("top_new_high_setups", rows, start_at, end_at)

    @app.get("/api/reports/biggest-1h-increases")
    async def report_biggest_1h_increases(
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        model: str | None = None,
        min_delta: float = 0.0,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        try:
            start_at, end_at = _report_window(from_time, to_time)
            rows = await asyncio.to_thread(
                analytics_store.report_biggest_1h_increases,
                start_at=start_at,
                end_at=end_at,
                model=model,
                min_delta=min_delta,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report_payload("biggest_1h_increases", rows, start_at, end_at)

    @app.get("/api/reports/biggest-24h-increases")
    async def report_biggest_24h_increases(
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        model: str | None = None,
        min_delta: float = 0.0,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        try:
            start_at, end_at = _report_window(from_time, to_time)
            rows = await asyncio.to_thread(
                analytics_store.report_biggest_24h_increases,
                start_at=start_at,
                end_at=end_at,
                model=model,
                min_delta=min_delta,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report_payload("biggest_24h_increases", rows, start_at, end_at)

    @app.get("/api/reports/repeated-high-setups")
    async def report_repeated_high_setups(
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        model: str | None = None,
        min_score: float = Query(70.0, ge=0, le=100),
        min_count: int = Query(2, ge=2),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        try:
            start_at, end_at = _report_window(from_time, to_time)
            rows = await asyncio.to_thread(
                analytics_store.report_repeated_high_setups,
                start_at=start_at,
                end_at=end_at,
                model=model,
                min_score=min_score,
                min_count=min_count,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report_payload("repeated_high_setups", rows, start_at, end_at)

    @app.get("/api/reports/deterioration")
    async def report_deterioration(
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        window: str = "24h",
        model: str | None = None,
        min_drop: float = 0.0,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        try:
            start_at, end_at = _report_window(from_time, to_time)
            rows = await asyncio.to_thread(
                analytics_store.report_deterioration,
                window=window,
                start_at=start_at,
                end_at=end_at,
                model=model,
                min_drop=min_drop,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report_payload("deterioration", rows, start_at, end_at)

    @app.get("/api/reports/calibration")
    async def report_calibration(
        model: str = "hybrid",
        horizon: str = "1d",
        bucket_size: float = Query(10.0, gt=0, le=100),
    ) -> dict[str, Any]:
        try:
            rows = await asyncio.to_thread(
                analytics_store.calibration_report,
                model=model,
                horizon=horizon,
                bucket_size=bucket_size,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"report": "calibration", "count": len(rows), "rows": rows}

    @app.get("/api/scheduled-scans")
    async def list_scheduled_scans() -> list[dict[str, Any]]:
        return await asyncio.to_thread(automation.list_schedules)

    @app.post("/api/scheduled-scans")
    async def create_scheduled_scan(request: ScheduledScanRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                automation.create_schedule,
                request.name,
                request.target_type,
                request.target,
                request.interval_seconds,
                request.enabled,
                request.next_run_at,
            )
        except AutomationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/scheduled-scans/{schedule_id}")
    async def get_scheduled_scan(schedule_id: int) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(automation.get_schedule, schedule_id)
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/api/scheduled-scans/{schedule_id}")
    async def update_scheduled_scan(schedule_id: int, request: ScheduledScanUpdateRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                automation.update_schedule,
                schedule_id,
                name=request.name,
                target_type=request.target_type,
                target=request.target,
                interval_seconds=request.interval_seconds,
                enabled=request.enabled,
                next_run_at=request.next_run_at,
            )
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AutomationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/scheduled-scans/{schedule_id}")
    async def delete_scheduled_scan(schedule_id: int) -> dict[str, int | bool]:
        deleted = await asyncio.to_thread(automation.delete_schedule, schedule_id)
        return {"id": schedule_id, "deleted": deleted}

    @app.post("/api/scheduled-scans/{schedule_id}/run")
    async def run_scheduled_scan_now(schedule_id: int) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(automation.run_scheduled_scan, schedule_id)
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AutomationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/scheduled-scan-runs")
    async def list_scheduled_scan_runs(
        schedule_id: int | None = None,
        limit: int = Query(50, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(automation.list_runs, schedule_id, limit)

    @app.get("/api/alerts")
    async def list_alerts(enabled_only: bool = False) -> list[dict[str, Any]]:
        return await asyncio.to_thread(automation.list_alerts, enabled_only)

    @app.post("/api/alerts")
    async def create_alert(request: AlertRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(automation.create_alert, request.name, request.rule, request.enabled)
        except AutomationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/alerts/{alert_id}")
    async def update_alert(alert_id: int, request: AlertUpdateRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                automation.update_alert,
                alert_id,
                name=request.name,
                rule=request.rule,
                enabled=request.enabled,
            )
        except AlertNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AutomationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/alerts/{alert_id}")
    async def delete_alert(alert_id: int) -> dict[str, int | bool]:
        deleted = await asyncio.to_thread(automation.delete_alert, alert_id)
        return {"id": alert_id, "deleted": deleted}

    @app.get("/api/alert-events")
    async def list_alert_events(
        alert_id: int | None = None,
        symbol: str | None = None,
        active_only: bool = False,
        limit: int = Query(100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(automation.list_alert_events, alert_id, symbol, active_only, limit)

    @app.post("/api/alert-events/{event_id}/ack")
    async def acknowledge_alert_event(event_id: int) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(automation.acknowledge_alert_event, event_id)
        except AlertNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/screens")
    async def list_screens() -> dict[str, list[dict[str, Any]]]:
        return {"screens": await asyncio.to_thread(screen_store.list_screens)}

    @app.post("/api/screens")
    async def create_screen(request: SavedScreenRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                screen_store.create_screen,
                request.name,
                _filters_from_request(request),
            )
        except ScreenStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/screens/{screen_id}")
    async def update_screen(screen_id: int, request: SavedScreenUpdateRequest) -> dict[str, Any]:
        try:
            if _has_filters(request):
                screen = await asyncio.to_thread(
                    screen_store.update_screen,
                    screen_id,
                    name=request.name,
                    filters=_filters_from_request(request),
                )
            else:
                screen = await asyncio.to_thread(screen_store.update_screen, screen_id, name=request.name)
        except ScreenStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if screen is None:
            raise HTTPException(status_code=404, detail="Saved screen not found.")
        return screen

    @app.delete("/api/screens/{screen_id}")
    async def delete_screen(screen_id: int) -> dict[str, bool]:
        deleted = await asyncio.to_thread(screen_store.delete_screen, screen_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Saved screen not found.")
        return {"deleted": True}

    @app.get("/api/watchlists")
    async def list_watchlists() -> dict[str, list[dict[str, Any]]]:
        return {"watchlists": await asyncio.to_thread(screen_store.list_watchlists)}

    @app.post("/api/watchlists")
    async def create_watchlist(request: WatchlistRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(screen_store.create_watchlist, request.name, request.symbols)
        except (InvalidSymbolError, ScreenStoreError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/watchlists/{watchlist_id}")
    async def update_watchlist(watchlist_id: int, request: WatchlistUpdateRequest) -> dict[str, Any]:
        try:
            watchlist = await asyncio.to_thread(screen_store.update_watchlist, watchlist_id, name=request.name)
        except ScreenStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if watchlist is None:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return watchlist

    @app.delete("/api/watchlists/{watchlist_id}")
    async def delete_watchlist(watchlist_id: int) -> dict[str, bool]:
        deleted = await asyncio.to_thread(screen_store.delete_watchlist, watchlist_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return {"deleted": True}

    @app.get("/api/watchlists/{watchlist_id}/symbols")
    async def list_watchlist_symbols(watchlist_id: int) -> dict[str, int | list[str]]:
        symbols = await asyncio.to_thread(screen_store.list_watchlist_symbols, watchlist_id)
        if symbols is None:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return {"watchlist_id": watchlist_id, "symbols": symbols}

    @app.post("/api/watchlists/{watchlist_id}/symbols")
    async def add_watchlist_symbols(watchlist_id: int, request: WatchlistSymbolsRequest) -> dict[str, Any]:
        try:
            watchlist = await asyncio.to_thread(screen_store.add_symbols, watchlist_id, request.symbols)
        except InvalidSymbolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if watchlist is None:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return watchlist

    @app.delete("/api/watchlists/{watchlist_id}/symbols/{symbol}")
    async def remove_watchlist_symbol(watchlist_id: int, symbol: str) -> dict[str, str | bool]:
        try:
            removed = await asyncio.to_thread(screen_store.remove_symbol, watchlist_id, symbol)
        except InvalidSymbolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if removed is None:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return {"symbol": symbol.upper(), "removed": removed}

    @app.post("/api/watchlists/{watchlist_id}/scan")
    async def scan_saved_watchlist(watchlist_id: int, request: RankingRequest | None = None) -> dict:
        ranking = request or RankingRequest()
        try:
            payload = await asyncio.to_thread(
                scan_watchlist,
                screen_store,
                scanner,
                watchlist_id,
                ranking_mode=ranking.ranking_mode,
                selected_model=ranking.selected_model,
                sort_direction=ranking.sort_direction,
            )
        except (InvalidSymbolError, InvalidRankingModeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if payload is None:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return payload

    return app


def _filters_from_request(request: SavedScreenRequest | SavedScreenUpdateRequest) -> dict[str, Any]:
    if request.filters is not None:
        return request.filters
    if request.filters_json is not None:
        return request.filters_json
    return {}


def _has_filters(request: SavedScreenUpdateRequest) -> bool:
    fields_set = getattr(request, "model_fields_set", getattr(request, "__fields_set__", set()))
    return "filters" in fields_set or "filters_json" in fields_set


def _parse_history_time_param(value: str | None) -> float | None:
    return parse_history_timestamp(value)


def _report_window(from_time: str | None, to_time: str | None) -> tuple[datetime, datetime]:
    end_at = _report_datetime(to_time) if to_time else datetime.now(timezone.utc)
    start_at = _report_datetime(from_time) if from_time else end_at - timedelta(days=1)
    if start_at > end_at:
        raise ValueError("Report start time must be before end time.")
    return start_at, end_at


def _report_datetime(value: str) -> datetime:
    timestamp = parse_history_timestamp(value)
    if timestamp is None:
        raise ValueError("Report timestamp is required.")
    return datetime.fromtimestamp(timestamp, timezone.utc)


def _report_payload(
    report_name: str,
    rows: list[dict[str, Any]],
    start_at: datetime,
    end_at: datetime,
) -> dict[str, Any]:
    return {
        "report": report_name,
        "from": start_at.isoformat(),
        "to": end_at.isoformat(),
        "count": len(rows),
        "rows": rows,
    }


app = create_app()
