from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = Path.cwd()
DEFAULT_CACHE_TTL_SECONDS = 60 * 60
DEFAULT_QUOTE_PROVIDER = "yahoo"
DEFAULT_PREMIUM_PROVIDER = "disabled"
DEFAULT_SCHEDULER_POLL_SECONDS = 30


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    reload: bool
    cache_db_path: Path
    cache_ttl_seconds: int
    quote_provider: str
    borrow_provider: str
    short_interest_provider: str
    corporate_actions_provider: str
    filings_provider: str
    event_provider: str
    scheduler_enabled: bool
    scheduler_poll_seconds: int
    default_owner_id: str | None
    alert_delivery_channels: tuple[str, ...]
    alert_webhook_url: str | None
    alert_webhook_timeout_seconds: float
    public_base_url: str | None


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
        quote_provider=_provider_setting("SQUEEZE_SCANNER_QUOTE_PROVIDER", DEFAULT_QUOTE_PROVIDER),
        borrow_provider=_provider_setting("SQUEEZE_SCANNER_BORROW_PROVIDER", DEFAULT_PREMIUM_PROVIDER),
        short_interest_provider=_provider_setting(
            "SQUEEZE_SCANNER_SHORT_INTEREST_PROVIDER",
            DEFAULT_PREMIUM_PROVIDER,
        ),
        corporate_actions_provider=_provider_setting(
            "SQUEEZE_SCANNER_CORPORATE_ACTIONS_PROVIDER",
            DEFAULT_PREMIUM_PROVIDER,
        ),
        filings_provider=_provider_setting("SQUEEZE_SCANNER_FILINGS_PROVIDER", DEFAULT_PREMIUM_PROVIDER),
        event_provider=_provider_setting("SQUEEZE_SCANNER_EVENT_PROVIDER", DEFAULT_PREMIUM_PROVIDER),
        scheduler_enabled=_env_bool("SQUEEZE_SCANNER_SCHEDULER_ENABLED", default=True),
        scheduler_poll_seconds=max(1, int(
            os.getenv("SQUEEZE_SCANNER_SCHEDULER_POLL_SECONDS", str(DEFAULT_SCHEDULER_POLL_SECONDS))
        )),
        default_owner_id=_optional_env("SQUEEZE_SCANNER_DEFAULT_OWNER_ID"),
        alert_delivery_channels=_csv_tuple(os.getenv("SQUEEZE_SCANNER_ALERT_DELIVERY_CHANNELS", "")),
        alert_webhook_url=_blank_to_none(os.getenv("SQUEEZE_SCANNER_ALERT_WEBHOOK_URL")),
        alert_webhook_timeout_seconds=float(os.getenv("SQUEEZE_SCANNER_ALERT_WEBHOOK_TIMEOUT_SECONDS", "5")),
        public_base_url=_blank_to_none(os.getenv("SQUEEZE_SCANNER_PUBLIC_BASE_URL")),
    )


def _resolve_project_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _provider_setting(name: str, default: str) -> str:
    return os.getenv(name, default).strip().lower().replace("-", "_") or default


def _env_bool(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _csv_tuple(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _blank_to_none(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None
