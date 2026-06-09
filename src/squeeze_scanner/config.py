from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = Path.cwd()
DEFAULT_CACHE_TTL_SECONDS = 60 * 60


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    reload: bool
    cache_db_path: Path
    cache_ttl_seconds: int


def load_environment() -> None:
    load_dotenv(PROJECT_ROOT / ".env")


@lru_cache
def get_settings() -> Settings:
    load_environment()
    return Settings(
        host=os.getenv("SQUEEZE_SCANNER_HOST", "0.0.0.0"),
        port=int(os.getenv("SQUEEZE_SCANNER_PORT", "7890")),
        reload=os.getenv("SQUEEZE_SCANNER_RELOAD", "").lower() in {"1", "true", "yes"},
        cache_db_path=_resolve_project_path(
            os.getenv("SQUEEZE_SCANNER_CACHE_DB", "data/market_data_cache.sqlite3")
        ),
        cache_ttl_seconds=int(os.getenv("SQUEEZE_SCANNER_CACHE_TTL_SECONDS", str(DEFAULT_CACHE_TTL_SECONDS))),
    )


def _resolve_project_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path

