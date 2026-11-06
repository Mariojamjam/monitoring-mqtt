"""Consistent logging for services: UTC timestamps and ``key=value`` fields."""

from __future__ import annotations

import logging
import sys
import time

FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)-7s %(name)s %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger to write to stdout (collected by Docker)."""
    formatter = logging.Formatter(FORMAT, DATE_FORMAT)
    formatter.converter = time.gmtime
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def kv(**fields: object) -> str:
    """Render fields as ``key=value`` pairs in a stable order."""
    return " ".join(f"{key}={value}" for key, value in fields.items())
