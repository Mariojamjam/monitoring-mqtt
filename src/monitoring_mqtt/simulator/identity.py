"""Worker and device identities.

``device_id`` doubles as the MQTT ``client_id`` and as the last topic segment:
``{domain}-{worker_id}-{index:03d}``, for example ``aquaculture-3f9a1c-001``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

from monitoring_mqtt.core.identity import host_short_id, sanitize_segment
from monitoring_mqtt.core.topics import validate_segment


def resolve_worker_id(env: Mapping[str, str] | None = None, hostname: str | None = None) -> str:
    """``WORKER_ID`` when set (persistent identities), else derived from the hostname."""
    env = os.environ if env is None else env
    explicit = env.get("WORKER_ID", "").strip()
    return sanitize_segment(explicit) if explicit else host_short_id(hostname)


def device_id(domain: str, worker_id: str, index: int) -> str:
    if index < 1:
        raise ValueError("device index starts at 1")
    return validate_segment(f"{domain}-{worker_id}-{index:03d}", "device_id")


_INDEX_RE = re.compile(r"^(?P<body>.+)-(?P<index>\d{3,})$")


def worker_of_device(device_id: str, domains: Iterable[str]) -> str:
    """``{domain}-{worker_id}-{index}`` → ``worker_id`` (domains come from the catalog).

    Raises ``ValueError`` when the id does not follow the device id format.
    """
    match = _INDEX_RE.match(device_id)
    if match:
        body = match.group("body")
        for domain in sorted(domains, key=len, reverse=True):
            prefix = f"{domain}-"
            if body.startswith(prefix) and len(body) > len(prefix):
                return body[len(prefix) :]
    raise ValueError(
        f"{device_id!r} is not a device id: expected {{domain}}-{{worker_id}}-{{index}}, "
        "e.g. poultry-3f9a1c-002 (see 'monitoring-mqtt stack workers' or the terminal UI)"
    )
