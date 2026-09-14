"""Configures loguru once per entry point, so every log line has the same shape."""

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
    """Replace loguru's default sink with one configured from settings.

    Call once per entry point (CLI, API), never per module.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format=_FORMAT,
        backtrace=True,
        # diagnose=True renders local variables into tracebacks, which would put
        # connection strings and API keys into the logs. Keep it off. (NFR-6)
        diagnose=False,
    )
