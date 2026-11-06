"""Telemetry payload contract (schema version 1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from monitoring_mqtt.core.topics import SEGMENT_PATTERN, parse_telemetry_topic, telemetry_topic

SCHEMA_VERSION = 1

Segment = Annotated[str, Field(pattern=SEGMENT_PATTERN)]
NonEmpty = Annotated[str, Field(min_length=1)]


class TopicMismatchError(ValueError):
    """Raised when a payload does not match the topic it was received on."""


class Measurement(BaseModel):
    """Immutable event describing one measurement published by a virtual device."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = SCHEMA_VERSION
    event_id: uuid.UUID
    run_id: NonEmpty
    device_id: Segment
    device_type: NonEmpty
    domain: Segment
    site_id: NonEmpty
    metric: Segment
    value: Annotated[float, Field(allow_inf_nan=False)]
    unit: str | None
    sequence: Annotated[int, Field(ge=0)]
    timestamp: AwareDatetime

    @field_validator("timestamp")
    @classmethod
    def _normalize_to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @classmethod
    def create(cls, **fields: object) -> Measurement:
        """Build a new event, generating ``event_id`` and a UTC ``timestamp`` when absent."""
        fields.setdefault("event_id", uuid.uuid4())
        fields.setdefault("timestamp", datetime.now(UTC))
        return cls.model_validate(fields)

    @property
    def topic(self) -> str:
        return telemetry_topic(self.domain, self.metric, self.device_id)

    def to_payload(self) -> bytes:
        """Serialize as UTF-8 JSON."""
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_payload(cls, payload: bytes | str) -> Measurement:
        """Parse and validate a JSON payload (raises ``pydantic.ValidationError``)."""
        return cls.model_validate_json(payload)

    def ensure_matches_topic(self, topic: str) -> None:
        """Raise ``TopicMismatchError`` if domain, metric or device differ from ``topic``."""
        parsed = parse_telemetry_topic(topic)
        expected = (parsed.domain, parsed.metric, parsed.device_id)
        actual = (self.domain, self.metric, self.device_id)
        if expected != actual:
            raise TopicMismatchError(
                f"payload {'/'.join(actual)} does not match topic {'/'.join(expected)}"
            )
