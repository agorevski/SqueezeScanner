from __future__ import annotations

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run("squeeze_scanner.web:app", host=settings.host, port=settings.port, reload=settings.reload)


if __name__ == "__main__":
    main()

