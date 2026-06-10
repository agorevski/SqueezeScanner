from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .analytics import AnalyticsStore
from .alert_delivery import build_alert_delivery_service
from .automation import AlertNotFoundError, AutomationError, AutomationScheduler, AutomationService, ScheduleNotFoundError
from .cache import CachedMarketDataProvider
from .config import PACKAGE_ROOT, get_settings
from .domain import InvalidRankingModeError, InvalidSymbolError, ScreenerError
from .history import ScoreHistoryStore, parse_history_timestamp
from .providers.premium import build_market_data_provider, provider_status_payload
from .providers.yahoo import YahooFinanceScreener
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
    owner_id: str | None = None


class SavedScreenUpdateRequest(BaseModel):
    name: str | None = None
    filters: dict[str, Any] | None = None
    filters_json: dict[str, Any] | None = None
    owner_id: str | None = None


class WatchlistRequest(BaseModel):
    name: str
    symbols: str | list[str] | None = None
    owner_id: str | None = None


class WatchlistUpdateRequest(BaseModel):
    name: str | None = None
    owner_id: str | None = None


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
    owner_id: str | None = None


class ScheduledScanUpdateRequest(BaseModel):
    name: str | None = None
    target_type: str | None = None
    target: dict[str, Any] | None = None
    interval_seconds: int | None = None
    enabled: bool | None = None
    next_run_at: str | None = None
    owner_id: str | None = None


class AlertRequest(BaseModel):
    name: str
    rule: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    delivery_channels: list[str] | None = None
    owner_id: str | None = None


class AlertUpdateRequest(BaseModel):
    name: str | None = None
    rule: dict[str, Any] | None = None
    enabled: bool | None = None
    delivery_channels: list[str] | None = None
    owner_id: str | None = None


def create_app() -> FastAPI:
    settings = get_settings()
    source_provider, premium_providers = build_market_data_provider(settings)
    market_data_provider = CachedMarketDataProvider(
        source_provider,
        db_path=settings.cache_db_path,
        ttl_seconds=settings.cache_ttl_seconds,
        provider_name=source_provider.provider_name,
    )
    history_store = ScoreHistoryStore(settings.cache_db_path)
    analytics_store = AnalyticsStore(settings.cache_db_path)
    scanner = ScannerService(market_data_provider, history_store=history_store)
    screen_store = ScreenStore(settings.cache_db_path)
    yahoo_screener = YahooFinanceScreener()
    alert_delivery = build_alert_delivery_service(
        default_channels=settings.alert_delivery_channels,
        webhook_url=settings.alert_webhook_url,
        webhook_timeout_seconds=settings.alert_webhook_timeout_seconds,
        public_base_url=settings.public_base_url or f"http://{settings.host}:{settings.port}",
    )
    automation = AutomationService(
        settings.cache_db_path,
        scanner=scanner,
        yahoo_screener=yahoo_screener,
        alert_delivery=alert_delivery,
    )
    automation_scheduler = AutomationScheduler(automation, poll_interval_seconds=settings.scheduler_poll_seconds)

    app = FastAPI(
        title="Squeeze Scanner",
        description="Ranks equities across independent squeeze setup models using public market data.",
        version="0.1.0",
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))

    @app.on_event("startup")
    async def start_automation_scheduler() -> None:
        if settings.scheduler_enabled:
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
    async def health() -> dict[str, Any]:
        return await asyncio.to_thread(_build_status_payload, settings, market_data_provider, automation, automation_scheduler)

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return await asyncio.to_thread(_build_status_payload, settings, market_data_provider, automation, automation_scheduler)

    @app.get("/api/scheduler/status")
    async def scheduler_status() -> dict[str, Any]:
        scheduler = automation_scheduler.status()
        scheduler["enabled"] = settings.scheduler_enabled
        scheduler["service"] = await asyncio.to_thread(automation.status)
        return scheduler

    @app.get("/api/providers")
    async def providers() -> dict[str, object]:
        return provider_status_payload(settings, premium_providers)

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
    async def list_reports() -> dict[str, list[dict[str, Any]]]:
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
                    "description": "Outcome statistics grouped by model, horizon, score bucket, and optional source-quality slice.",
                },
                {
                    "name": "Model-version comparison",
                    "path": "/api/reports/model-version-comparison",
                    "description": "Pairs outcomes for two scoring model versions on the same historical scans.",
                },
                {
                    "name": "Gamma threshold review",
                    "path": "/api/reports/gamma-threshold-review",
                    "description": "Outcome buckets for persisted gamma metrics such as GEX %, flip distance, walls, and OI change.",
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
        format: str = "json",
    ) -> Any:
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
        return _report_response(_report_payload("top_new_high_setups", rows, start_at, end_at), format, analytics_store)

    @app.get("/api/reports/biggest-1h-increases")
    async def report_biggest_1h_increases(
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        model: str | None = None,
        min_delta: float = 0.0,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        format: str = "json",
    ) -> Any:
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
        return _report_response(_report_payload("biggest_1h_increases", rows, start_at, end_at), format, analytics_store)

    @app.get("/api/reports/biggest-24h-increases")
    async def report_biggest_24h_increases(
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        model: str | None = None,
        min_delta: float = 0.0,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        format: str = "json",
    ) -> Any:
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
        return _report_response(_report_payload("biggest_24h_increases", rows, start_at, end_at), format, analytics_store)

    @app.get("/api/reports/repeated-high-setups")
    async def report_repeated_high_setups(
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        model: str | None = None,
        min_score: float = Query(70.0, ge=0, le=100),
        min_count: int = Query(2, ge=2),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        format: str = "json",
    ) -> Any:
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
        return _report_response(_report_payload("repeated_high_setups", rows, start_at, end_at), format, analytics_store)

    @app.get("/api/reports/deterioration")
    async def report_deterioration(
        from_time: str | None = Query(None, alias="from"),
        to_time: str | None = Query(None, alias="to"),
        window: str = "24h",
        model: str | None = None,
        min_drop: float = 0.0,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        format: str = "json",
    ) -> Any:
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
        return _report_response(_report_payload("deterioration", rows, start_at, end_at), format, analytics_store)

    @app.get("/api/reports/calibration")
    async def report_calibration(
        model: str = "hybrid",
        horizon: str = "1d",
        bucket_size: float = Query(10.0, gt=0, le=100),
        scoring_model_version: str | None = None,
        slice_by_source_quality: bool = False,
        source_quality_slice: str | None = None,
        format: str = "json",
    ) -> Any:
        try:
            rows = await asyncio.to_thread(
                analytics_store.calibration_report,
                model=model,
                horizon=horizon,
                bucket_size=bucket_size,
                scoring_model_version=scoring_model_version,
                slice_by_source_quality=slice_by_source_quality,
                source_quality_slice=source_quality_slice,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report_response(
            {
                "report": "calibration",
                "model": model,
                "horizon": horizon,
                "count": len(rows),
                "rows": rows,
            },
            format,
            analytics_store,
        )

    @app.get("/api/reports/model-version-comparison")
    async def report_model_version_comparison(
        model: str = "hybrid",
        horizon: str = "1d",
        base_version: str | None = None,
        compare_version: str | None = None,
        bucket_size: float = Query(10.0, gt=0, le=100),
        deterioration_threshold_pct: float = Query(0.0, ge=0),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        format: str = "json",
    ) -> Any:
        try:
            rows = await asyncio.to_thread(
                analytics_store.report_model_version_comparison,
                model=model,
                horizon=horizon,
                base_version=base_version,
                compare_version=compare_version,
                bucket_size=bucket_size,
                deterioration_threshold_pct=deterioration_threshold_pct,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report_response(
            {
                "report": "model_version_comparison",
                "model": model,
                "horizon": horizon,
                "base_version": base_version,
                "compare_version": compare_version,
                "count": len(rows),
                "rows": rows,
            },
            format,
            analytics_store,
        )

    @app.get("/api/reports/gamma-threshold-review")
    async def report_gamma_threshold_review(
        model: str = "gamma_candidate",
        horizon: str = "1d",
        metrics: str | None = None,
        bucket_size: float = Query(5.0, gt=0, le=1000),
        scoring_model_version: str | None = None,
        include_missing: bool = True,
        limit: int = Query(500, ge=1, le=2000),
        offset: int = Query(0, ge=0),
        format: str = "json",
    ) -> Any:
        try:
            rows = await asyncio.to_thread(
                analytics_store.report_gamma_threshold_review,
                model=model,
                horizon=horizon,
                metrics=metrics,
                bucket_size=bucket_size,
                scoring_model_version=scoring_model_version,
                include_missing=include_missing,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return _report_response(
            {
                "report": "gamma_threshold_review",
                "model": model,
                "horizon": horizon,
                "count": len(rows),
                "rows": rows,
            },
            format,
            analytics_store,
        )

    @app.get("/api/scheduled-scans")
    async def list_scheduled_scans(owner_id: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(automation.list_schedules, _normalize_owner_id(owner_id))

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
                _owner_for_create(request.owner_id, settings.default_owner_id),
            )
        except AutomationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/scheduled-scans/{schedule_id}")
    async def get_scheduled_scan(schedule_id: int, owner_id: str | None = None) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(automation.get_schedule, schedule_id, _normalize_owner_id(owner_id))
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/api/scheduled-scans/{schedule_id}")
    async def update_scheduled_scan(schedule_id: int, request: ScheduledScanUpdateRequest) -> dict[str, Any]:
        try:
            kwargs: dict[str, Any] = {
                "name": request.name,
                "target_type": request.target_type,
                "target": request.target,
                "interval_seconds": request.interval_seconds,
                "enabled": request.enabled,
                "next_run_at": request.next_run_at,
            }
            if _has_owner_id(request):
                kwargs["owner_id"] = _normalize_owner_id(request.owner_id)
            return await asyncio.to_thread(
                automation.update_schedule,
                schedule_id,
                **kwargs,
            )
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AutomationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/scheduled-scans/{schedule_id}")
    async def delete_scheduled_scan(schedule_id: int, owner_id: str | None = None) -> dict[str, int | bool]:
        deleted = await asyncio.to_thread(automation.delete_schedule, schedule_id, _normalize_owner_id(owner_id))
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
    async def list_alerts(enabled_only: bool = False, owner_id: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(automation.list_alerts, enabled_only, _normalize_owner_id(owner_id))

    @app.post("/api/alerts")
    async def create_alert(request: AlertRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                automation.create_alert,
                request.name,
                request.rule,
                request.enabled,
                request.delivery_channels,
                _owner_for_create(request.owner_id, settings.default_owner_id),
            )
        except AutomationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/api/alerts/{alert_id}")
    async def update_alert(alert_id: int, request: AlertUpdateRequest) -> dict[str, Any]:
        try:
            kwargs: dict[str, Any] = {
                "name": request.name,
                "rule": request.rule,
                "enabled": request.enabled,
                "delivery_channels": request.delivery_channels,
            }
            if _has_owner_id(request):
                kwargs["owner_id"] = _normalize_owner_id(request.owner_id)
            return await asyncio.to_thread(
                automation.update_alert,
                alert_id,
                **kwargs,
            )
        except AlertNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AutomationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/alerts/{alert_id}")
    async def delete_alert(alert_id: int, owner_id: str | None = None) -> dict[str, int | bool]:
        deleted = await asyncio.to_thread(automation.delete_alert, alert_id, _normalize_owner_id(owner_id))
        return {"id": alert_id, "deleted": deleted}

    @app.get("/api/alert-events")
    async def list_alert_events(
        alert_id: int | None = None,
        symbol: str | None = None,
        active_only: bool = False,
        limit: int = Query(100, ge=1, le=500),
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            automation.list_alert_events,
            alert_id,
            symbol,
            active_only,
            limit,
            _normalize_owner_id(owner_id),
        )

    @app.post("/api/alert-events/{event_id}/ack")
    async def acknowledge_alert_event(event_id: int) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(automation.acknowledge_alert_event, event_id)
        except AlertNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/alert-delivery-attempts")
    async def list_alert_delivery_attempts(
        alert_event_id: int | None = None,
        alert_id: int | None = None,
        status: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            automation.list_alert_delivery_attempts,
            alert_event_id,
            alert_id,
            status,
            limit,
            _normalize_owner_id(owner_id),
        )

    @app.post("/api/alert-delivery-attempts/{attempt_id}/retry")
    async def retry_alert_delivery_attempt(attempt_id: int, owner_id: str | None = None) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                automation.retry_alert_delivery_attempt,
                attempt_id,
                _normalize_owner_id(owner_id),
            )
        except AlertNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/screens")
    async def list_screens(owner_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        return {"screens": await asyncio.to_thread(screen_store.list_screens, _normalize_owner_id(owner_id))}

    @app.post("/api/screens")
    async def create_screen(request: SavedScreenRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                screen_store.create_screen,
                request.name,
                _filters_from_request(request),
                _owner_for_create(request.owner_id, settings.default_owner_id),
            )
        except ScreenStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/screens/{screen_id}")
    async def update_screen(screen_id: int, request: SavedScreenUpdateRequest) -> dict[str, Any]:
        try:
            kwargs: dict[str, Any] = {"name": request.name}
            if _has_filters(request):
                kwargs["filters"] = _filters_from_request(request)
            if _has_owner_id(request):
                kwargs["owner_id"] = _normalize_owner_id(request.owner_id)
            screen = await asyncio.to_thread(screen_store.update_screen, screen_id, **kwargs)
        except ScreenStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if screen is None:
            raise HTTPException(status_code=404, detail="Saved screen not found.")
        return screen

    @app.delete("/api/screens/{screen_id}")
    async def delete_screen(screen_id: int, owner_id: str | None = None) -> dict[str, bool]:
        deleted = await asyncio.to_thread(screen_store.delete_screen, screen_id, _normalize_owner_id(owner_id))
        if not deleted:
            raise HTTPException(status_code=404, detail="Saved screen not found.")
        return {"deleted": True}

    @app.get("/api/watchlists")
    async def list_watchlists(owner_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        return {"watchlists": await asyncio.to_thread(screen_store.list_watchlists, _normalize_owner_id(owner_id))}

    @app.post("/api/watchlists")
    async def create_watchlist(request: WatchlistRequest) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(
                screen_store.create_watchlist,
                request.name,
                request.symbols,
                _owner_for_create(request.owner_id, settings.default_owner_id),
            )
        except (InvalidSymbolError, ScreenStoreError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.put("/api/watchlists/{watchlist_id}")
    async def update_watchlist(watchlist_id: int, request: WatchlistUpdateRequest) -> dict[str, Any]:
        try:
            kwargs: dict[str, Any] = {"name": request.name}
            if _has_owner_id(request):
                kwargs["owner_id"] = _normalize_owner_id(request.owner_id)
            watchlist = await asyncio.to_thread(screen_store.update_watchlist, watchlist_id, **kwargs)
        except ScreenStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if watchlist is None:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return watchlist

    @app.delete("/api/watchlists/{watchlist_id}")
    async def delete_watchlist(watchlist_id: int, owner_id: str | None = None) -> dict[str, bool]:
        deleted = await asyncio.to_thread(screen_store.delete_watchlist, watchlist_id, _normalize_owner_id(owner_id))
        if not deleted:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return {"deleted": True}

    @app.get("/api/watchlists/{watchlist_id}/symbols")
    async def list_watchlist_symbols(
        watchlist_id: int,
        owner_id: str | None = None,
    ) -> dict[str, int | list[str]]:
        symbols = await asyncio.to_thread(
            screen_store.list_watchlist_symbols,
            watchlist_id,
            _normalize_owner_id(owner_id),
        )
        if symbols is None:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return {"watchlist_id": watchlist_id, "symbols": symbols}

    @app.post("/api/watchlists/{watchlist_id}/symbols")
    async def add_watchlist_symbols(
        watchlist_id: int,
        request: WatchlistSymbolsRequest,
        owner_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            watchlist = await asyncio.to_thread(
                screen_store.add_symbols,
                watchlist_id,
                request.symbols,
                _normalize_owner_id(owner_id),
            )
        except InvalidSymbolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if watchlist is None:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return watchlist

    @app.delete("/api/watchlists/{watchlist_id}/symbols/{symbol}")
    async def remove_watchlist_symbol(
        watchlist_id: int,
        symbol: str,
        owner_id: str | None = None,
    ) -> dict[str, str | bool]:
        try:
            removed = await asyncio.to_thread(
                screen_store.remove_symbol,
                watchlist_id,
                symbol,
                _normalize_owner_id(owner_id),
            )
        except InvalidSymbolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if removed is None:
            raise HTTPException(status_code=404, detail="Watchlist not found.")
        return {"symbol": symbol.upper(), "removed": removed}

    @app.post("/api/watchlists/{watchlist_id}/scan")
    async def scan_saved_watchlist(
        watchlist_id: int,
        request: RankingRequest | None = None,
        owner_id: str | None = None,
    ) -> dict:
        ranking = request or RankingRequest()
        try:
            payload = await asyncio.to_thread(
                scan_watchlist,
                screen_store,
                scanner,
                watchlist_id,
                owner_id=_normalize_owner_id(owner_id),
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


def _build_status_payload(
    settings: Any,
    market_data_provider: CachedMarketDataProvider,
    automation: AutomationService,
    automation_scheduler: AutomationScheduler,
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    cache_status = market_data_provider.status()
    automation_status = automation.status()
    scheduler_status = automation_scheduler.status()
    scheduler_status["enabled"] = settings.scheduler_enabled
    component_statuses = [cache_status.get("status"), automation_status.get("status")]
    overall_status = "ok" if all(status == "ok" for status in component_statuses) else "degraded"
    return {
        "status": overall_status,
        "generated_at": generated_at,
        "app": {
            "name": "squeeze-scanner",
            "version": "0.1.0",
            "auth_required": False,
            "owner_scoping_available": True,
            "default_owner_configured": settings.default_owner_id is not None,
        },
        "storage": cache_status.get("database", {"backend": "sqlite", "accessible": False}),
        "cache": cache_status.get("cache", {}),
        "provider": cache_status.get("provider", {}),
        "scheduler": scheduler_status,
        "automation": automation_status,
    }


def _filters_from_request(request: SavedScreenRequest | SavedScreenUpdateRequest) -> dict[str, Any]:
    if request.filters is not None:
        return request.filters
    if request.filters_json is not None:
        return request.filters_json
    return {}


def _has_filters(request: SavedScreenUpdateRequest) -> bool:
    fields_set = _request_fields_set(request)
    return "filters" in fields_set or "filters_json" in fields_set


def _has_owner_id(request: BaseModel) -> bool:
    return "owner_id" in _request_fields_set(request)


def _request_fields_set(request: BaseModel) -> set[str]:
    return set(getattr(request, "model_fields_set", getattr(request, "__fields_set__", set())))


def _owner_for_create(owner_id: str | None, default_owner_id: str | None) -> str | None:
    return _normalize_owner_id(owner_id) or _normalize_owner_id(default_owner_id)


def _normalize_owner_id(owner_id: str | None) -> str | None:
    if owner_id is None:
        return None
    normalized = str(owner_id).strip()
    return normalized or None


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


def _report_response(payload: dict[str, Any], report_format: str, analytics_store: AnalyticsStore) -> Any:
    normalized_format = str(report_format or "json").strip().lower()
    if normalized_format == "json":
        return payload
    if normalized_format != "csv":
        raise HTTPException(status_code=400, detail="Report format must be 'json' or 'csv'.")
    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    report_name = str(payload.get("report") or "report").replace("/", "_")
    return Response(
        content=analytics_store.rows_to_csv(rows),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{report_name}.csv"'},
    )


app = create_app()
