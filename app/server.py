from __future__ import annotations

import os
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 7890
BASE_DIR = Path(__file__).resolve().parent.parent


def main() -> None:
    load_dotenv(BASE_DIR / ".env")

    host = os.getenv("SQUEEZE_SCANNER_HOST", DEFAULT_HOST)
    port = int(os.getenv("SQUEEZE_SCANNER_PORT", str(DEFAULT_PORT)))
    reload = os.getenv("SQUEEZE_SCANNER_RELOAD", "").lower() in {"1", "true", "yes"}

    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()
