"""Durations written by people: ``500ms``, ``30s``, ``2m``, ``1h``.

Used by the experiment file (``behavior``, ``storage``), the CLI (``--for``) and the API
(``bucket``). A bare number is rejected: the unit must be explicit.
"""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)\s*$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def _seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"write a duration with a unit, e.g. '30s' or '2m' (got {value!r})")
    match = _DURATION_RE.match(value)
    if not match:
        if re.fullmatch(r"\s*\d+(\.\d+)?\s*", value):
            raise ValueError(
                f"add a unit to {value!r}, e.g. '{value.strip()}s' or '{value.strip()}m'"
            )
        raise ValueError(f"invalid duration {value!r}: use e.g. '500ms', '30s', '2m' or '1h'")
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def parse_duration(value: object) -> float:
    """``"30s"`` → 30.0. Bare numbers are rejected: the unit must be explicit."""
    seconds = _seconds(value)
    if seconds <= 0:
        raise ValueError(f"duration {value!r} must be greater than zero")
    return seconds


def parse_offset(value: object) -> float:
    """A time after the start, like a duration but ``0s`` (right away) is allowed."""
    return _seconds(value)


def format_duration(seconds: float) -> str:
    return f"{seconds:g}s"


Duration = Annotated[
    float, BeforeValidator(parse_duration), PlainSerializer(format_duration, return_type=str)
]
Offset = Annotated[
    float, BeforeValidator(parse_offset), PlainSerializer(format_duration, return_type=str)
]


def human_duration(seconds: float) -> str:
    """30.0 → ``30s``, 120.0 → ``2m``, 90.0 → ``90s``."""
    for unit, size in (("h", 3600.0), ("m", 60.0), ("s", 1.0)):
        if seconds >= size and (seconds / size).is_integer():
            return f"{int(seconds / size)}{unit}"
    return f"{seconds:g}s"
