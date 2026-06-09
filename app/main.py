from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import List, Union

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .cache import CachedMarketDataProvider, DEFAULT_CACHE_TTL_SECONDS
from .scanner import (
    InvalidSymbolError,
    ScannerService,
    YahooFinanceProvider,
    build_scan_response,
    score_snapshot,
    scoring_model_metadata,
)

BASE_DIR = Path(__file__).resolve().parent.parent
CACHE_DB_PATH = Path(os.getenv("SQUEEZE_SCANNER_CACHE_DB", BASE_DIR / "data" / "market_data_cache.sqlite3"))
CACHE_TTL_SECONDS = int(os.getenv("SQUEEZE_SCANNER_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS)))

app = FastAPI(
    title="Short Squeeze Scanner",
    description="Ranks equities by short squeeze setup indicators using public market data.",
    version="0.1.0",
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

market_data_provider = CachedMarketDataProvider(
    YahooFinanceProvider(),
    db_path=CACHE_DB_PATH,
    ttl_seconds=CACHE_TTL_SECONDS,
)
scanner = ScannerService(market_data_provider)


@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith(("/static/", "/api/")):
        response.headers["Cache-Control"] = "no-store"
    return response


class ScanRequest(BaseModel):
    symbols: Union[str, List[str]]


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


@app.post("/api/scan")
async def scan(request: ScanRequest) -> dict:
    try:
        return await asyncio.to_thread(scanner.scan, request.symbols)
    except InvalidSymbolError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
