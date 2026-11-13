"""Worker registry contract: workers claim their ``worker_id`` through the broker.

A worker's ``worker_id`` becomes part of every device ``client_id`` it hosts, so two
workers with the same id (for example on different machines) would make their devices
take over each other's MQTT connections. Before creating devices, each worker publishes a
retained claim on::

    monitoring/registry/v1/workers/{worker_id}

The registry lives outside ``monitoring/v1/#``, so telemetry subscribers never see it.
An empty retained payload removes a claim: a worker publishes it on a clean shutdown, and
the broker publishes it as the worker's Last Will when the connection is lost.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from monitoring_mqtt.core.status import NodeName
from monitoring_mqtt.core.topics import SEGMENT_PATTERN, TELEMETRY_ROOT, validate_segment

REGISTRY_VERSION = "v1"
REGISTRY_PREFIX = f"{TELEMETRY_ROOT}/registry/{REGISTRY_VERSION}"
WORKER_CLAIMS_FILTER = f"{REGISTRY_PREFIX}/workers/+"
# Claims are control messages: always QoS 1 and retained, whatever the experiment uses.
CLAIM_QOS = 1

Segment = Annotated[str, Field(pattern=SEGMENT_PATTERN)]
NonEmpty = Annotated[str, Field(min_length=1)]


def worker_claim_topic(worker_id: str) -> str:
    return f"{REGISTRY_PREFIX}/workers/{validate_segment(worker_id, 'worker_id')}"


class WorkerClaim(BaseModel):
    """Retained claim of one ``worker_id`` by one worker process (schema version 1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    worker_id: Segment
    instance_id: uuid.UUID  # one per process start
    hostname: NonEmpty  # container hostname: tells a restart from a different worker
    node: NodeName | None = None  # machine label (NODE_NAME); None when unknown
    run_id: NonEmpty
    device_types: list[NonEmpty]
    devices: Annotated[int, Field(ge=0)]
    started_at: AwareDatetime

    @field_validator("started_at")
    @classmethod
    def _normalize_to_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @classmethod
    def create(cls, **fields: object) -> WorkerClaim:
        fields.setdefault("instance_id", uuid.uuid4())
        fields.setdefault("started_at", datetime.now(UTC))
        return cls.model_validate(fields)

    @property
    def topic(self) -> str:
        return worker_claim_topic(self.worker_id)

    @property
    def precedence(self) -> tuple[datetime, int]:
        """Lower wins a conflict: the earlier start, then the lower instance id."""
        return (self.started_at, self.instance_id.int)

    def to_payload(self) -> bytes:
        return self.model_dump_json().encode("utf-8")

    @classmethod
    def from_payload(cls, payload: bytes | str) -> WorkerClaim:
        return cls.model_validate_json(payload)
