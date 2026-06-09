from __future__ import annotations

import asyncio

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .cache import CachedMarketDataProvider
from .config import PACKAGE_ROOT, get_settings
from .domain import InvalidSymbolError, ScreenerError
from .providers.yahoo import YahooFinanceProvider, YahooFinanceScreener
from .scoring import score_snapshot, scoring_model_metadata
from .service import ScannerService, build_scan_response, normalize_symbols


class ScanRequest(BaseModel):
    symbols: str | list[str]


def create_app() -> FastAPI:
    settings = get_settings()
    market_data_provider = CachedMarketDataProvider(
        YahooFinanceProvider(),
        db_path=settings.cache_db_path,
        ttl_seconds=settings.cache_ttl_seconds,
    )
    scanner = ScannerService(market_data_provider)
    yahoo_screener = YahooFinanceScreener()

    app = FastAPI(
        title="Squeeze Scanner",
        description="Ranks equities across independent squeeze setup models using public market data.",
        version="0.1.0",
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_ROOT / "static"), name="static")
    templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))

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
    async def recent_scans() -> dict:
        snapshots = await asyncio.to_thread(market_data_provider.recent_snapshots)
        results = [score_snapshot(snapshot) for snapshot in snapshots]
        scan_times = await asyncio.to_thread(market_data_provider.scan_times, [result.symbol for result in results])
        return build_scan_response(results, scan_times=scan_times)

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
            return await asyncio.to_thread(scanner.scan, request.symbols)
        except InvalidSymbolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/scan/most-shorted")
    async def scan_most_shorted(count: int = Query(100, ge=1, le=250)) -> dict:
        try:
            symbols = await asyncio.to_thread(yahoo_screener.most_shorted_symbols, count)
            return await asyncio.to_thread(scanner.scan, symbols, count)
        except ScreenerError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except InvalidSymbolError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app


app = create_app()
