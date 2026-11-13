import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from monitoring_mqtt.api.app import create_app
from monitoring_mqtt.api.docs import SCALAR_SCRIPT, standalone_page
from monitoring_mqtt.storage.queries import InvalidCursorError, Page, decode_cursor, encode_cursor
from monitoring_mqtt.storage.store import StorageUnavailableError
from monitoring_mqtt.storage.writes import WriteResult

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]


class FakeStore:
    """Records calls; ``down`` simulates a database outage."""

    def __init__(self) -> None:
        self.down = False
        self.stored: list = []
        self.calls: list[tuple[str, dict]] = []
        self.seen: set[uuid.UUID] = set()

    def _check(self) -> None:
        if self.down:
            raise StorageUnavailableError("connection refused")

    def ping(self) -> None:
        self._check()

    def store_measurements(self, items) -> WriteResult:
        self._check()
        new = [i for i in items if i.event.event_id not in self.seen]
        self.seen.update(i.event.event_id for i in items)
        self.stored += new
        return WriteResult(stored=len(new), duplicates=len(items) - len(new))

    def store_alerts(self, items) -> WriteResult:
        self._check()
        self.stored += items
        return WriteResult(stored=len(items), duplicates=0)

    def list_measurements(self, **filters) -> Page:
        self._check()
        self.calls.append(("measurements", filters))
        if filters.get("cursor") == "bad":
            raise InvalidCursorError("invalid cursor")
        return Page([], None)

    def list_alerts(self, **filters) -> Page:
        self.calls.append(("alerts", filters))
        return Page([], None)

    def series(self, device_id, bucket_seconds, **filters):
        self.calls.append(("series", {"bucket": bucket_seconds, **filters}))
        return [{"bucket_start": NOW, "count": 2, "avg": 1.5, "min": 1.0, "max": 2.0}]

    def get_device(self, device_id):
        return None

    def get_run(self, run_id):
        return None


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def client(store: FakeStore) -> TestClient:
    return TestClient(create_app(store=store))


def item(measurement, **overrides) -> dict:
    m = measurement(**overrides)
    return {"received_at": NOW.isoformat(), "event": m.model_dump(mode="json")}


def test_health_does_not_need_the_database(client: TestClient, store: FakeStore) -> None:
    store.down = True
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/ready")
    assert response.status_code == 503 and response.headers["retry-after"] == "2"


def test_batch_is_stored_once_and_invalid_items_are_reported(
    client: TestClient, store: FakeStore, measurement
) -> None:
    items = [item(measurement, sequence=1), item(measurement, sequence=2), {
        "received_at": NOW.isoformat(), "event": {"value": "high"}}]  # fmt: skip
    first = client.post("/ingest/measurements", json={"source": "t", "items": items}).json()
    assert (first["received"], first["stored"], first["duplicates"]) == (3, 2, 0)
    assert first["rejected"][0]["index"] == 2 and "Field required" in first["rejected"][0]["reason"]
    again = client.post("/ingest/measurements", json={"source": "t", "items": items[:2]}).json()
    assert (again["stored"], again["duplicates"]) == (0, 2)  # a retried batch
    assert store.stored[0].received_at == NOW


def test_ingest_limits_and_outages(client: TestClient, store: FakeStore, measurement) -> None:
    one = item(measurement)
    too_many = {"source": "t", "items": [one] * 1001}
    assert client.post("/ingest/measurements", json=too_many).status_code == 413
    malformed = client.post("/ingest/measurements", json={"items": "nope"})
    assert malformed.status_code == 422
    store.down = True
    response = client.post("/ingest/measurements", json={"source": "t", "items": [one]})
    assert response.status_code == 503  # the consumer keeps the batch and retries


def test_alerts_are_validated_with_the_alert_contract(client: TestClient) -> None:
    response = client.post("/ingest/alerts", json={"source": "t", "items": [
        {"received_at": NOW.isoformat(), "event": {"state": "raised"}}]})  # fmt: skip
    assert response.json()["rejected"][0]["index"] == 0


def test_query_parameters(client: TestClient, store: FakeStore) -> None:
    since = (NOW - timedelta(hours=1)).isoformat()
    response = client.get("/measurements", params={"device_id": "d1", "from": since, "limit": 10})
    assert response.status_code == 200 and response.json() == {"items": [], "next_cursor": None}
    _, filters = store.calls[-1]
    assert filters["device_id"] == "d1" and filters["limit"] == 10 and filters["since"].tzinfo
    assert client.get("/measurements", params={"limit": 0}).status_code == 422
    assert client.get("/measurements", params={"cursor": "bad"}).status_code == 400
    client.get("/alerts", params={"state": "active"})
    assert store.calls[-1][1]["state"] == "raised"
    assert client.get("/alerts", params={"state": "open"}).status_code == 422


def test_series_bucket(client: TestClient, store: FakeStore) -> None:
    body = client.get("/measurements/series", params={"device_id": "d1", "bucket": "5m"}).json()
    assert body["bucket_seconds"] == 300 and body["points"][0]["count"] == 2
    bad = client.get("/measurements/series", params={"device_id": "d1", "bucket": "30"})
    assert bad.status_code == 422 and "add a unit" in bad.json()["detail"]
    assert client.get("/measurements/series", params={"device_id": "d1", "bucket": "500ms"}
                      ).status_code == 422  # fmt: skip


def test_not_found(client: TestClient) -> None:
    assert client.get("/devices/x").status_code == 404
    assert client.get("/runs/x").status_code == 404


def test_cursor_round_trip() -> None:
    cursor = encode_cursor(NOW, 42)
    assert decode_cursor(cursor, 2) == [NOW.isoformat(), "42"]
    for bad in ("garbage", encode_cursor(1)):
        with pytest.raises(InvalidCursorError):
            decode_cursor(bad, 2)


def test_reference_page_and_openapi(client: TestClient) -> None:
    page = client.get("/docs")
    assert page.status_code == 200 and SCALAR_SCRIPT in page.text and "/openapi.json" in page.text
    schema = client.get("/openapi.json").json()
    assert {"Measurement", "AlertEvent", "IngestResult"} <= set(schema["components"]["schemas"])
    assert "/measurements/series" in schema["paths"]


def test_committed_openapi_document_is_up_to_date(client: TestClient) -> None:
    committed = json.loads((REPO / "docs" / "openapi.json").read_text(encoding="utf-8"))
    assert committed == client.get("/openapi.json").json(), (
        "docs/openapi.json is out of date: run "
        "`uv run python -m monitoring_mqtt.api.docs > docs/openapi.json`"
    )


def test_committed_reference_page_is_up_to_date(client: TestClient) -> None:
    committed = (REPO / "docs" / "api-reference.html").read_text(encoding="utf-8")
    expected = standalone_page("Monitoring MQTT API reference", client.get("/openapi.json").json())
    assert committed == expected, (
        "docs/api-reference.html is out of date: run "
        "`uv run python -m monitoring_mqtt.api.docs --html > docs/api-reference.html`"
    )
