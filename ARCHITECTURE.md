# Architecture

```mermaid
flowchart LR
    Phone["Phone / browser"] -->|HTTP configured port| Network["Configured host/IP<br/>from .env"]
    Network --> Uvicorn["Uvicorn<br/>FastAPI app"]

    subgraph WebApp["SqueezeScanner application"]
        UI["HTML/CSS/JS<br/>templates + static"]
        API["FastAPI routes<br/>/api/model<br/>/api/scan<br/>/api/scans/recent<br/>/api/health"]
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
```

## Key design points

- `uv` manages dependencies and the `squeeze-scanner` console script.
- Network binding, port, reload mode, cache path, and cache TTL are configured through `.env`; `.env.example` documents the supported variables.
- Uvicorn auto-reload can watch `app/`, `templates/`, and `static/` during development.
- Browser, static, and API responses use `Cache-Control: no-store` to prevent stale UI assets.
- The frontend gets signal labels, weights, descriptions, calculations, tooltips, and legend data from the Python scoring model via `/api/model` and each response `model` block.
- SQLite stores raw financial-service snapshots plus `fetched_at` and `scanned_at` timestamps. Scores, risk labels, components, rationale, and rendered UI state are never cached.
- `/api/scans/recent` returns all tickers screened within the current TTL and recomputes their scores with the current model.
- `/api/scan` uses cached raw data for fresh symbols and fetches Yahoo Finance only for new or stale symbols.
