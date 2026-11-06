import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from monitoring_mqtt.core.models import Measurement, TopicMismatchError

FIELDS = {
    "run_id": "run-1",
    "device_id": "aquaculture-3f9a1c-001",
    "device_type": "dissolved_oxygen_probe",
    "domain": "aquaculture",
    "site_id": "pond-01",
    "metric": "dissolved_oxygen",
    "value": 6.42,
    "unit": "mg/L",
    "sequence": 15,
}


def payload(**overrides: object) -> dict:
    """A valid wire payload (schema version 1) with optional overrides."""
    data = {
        "schema_version": 1,
        "event_id": "f729c4df-9c18-42e8-b2ed-e67a9d647fd5",
        "timestamp": "2026-11-06T03:00:00Z",
        **FIELDS,
    }
    data.update(overrides)
    return data


def test_create_fills_event_id_and_utc_timestamp() -> None:
    m = Measurement.create(**FIELDS)
    assert isinstance(m.event_id, uuid.UUID)
    assert m.timestamp.tzinfo is UTC
    assert m.schema_version == 1
    assert m.topic == "monitoring/v1/aquaculture/dissolved_oxygen/aquaculture-3f9a1c-001"


def test_payload_roundtrip_preserves_event() -> None:
    m = Measurement.create(**FIELDS)
    raw = m.to_payload()
    assert json.loads(raw)["timestamp"].endswith("Z")  # documented wire format
    assert Measurement.from_payload(raw) == m


def test_timestamp_normalized_to_utc() -> None:
    local = datetime(2026, 11, 6, 0, 0, tzinfo=timezone(timedelta(hours=-3)))
    m = Measurement.create(**FIELDS, timestamp=local)
    assert m.timestamp == datetime(2026, 11, 6, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "overrides",
    [
        {"value": float("nan")},  # contract: value is finite
        {"value": float("inf")},
        {"timestamp": "2026-11-06T03:00:00"},  # naive timestamp is ambiguous
        {"device_id": "Bad/Id"},  # would inject a topic level
        {"schema_version": 2},  # unknown schema version
        {"unexpected": "field"},  # v1 is closed: producers cannot add fields silently
    ],
)
def test_contract_violations_rejected(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        Measurement.model_validate(payload(**overrides))


def test_unit_may_be_null_for_dimensionless_metrics() -> None:
    assert Measurement.model_validate(payload(unit=None)).unit is None


def test_topic_consistency() -> None:
    m = Measurement.model_validate(payload())
    m.ensure_matches_topic(m.topic)
    with pytest.raises(TopicMismatchError):
        m.ensure_matches_topic("monitoring/v1/poultry/dissolved_oxygen/aquaculture-3f9a1c-001")
