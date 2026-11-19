"""Configuração de logging da aplicação (API, workers e scripts)."""

from __future__ import annotations

import logging

from config import settings

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging() -> None:
    level_name = (settings.log_level or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(level=level, format=_FORMAT)
    # Bibliotecas de HTTP são barulhentas em DEBUG/INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(max(level, logging.INFO))
