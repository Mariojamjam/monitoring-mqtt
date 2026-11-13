"""HTTP ingestion contract between the ingestion consumer and the API.

The consumer receives events from the broker and posts them to the API in batches::

    POST /api/ingest/measurements  {"source": "...", "items": [{"received_at": …, "event": {…}}]}
    POST /api/ingest/alerts        same envelope; ``event`` is an alert event

``event`` is exactly the MQTT payload (``core.models.Measurement`` or
``core.alerts.AlertEvent``); ``received_at`` is when the consumer got it from the broker.
The API validates every item on its own: valid items are stored, invalid ones are reported
back one by one, so a single bad event never blocks a batch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

MAX_BATCH_ITEMS = 1000

NonEmpty = Annotated[str, Field(min_length=1, max_length=128)]


class IngestItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    received_at: AwareDatetime
    event: dict[str, Any]  # validated by the API against the event contract


class IngestBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: NonEmpty
    items: list[IngestItem]

    @classmethod
    def of(cls, source: str, items: list[tuple[datetime, dict[str, Any]]]) -> IngestBatch:
        return cls(
            source=source,
            items=[IngestItem(received_at=at, event=event) for at, event in items],
        )


class Rejected(BaseModel):
    index: int
    reason: str


class IngestResult(BaseModel):
    """What the API did with a batch."""

    received: int
    stored: int  # new rows (or alert transitions applied)
    duplicates: int  # already stored: QoS 1 duplicates or a retried batch
    rejected: list[Rejected] = Field(default_factory=list)
