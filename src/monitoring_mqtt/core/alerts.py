"""Alert contract: derived events published by the alert engine.

Two topic families, both outside ``monitoring/v1/#`` (monitors never receive them)::

    monitoring/alerts/v1/events/{domain}/{severity}/{device_id}   raised / cleared (history)
    monitoring/alerts/v1/active/{device_id}/{rule_id}             current state, retained

An alert is an episode with one ``alert_id``: the ``raised`` event opens it and the
``cleared`` event, with the same id, closes it. The retained *active* topic holds the
``raised`` event while the alert is open and an empty payload once it is cleared, so a
newcomer (the terminal UI, or the engine itself after a restart) knows at once which alerts
are open. Events are QoS 1 and not retained.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from monitoring_mqtt.core.topics import SEGMENT_PATTERN, TELEMETRY_ROOT, validate_segment

ALERTS_PREFIX = f"{TELEMETRY_ROOT}/alerts/v1"
ALERT_EVENTS_FILTER = f"{ALERTS_PREFIX}/events/#"
ACTIVE_ALERTS_FILTER = f"{ALERTS_PREFIX}/active/#"
ALERTS_FILTER = f"{ALERTS_PREFIX}/#"
ALERT_QOS = 1

Segment = Annotated[str, Field(pattern=SEGMENT_PATTERN)]
NonEmpty = Annotated[str, Field(min_length=1)]
Finite = Annotated[float, Field(allow_inf_nan=False)]


class Severity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class AlertState(StrEnum):
    RAISED = "raised"
    CLEARED = "cleared"


class Condition(StrEnum):
    ABOVE = "above"
    BELOW = "below"


def alert_event_topic(domain: str, severity: Severity | str, device_id: str) -> str:
    return (
        f"{ALERTS_PREFIX}/events/{validate_segment(domain, 'domain')}/"
        f"{Severity(severity).value}/{validate_segment(device_id, 'device_id')}"
    )


def active_alert_topic(device_id: str, rule_id: str) -> str:
    return (
        f"{ALERTS_PREFIX}/active/{validate_segment(device_id, 'device_id')}/"
        f"{validate_segment(rule_id, 'rule_id')}"
    )


class AlertEvent(BaseModel):
    """An alert opening (``raised``) or closing (``cleared``), schema version 1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    alert_id: uuid.UUID
    state: AlertState
    rule_id: Segment
    severity: Severity
    condition: Condition
    threshold: Finite
    device_id: Segment
    device_type: NonEmpty
    domain: Segment
    metric: Segment
    unit: str | None
    run_id: NonEmpty
    value: Finite  # the measurement that caused this transition
    measurement_event_id: uuid.UUID
    measurement_timestamp: AwareDatetime
    at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("measurement_timestamp", "at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @property
    def topic(self) -> str:
        return alert_event_topic(self.domain, self.severity, self.device_id)

    @property
    def active_topic(self) -> str:
        return active_alert_topic(self.device_id, self.rule_id)

    def to_payload(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_payload(cls, payload: bytes | str) -> AlertEvent:
        return cls.model_validate_json(payload)
