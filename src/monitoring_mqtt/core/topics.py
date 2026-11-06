"""MQTT topic contract.

Telemetry topics follow ``monitoring/v1/{domain}/{metric}/{device_id}``. Every service
builds and parses topics through this module so the hierarchy is defined in one place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from paho.mqtt.client import topic_matches_sub

TELEMETRY_ROOT = "monitoring"
TELEMETRY_VERSION = "v1"
TELEMETRY_PREFIX = f"{TELEMETRY_ROOT}/{TELEMETRY_VERSION}"
ALL_TELEMETRY_FILTER = f"{TELEMETRY_PREFIX}/#"

# Lowercase identifiers; no MQTT separators or wildcards inside a segment.
SEGMENT_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"
_SEGMENT_RE = re.compile(SEGMENT_PATTERN)


class InvalidTopicError(ValueError):
    """Raised when a topic or topic filter does not follow the contract."""


@dataclass(frozen=True)
class TelemetryTopic:
    domain: str
    metric: str
    device_id: str

    def __str__(self) -> str:
        return telemetry_topic(self.domain, self.metric, self.device_id)


def validate_segment(value: str, name: str) -> str:
    if not _SEGMENT_RE.fullmatch(value):
        raise InvalidTopicError(
            f"invalid {name} {value!r}: use lowercase letters, digits, '_' or '-'"
        )
    return value


def telemetry_topic(domain: str, metric: str, device_id: str) -> str:
    """Build the telemetry topic for one device."""
    validate_segment(domain, "domain")
    validate_segment(metric, "metric")
    validate_segment(device_id, "device_id")
    return f"{TELEMETRY_PREFIX}/{domain}/{metric}/{device_id}"


def parse_telemetry_topic(topic: str) -> TelemetryTopic:
    """Parse a concrete telemetry topic, raising ``InvalidTopicError`` if malformed."""
    parts = topic.split("/")
    if len(parts) != 5 or parts[0] != TELEMETRY_ROOT or parts[1] != TELEMETRY_VERSION:
        raise InvalidTopicError(
            f"not a telemetry topic: {topic!r} (expected {TELEMETRY_PREFIX}/domain/metric/device)"
        )
    domain, metric, device_id = parts[2:]
    validate_segment(domain, "domain")
    validate_segment(metric, "metric")
    validate_segment(device_id, "device_id")
    return TelemetryTopic(domain, metric, device_id)


def domain_filter(domain: str) -> str:
    """Filter matching every telemetry topic of one domain."""
    return f"{TELEMETRY_PREFIX}/{validate_segment(domain, 'domain')}/#"


def metric_filter(metric: str) -> str:
    """Filter matching one metric across all domains and devices."""
    return f"{TELEMETRY_PREFIX}/+/{validate_segment(metric, 'metric')}/+"


def validate_filter(topic_filter: str) -> str:
    """Validate MQTT subscription filter syntax (wildcards must fill whole levels)."""
    if not topic_filter:
        raise InvalidTopicError("topic filter must not be empty")
    levels = topic_filter.split("/")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            raise InvalidTopicError(f"'#' must be the last whole level in {topic_filter!r}")
        if "+" in level and level != "+":
            raise InvalidTopicError(f"'+' must fill a whole level in {topic_filter!r}")
    return topic_filter


def matches(topic_filter: str, topic: str) -> bool:
    """Return True if ``topic`` matches the MQTT subscription ``topic_filter``."""
    return topic_matches_sub(validate_filter(topic_filter), topic)
