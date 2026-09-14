"""Configures loguru once per entry point."""

import sys
from typing import Final

from loguru import logger

from pipeline.config import settings

_FORMAT: Final = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


def configure_logging() -> None:
    """Call once per entry point (CLI, API) — never per module."""
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=_FORMAT,
        backtrace=True,
        diagnose=False,  # would render locals into tracebacks, leaking secrets
    )
