"""Observability plane: subscribers announce their status through the broker.

MQTT does not expose who subscribes to what, so every monitor publishes its own status::

    monitoring/status/v1/monitors/{client_id}

- ``online`` (retained) on every connection and then periodically, with its counters;
- ``offline`` (retained) as the connection's Last Will, published by the broker when the
  monitor disappears without a clean disconnect;
- ``stopped`` (not retained) on a clean shutdown, followed by an empty retained payload
  that removes the status.

This plane is for operators and observer tools such as the TUI. Publishers never read it:
devices publish the same way whether or not anyone consumes. It lives outside
``monitoring/v1/#``, so telemetry subscribers never receive it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from monitoring_mqtt.core.topics import SEGMENT_PATTERN, TELEMETRY_ROOT, validate_segment

STATUS_PREFIX = f"{TELEMETRY_ROOT}/status/v1"
MONITOR_STATUS_FILTER = f"{STATUS_PREFIX}/monitors/+"
STATUS_QOS = 1
STATUS_INTERVAL_SECONDS = 5.0  # online status refresh period (part of the contract)
NODE_NAME_MAX_LENGTH = 64
RECENT_MESSAGES = 10  # latest received messages carried by each status

Segment = Annotated[str, Field(pattern=SEGMENT_PATTERN)]
NonEmpty = Annotated[str, Field(min_length=1)]
Count = Annotated[int, Field(ge=0)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
NodeName = Annotated[str, Field(min_length=1, max_length=NODE_NAME_MAX_LENGTH)]


def node_name(env: Mapping[str, str] | None = None) -> str | None:
    """The machine label (``NODE_NAME``) set by the CLI, or ``None`` when unknown."""
    value = (os.environ if env is None else env).get("NODE_NAME", "").strip()
    return value[:NODE_NAME_MAX_LENGTH] or None


def monitor_status_topic(client_id: str) -> str:
    return f"{STATUS_PREFIX}/monitors/{validate_segment(client_id, 'client_id')}"


class MonitorState(StrEnum):
    ONLINE = "online"
    STOPPED = "stopped"
    OFFLINE = "offline"


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LatencyStats(_Model):
    """Milliseconds from the device timestamp to reception (needs synchronized clocks)."""

    last: Finite
    avg: Finite
    p95: Finite


class ReceivedMessage(_Model):
    """One message as the monitor received it (what was actually delivered to it)."""

    topic: NonEmpty
    device_id: Segment
    value: Finite
    unit: str | None
    sequence: Count
    event_id: uuid.UUID
    received_at: AwareDatetime
    latency_ms: Finite


class MonitorCounters(_Model):
    received: Count
    invalid: Count
    devices_seen: Count
    by_domain: dict[str, Count]
    latency_ms: LatencyStats | None = None
    last_received_at: AwareDatetime | None = None
    # Latest valid messages received, oldest first.
    recent: Annotated[list[ReceivedMessage], Field(max_length=RECENT_MESSAGES)] = []


class DeliveryCounters(_Model):
    """Consumers that hand events to the API (ingestion): where the events went."""

    api_url: NonEmpty
    buffered: Count  # received, not yet stored
    dropped: Count  # oldest events dropped because the buffer was full
    batches: Count  # batches the API answered
    stored: Count
    duplicates: Count
    rejected: Count  # invalid items refused by the API
    api_errors: Count  # failed attempts (retried)
    last_delivery_at: AwareDatetime | None = None


class AlertCounters(_Model):
    """The alert engine: alerts it raised and how many are open now."""

    open: Count
    raised: Count
    cleared: Count
    open_by_severity: dict[str, Count]


class MonitorStatus(_Model):
    """Status of one monitor connection (schema version 1)."""

    schema_version: Literal[1] = 1
    client_id: Segment
    monitor: Segment
    topic_filter: NonEmpty
    node: NodeName | None = None  # machine label; None when unknown
    hostname: NonEmpty  # container hostname
    state: MonitorState
    started_at: AwareDatetime
    updated_at: AwareDatetime
    counters: MonitorCounters | None = None  # None in the Last Will (offline)
    delivery: DeliveryCounters | None = None  # consumers that store events (ingestion)
    alerts: AlertCounters | None = None  # the alert engine

    @field_validator("started_at", "updated_at")
    @classmethod
    def _normalize_to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def topic(self) -> str:
        return monitor_status_topic(self.client_id)

    def to_payload(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_payload(cls, payload: bytes | str) -> MonitorStatus:
        return cls.model_validate_json(payload)
