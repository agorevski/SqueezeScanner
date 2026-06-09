# Architecture

```mermaid
flowchart LR
    Phone["Phone / browser"] -->|HTTP configured port| Network["Configured host/IP<br/>from .env"]
    Network --> Uvicorn["Uvicorn<br/>FastAPI app"]

    subgraph WebApp["SqueezeScanner application"]
        UI["HTML/CSS/JS<br/>src/squeeze_scanner/templates<br/>src/squeeze_scanner/static"]
        API["FastAPI routes<br/>/api/model<br/>/api/scan<br/>/api/scan/most-shorted<br/>/api/scans/recent<br/>DELETE /api/scans/{symbol}<br/>/api/health"]
        Scanner["ScannerService<br/>score recomputation"]
        CacheProvider["CachedMarketDataProvider<br/>1 hour TTL"]
        YahooProvider["YahooFinanceProvider<br/>yfinance adapter"]
    end

    Uvicorn --> UI
    Uvicorn --> API
    API --> Scanner
    Scanner --> CacheProvider
    API -->|"recent cached snapshots"| CacheProvider
    CacheProvider --> SQLite[("SQLite<br/>data/market_data_cache.sqlite3<br/>raw snapshots only")]
    CacheProvider -->|"cache miss or >1 hr stale"| YahooProvider
    YahooProvider --> Yahoo["Yahoo Finance<br/>market data"]
```

## Request flow

```mermaid
sequenceDiagram
    participant Browser
    participant API as FastAPI
    participant Scanner as ScannerService
    participant Cache as SQLite raw-data cache
    participant Yahoo as Yahoo Finance / yfinance

    Browser->>API: GET /api/scans/recent
    API->>Cache: Read snapshots fetched within 1 hour
    Cache-->>API: Raw TickerSnapshot rows
    API->>Scanner: Recompute scores from raw snapshots
    API-->>Browser: Recent screened cards

    Browser->>API: POST /api/scan {symbols}
    API->>Scanner: Scan requested symbols
    Scanner->>Cache: Fetch raw snapshot for each symbol
    alt cached raw data is fresh
        Cache-->>Scanner: Cached TickerSnapshot
    else missing or older than 1 hour
        Cache->>Yahoo: Fetch quote, short interest, volume, history
        Yahoo-->>Cache: Raw market data
        Cache->>Cache: Store raw TickerSnapshot only
        Cache-->>Scanner: Fresh TickerSnapshot
    end
    Scanner->>Scanner: Recompute squeeze-v2 score
    Scanner-->>API: Ranked scan results
    API-->>Browser: Append/update screened cards

    Browser->>API: POST /api/scan/most-shorted
    API->>Yahoo: Load Yahoo predefined most_shorted_stocks screener
    Yahoo-->>API: Candidate symbols
    API->>Scanner: Analyze screener symbols through normal scanner/cache path
    Scanner-->>API: Ranked scan results
    API-->>Browser: Merge analyzed universe into screened cards

    Browser->>API: DELETE /api/scans/{symbol}
    API->>Cache: Delete symbol row
    Cache-->>API: Deleted flag
    API-->>Browser: Remove card/row from page
```

## Key design points

- `uv` manages dependencies and the `squeeze-scanner` console script.
- Network binding, port, reload mode, cache path, and cache TTL are configured through `.env`; `.env.example` documents the supported variables.
- Source code, templates, and static assets live under `src/squeeze_scanner`; legacy `app.*` modules are compatibility shims.
- Uvicorn auto-reload can watch `src/squeeze_scanner` during development.
- Browser, static, and API responses use `Cache-Control: no-store` to prevent stale UI assets.
- The frontend gets signal labels, weights, descriptions, calculations, tooltips, and legend data from the Python scoring model via `/api/model` and each response `model` block.
- SQLite stores raw financial-service snapshots plus `fetched_at` and `scanned_at` timestamps. Scores, risk labels, components, rationale, and rendered UI state are never cached.
- `/api/scans/recent` returns all tickers screened within the current TTL and recomputes their scores with the current model.
- `DELETE /api/scans/{symbol}` removes one ticker from the SQLite cache; the frontend also removes it from the visible screened list.
- `/api/scan` uses cached raw data for fresh symbols and fetches Yahoo Finance only for new or stale symbols.
- `/api/scan/most-shorted` pulls Yahoo's predefined most-shorted screener symbols, then analyzes them through the same scanner/cache path.
