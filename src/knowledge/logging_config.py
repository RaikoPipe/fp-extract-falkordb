"""Logging configuration using loguru."""

from __future__ import annotations

import sys

from loguru import logger

_INFO_FORMAT = "<level>{level.icon}</level> {message}"
_DEBUG_FORMAT = (
    "<dim>{time:HH:mm:ss}</dim> | <level>{level:<7}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | {message}"
)


def setup_logging(*, debug: bool = False) -> None:
    """Configure loguru for the application.

    Normal mode (default): INFO-level, minimal format.
    Debug mode: DEBUG-level, detailed format with timestamps and source location.
    """
    logger.remove()
    if debug:
        logger.add(sys.stderr, level="DEBUG", format=_DEBUG_FORMAT, colorize=True)
    else:
        logger.add(sys.stderr, level="INFO", format=_INFO_FORMAT, colorize=True)
