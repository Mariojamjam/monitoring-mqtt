"""Instance identity helpers shared by publishers and subscribers."""

from __future__ import annotations

import re
import socket

HOST_ID_LENGTH = 6


def sanitize_segment(value: str) -> str:
    """Lowercase ``value`` and replace characters not allowed in topic segments with '-'."""
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_")
    if not cleaned:
        raise ValueError(f"cannot derive an identifier from {value!r}")
    return cleaned


def host_short_id(hostname: str | None = None) -> str:
    """Short id derived from the hostname.

    Docker sets a container's hostname to its container ID, so this is unique per
    replica without a fixed ``container_name``.
    """
    return sanitize_segment(hostname or socket.gethostname())[:HOST_ID_LENGTH]


def worker_of(device_id: str, domain: str) -> str:
    """``{domain}-{worker_id}-{index}`` → ``worker_id`` (the domain is known)."""
    prefix = f"{domain}-"
    body = device_id[len(prefix) :] if device_id.startswith(prefix) else device_id
    return body.rsplit("-", 1)[0] if "-" in body else body
