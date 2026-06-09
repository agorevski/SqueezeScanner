from __future__ import annotations

import os

import uvicorn

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 7890


def main() -> None:
    host = os.getenv("SQUEEZE_SCANNER_HOST", DEFAULT_HOST)
    port = int(os.getenv("SQUEEZE_SCANNER_PORT", str(DEFAULT_PORT)))
    reload = os.getenv("SQUEEZE_SCANNER_RELOAD", "").lower() in {"1", "true", "yes"}

    uvicorn.run("app.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    main()

