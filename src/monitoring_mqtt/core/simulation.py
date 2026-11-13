"""Simulation plane: ground truth about simulated episodes, and live control of devices.

Ground truth. Every worker announces when one of its devices starts or ends an episode
(an anomaly, a pause, a delay, a disconnection, being switched off)::

    monitoring/sim/v1/devices/{device_id}

Consumers must not use it to process telemetry: it exists to check them. An alert engine,
for example, is evaluated by comparing its alerts with these events.

Control. Operators (``monitoring-mqtt device ...``) send commands to one device::

    monitoring/control/v1/workers/{worker_id}/devices/{device_id}

The worker hosting the device acknowledges each command on
``monitoring/control/v1/acks/{request_id}`` and applies it at the device's next publish
time; the effect then appears as ground truth events.

Both planes live outside ``monitoring/v1/#``, so telemetry subscribers never see them.
Messages are QoS 1 and never retained: an old command must not run again after a restart.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from monitoring_mqtt.core.topics import SEGMENT_PATTERN, TELEMETRY_ROOT, validate_segment

SIM_PREFIX = f"{TELEMETRY_ROOT}/sim/v1"
SIM_EVENTS_FILTER = f"{SIM_PREFIX}/devices/+"
CONTROL_PREFIX = f"{TELEMETRY_ROOT}/control/v1"
SIM_QOS = 1

Segment = Annotated[str, Field(pattern=SEGMENT_PATTERN)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class EpisodeKind(StrEnum):
    """What a device does during an episode."""

    ANOMALY = "anomaly"  # values leave the operating range and come back
    PAUSE = "pause"  # connected, but generates nothing (silence, no sequence gap)
    DELAY = "delay"  # generates on time, sends everything when the episode ends
    DISCONNECT = "disconnect"  # disconnected: generates and drops (sequence gaps)
    STOP = "stop"  # switched off: disconnected and generates nothing

    @property
    def affects_values(self) -> bool:
        return self is EpisodeKind.ANOMALY


class EpisodeSource(StrEnum):
    ANOMALIES = "anomalies"  # random, from behavior.anomalies
    INSTABILITY = "instability"  # random, from behavior.instability
    FAULT = "fault"  # scheduled, from behavior.faults
    MANUAL = "manual"  # a live command


class EpisodePhase(StrEnum):
    START = "start"
    END = "end"


RESUME = "resume"
ControlAction = Literal["anomaly", "pause", "delay", "disconnect", "stop", "resume"]
CONTROL_ACTIONS: tuple[str, ...] = (*(k.value for k in EpisodeKind), RESUME)


def sim_event_topic(device_id: str) -> str:
    return f"{SIM_PREFIX}/devices/{validate_segment(device_id, 'device_id')}"


def control_topic(worker_id: str, device_id: str) -> str:
    worker = validate_segment(worker_id, "worker_id")
    return f"{CONTROL_PREFIX}/workers/{worker}/devices/{validate_segment(device_id, 'device_id')}"


def worker_control_filter(worker_id: str) -> str:
    return f"{CONTROL_PREFIX}/workers/{validate_segment(worker_id, 'worker_id')}/devices/+"


def ack_topic(request_id: uuid.UUID) -> str:
    return f"{CONTROL_PREFIX}/acks/{request_id}"


def utc_now() -> datetime:
    return datetime.now(UTC)


class _Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    def to_payload(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def from_payload(cls, payload: bytes):
        return cls.model_validate_json(payload)


class SimEvent(_Message):
    """A device started or ended a simulated episode."""

    schema_version: Literal[1] = 1
    run_id: str
    worker_id: Segment
    device_id: Segment
    kind: EpisodeKind
    phase: EpisodePhase
    source: EpisodeSource
    direction: Literal["high", "low"] | None = None  # anomalies only
    until: AwareDatetime | None = None  # planned end (start events); None = open-ended
    request_id: uuid.UUID | None = None  # the command that caused it (manual source)
    at: AwareDatetime = Field(default_factory=utc_now)

    @property
    def topic(self) -> str:
        return sim_event_topic(self.device_id)


class ControlCommand(_Message):
    schema_version: Literal[1] = 1
    request_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    device_id: Segment
    action: ControlAction
    lasts_seconds: Positive | None = None  # None: until a `resume`
    direction: Literal["high", "low"] = "high"  # anomaly only
    issued_at: AwareDatetime = Field(default_factory=utc_now)


class ControlAck(_Message):
    schema_version: Literal[1] = 1
    request_id: uuid.UUID
    device_id: Segment
    worker_id: Segment
    accepted: bool
    detail: str
    at: AwareDatetime = Field(default_factory=utc_now)
