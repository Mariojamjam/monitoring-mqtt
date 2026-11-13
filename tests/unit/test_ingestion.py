import json
import random

import httpx
import pytest

from monitoring_mqtt.consumers.alerts import AlertEngine
from monitoring_mqtt.consumers.buffer import BatchBuffer
from monitoring_mqtt.consumers.delivery import (
    Backoff,
    BatchRejectedError,
    Delivery,
    RetryLaterError,
)
from monitoring_mqtt.consumers.ingestion import Ingestion, IngestionSettings
from monitoring_mqtt.consumers.rules import Rule, RulesFile
from monitoring_mqtt.core.alerts import AlertEvent
from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.core.ingest import IngestBatch, IngestResult
from monitoring_mqtt.core.status import MonitorState


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


# Buffer -----------------------------------------------------------------------------------


def test_batches_by_size_or_waiting_time() -> None:
    clock = Clock()
    buffer: BatchBuffer[int] = BatchBuffer(100, clock=clock)
    assert not buffer.ready(3, max_wait=1.0)
    buffer.add(1)
    buffer.add(2)
    assert not buffer.ready(3, max_wait=1.0)  # neither full nor old enough
    clock.now = 1.0
    assert buffer.ready(3, max_wait=1.0)  # the oldest waited 1 s
    buffer.add(3)
    assert buffer.take(2) == [1, 2] and buffer.take(5) == [3]


def test_undelivered_batch_goes_back_in_order() -> None:
    buffer: BatchBuffer[int] = BatchBuffer(100)
    for i in range(5):
        buffer.add(i)
    batch = buffer.take(3)
    buffer.add(5)
    buffer.give_back(batch)
    assert buffer.take(10) == [0, 1, 2, 3, 4, 5]


def test_full_buffer_drops_the_oldest() -> None:
    buffer: BatchBuffer[int] = BatchBuffer(3)
    for i in range(5):
        buffer.add(i)
    assert buffer.take(10) == [2, 3, 4]
    assert buffer.stats().dropped == 2


def test_backoff_grows_and_resets() -> None:
    backoff, rng = Backoff(initial=0.5, maximum=4.0), random.Random(1)
    delays = [backoff.next_delay(rng) for _ in range(6)]
    assert delays[0] <= 0.5 and delays[-1] <= 4.0 and delays[-1] >= 2.0  # capped, with jitter
    backoff.reset()
    assert backoff.next_delay(rng) <= 0.5


# Delivery ---------------------------------------------------------------------------------


def delivery_answering(*responses: httpx.Response | Exception) -> tuple[Delivery, list]:
    requests: list[httpx.Request] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = httpx.Client(base_url="http://api:8000", transport=httpx.MockTransport(handler))
    return Delivery("http://api:8000", client=client), requests


OK = IngestResult(received=1, stored=1, duplicates=0).model_dump(mode="json")
BATCH = IngestBatch(source="ingestion", items=[])


def test_delivery_success_and_errors() -> None:
    delivery, requests = delivery_answering(
        httpx.Response(200, json=OK),
        httpx.Response(503),
        httpx.ConnectError("refused"),
        httpx.Response(422, text="bad batch"),
    )
    assert delivery.send("/ingest/measurements", BATCH).stored == 1
    assert json.loads(requests[0].content) == {"source": "ingestion", "items": []}
    with pytest.raises(RetryLaterError):
        delivery.send("/ingest/measurements", BATCH)  # database down
    with pytest.raises(RetryLaterError):
        delivery.send("/ingest/measurements", BATCH)  # API down
    with pytest.raises(BatchRejectedError):
        delivery.send("/ingest/measurements", BATCH)  # retrying cannot help


# Ingestion --------------------------------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, bool]] = []
        self.on_message = None

    def will_set(self, *args, **kwargs) -> None:
        pass

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False):
        self.published.append((topic, payload, retain))

    def is_connected(self) -> bool:
        return True


class FakeDelivery:
    def __init__(self, *outcomes: IngestResult | Exception) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[tuple[str, IngestBatch]] = []

    def send(self, path: str, batch: IngestBatch) -> IngestResult:
        self.sent.append((path, batch))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        pass


def ingestion(delivery: FakeDelivery, **settings) -> Ingestion:
    return Ingestion(IngestionSettings(**settings), MqttSettings(), delivery=delivery,
                     client=FakeClient(), hostname="host")  # type: ignore[arg-type]  # fmt: skip


def test_measurements_are_batched_and_delivered(measurement) -> None:
    result = IngestResult(received=2, stored=2, duplicates=0)
    delivery = FakeDelivery(result)
    ing = ingestion(delivery, batch_size=2, flush_interval=60)
    for seq in (1, 2):
        m = measurement(sequence=seq)
        ing.handle(m.topic, m.to_payload())
    ing.handle("monitoring/v1/aquaculture/dissolved_oxygen/x", b"not json")  # counted, skipped
    assert ing.deliver_once(ing.measurements, "/ingest/measurements")
    path, batch = delivery.sent[0]
    assert path == "/ingest/measurements" and len(batch.items) == 2
    assert batch.items[0].event["sequence"] == 1 and batch.items[0].received_at.tzinfo
    status = ing.status(MonitorState.ONLINE)
    assert status.client_id == "ingestion" and status.counters is not None
    assert status.counters.invalid == 1
    assert status.delivery is not None and status.delivery.stored == 2


def test_unavailable_api_keeps_the_batch(measurement) -> None:
    delivery = FakeDelivery(RetryLaterError("down"), IngestResult(received=1, stored=1,
                                                                  duplicates=0))  # fmt: skip
    ing = ingestion(delivery, batch_size=1)
    m = measurement()
    ing.handle(m.topic, m.to_payload())
    assert not ing.deliver_once(ing.measurements, "/ingest/measurements")
    assert len(ing.measurements) == 1  # kept for the retry
    assert ing.deliver_once(ing.measurements, "/ingest/measurements")
    assert len(ing.measurements) == 0
    assert ing.status(MonitorState.ONLINE).delivery.api_errors == 1  # type: ignore[union-attr]


def test_persistent_session_and_alert_events(measurement) -> None:
    ing = ingestion(FakeDelivery())
    assert ing.mqtt_settings.clean_session is False  # whatever the experiment says
    smoke = measurement(domain="smoke", device_id="smoke-1", metric="probe")
    ing.handle(smoke.topic, smoke.to_payload())
    assert len(ing.measurements) == 0  # smoke probes are not history


# Alert engine -----------------------------------------------------------------------------


def test_engine_publishes_events_and_active_state(measurement) -> None:
    rule = Rule(id="oxygen-low", device_type="oxygen_probe", below=4.0)
    client = FakeClient()
    engine = AlertEngine(RulesFile(schema_version=1, rules=[rule]), MqttSettings(),
                         client=client, hostname="host")  # type: ignore[arg-type]  # fmt: skip
    low, normal = measurement(value=3.0, sequence=1), measurement(value=6.5, sequence=2)
    (raised,) = engine.handle(low.topic, low.to_payload())
    (cleared,) = engine.handle(normal.topic, normal.to_payload())
    topics = [(topic, payload is None, retain) for topic, payload, retain in client.published]
    assert topics == [
        (raised.topic, False, False),
        (raised.active_topic, False, True),  # open: retained
        (cleared.topic, False, False),
        (cleared.active_topic, True, True),  # closed: retained state removed
    ]
    assert AlertEvent.from_payload(client.published[0][1]) == raised
    counters = engine.status(MonitorState.ONLINE).alerts
    assert counters is not None and (counters.raised, counters.cleared, counters.open) == (1, 1, 0)
