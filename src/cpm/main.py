"""Entry point: `python -m cpm.main` serves the app on 127.0.0.1 only."""

from __future__ import annotations

import uvicorn

from cpm.api.app import create_app
from cpm.config import load_settings


def main() -> None:
    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
