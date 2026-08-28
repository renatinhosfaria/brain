"""Process entrypoint for the Brain systemd service."""

from __future__ import annotations

import logging

import uvicorn

from .config import BrainSettings
from .mcp_server import BrainMCPServer
from .service import BrainService


def create_app(settings: BrainSettings | None = None):
    resolved = settings or BrainSettings.from_env()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    service = BrainService(resolved)
    startup_health = service.health()
    if startup_health.status != "ok":
        logging.getLogger("brain").error(
            "startup schema guard failed; Brain will serve only controlled errors"
        )
    return BrainMCPServer(service).app()


def main() -> None:
    settings = BrainSettings.from_env()
    uvicorn.run(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":  # pragma: no cover
    main()
