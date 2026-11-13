"""Response models of the read side (what the API returns, documented in OpenAPI)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = Field(
        description="Pass it as `cursor` to get the next page; null on the last page."
    )


class Device(BaseModel):
    device_id: str
    device_type: str
    domain: str
    metric: str
    unit: str | None
    site_id: str
    worker_id: str
    first_seen_at: datetime
    last_seen_at: datetime = Field(description="Generation time of its latest measurement")
    last_value: float
    last_sequence: int
    last_run_id: str
    open_alerts: int = 0


class Alert(BaseModel):
    alert_id: uuid.UUID
    rule_id: str
    severity: str
    state: str = Field(description="`raised` (open) or `cleared`")
    device_id: str
    device_type: str
    domain: str
    metric: str
    unit: str | None
    run_id: str
    condition: str = Field(description="`above` or `below` the threshold")
    threshold: float
    raised_value: float | None
    raised_event_id: uuid.UUID | None = Field(description="Measurement that raised it")
    raised_at: datetime | None
    cleared_value: float | None
    cleared_event_id: uuid.UUID | None
    cleared_at: datetime | None
    duration_seconds: float | None = Field(None, description="Only for cleared alerts")
    received_at: datetime
    stored_at: datetime

    @classmethod
    def of(cls, row: dict) -> Alert:
        duration = None
        if row.get("raised_at") and row.get("cleared_at"):
            duration = round((row["cleared_at"] - row["raised_at"]).total_seconds(), 3)
        return cls.model_validate({**row, "duration_seconds": duration})


class DeviceDetail(Device):
    measurements: int
    open_alerts: list[Alert]  # type: ignore[assignment]


class Measurement(BaseModel):
    event_id: uuid.UUID
    run_id: str
    device_id: str
    domain: str
    metric: str
    site_id: str
    value: float
    unit: str | None
    sequence: int
    generated_at: datetime = Field(description="On the device (payload timestamp)")
    received_at: datetime = Field(description="When the ingestion consumer received it")
    stored_at: datetime = Field(description="When the database stored it")


class SeriesPoint(BaseModel):
    bucket_start: datetime
    count: int
    avg: float
    min: float
    max: float


class Series(BaseModel):
    device_id: str
    bucket_seconds: float
    points: list[SeriesPoint]


class DomainCount(BaseModel):
    domain: str
    measurements: int
    devices: int


class Run(BaseModel):
    run_id: str
    first_seen_at: datetime
    last_seen_at: datetime
    measurements: int
    devices: int
    alerts: int


class RunDetail(Run):
    domains: list[DomainCount]


class Stats(BaseModel):
    measurements: int
    devices: int
    runs: int
    alerts_total: int
    alerts_open: int
    last_stored_at: datetime | None
    received_to_stored_ms: float | None = Field(
        description="Average over the latest 1000 measurements: ingestion → database"
    )
    generated_to_stored_ms: float | None = Field(
        description="Average over the latest 1000 measurements: device → database"
    )
