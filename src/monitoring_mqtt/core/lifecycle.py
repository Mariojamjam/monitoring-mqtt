"""Graceful shutdown for long-running services."""

from __future__ import annotations

import signal
import threading


def shutdown_event() -> threading.Event:
    """Return an event set on SIGTERM (``docker stop``) or SIGINT (Ctrl+C)."""
    event = threading.Event()

    def handle(signum: int, frame: object) -> None:
        event.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    return event
