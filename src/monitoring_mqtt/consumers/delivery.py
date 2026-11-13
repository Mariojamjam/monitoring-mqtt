"""Delivery of batches to the API over HTTP, with retries.

Errors are split in two:

- ``RetryLater``: the API or its database is unavailable (connection error, timeout, 5xx,
  503). The batch is kept and sent again after a backoff;
- ``BatchRejected``: the API refused the whole batch as malformed (4xx). Retrying cannot
  help, so the batch is dropped and logged. Single invalid items never cause this: the API
  reports them one by one in the result.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import httpx

from monitoring_mqtt.core.ingest import IngestBatch, IngestResult

MEASUREMENTS_PATH = "/ingest/measurements"
ALERTS_PATH = "/ingest/alerts"


class RetryLaterError(RuntimeError):
    """Temporary failure: send the same batch again later."""


class BatchRejectedError(RuntimeError):
    """Permanent failure: the API refuses this batch."""


@dataclass
class Backoff:
    """Exponential backoff with jitter: 0.5 s, 1 s, 2 s … up to ``maximum``."""

    initial: float = 0.5
    maximum: float = 10.0
    attempt: int = 0

    def next_delay(self, rng: random.Random | None = None) -> float:
        delay = min(self.maximum, self.initial * 2**self.attempt)
        self.attempt += 1
        return delay * (0.5 + (rng or random).random() / 2)

    def reset(self) -> None:
        self.attempt = 0


class Delivery:
    def __init__(self, api_url: str, timeout: float = 5.0, client: httpx.Client | None = None):
        self.api_url = api_url.rstrip("/")
        self.client = client or httpx.Client(base_url=self.api_url, timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def send(self, path: str, batch: IngestBatch) -> IngestResult:
        try:
            response = self.client.post(path, content=batch.model_dump_json(),
                                        headers={"content-type": "application/json"})  # fmt: skip
        except httpx.HTTPError as exc:  # connection refused, timeout, broken connection
            raise RetryLaterError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 500:
            raise RetryLaterError(f"HTTP {response.status_code}")
        if response.status_code >= 400:
            raise BatchRejectedError(f"HTTP {response.status_code}: {response.text[:300]}")
        return IngestResult.model_validate_json(response.content)
