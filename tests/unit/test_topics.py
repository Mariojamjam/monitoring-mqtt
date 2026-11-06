import pytest
from paho.mqtt.client import topic_matches_sub

from monitoring_mqtt.core.topics import (
    ALL_TELEMETRY_FILTER,
    InvalidTopicError,
    TelemetryTopic,
    domain_filter,
    parse_telemetry_topic,
    telemetry_topic,
    validate_filter,
)

TOPIC = "monitoring/v1/aquaculture/dissolved_oxygen/aquaculture-3f9a1c-001"


def test_build_and_parse_roundtrip() -> None:
    built = telemetry_topic("aquaculture", "dissolved_oxygen", "aquaculture-3f9a1c-001")
    assert built == TOPIC
    parsed = parse_telemetry_topic(built)
    assert parsed == TelemetryTopic("aquaculture", "dissolved_oxygen", "aquaculture-3f9a1c-001")


@pytest.mark.parametrize("bad", ["", "Aqua", "a/b", "a+b", "#", "-lead", "a b"])
def test_invalid_segments_rejected(bad: str) -> None:
    with pytest.raises(InvalidTopicError):
        telemetry_topic(bad, "metric", "dev-001")


@pytest.mark.parametrize(
    "topic",
    [
        "monitoring/v1/aquaculture/dissolved_oxygen",
        "monitoring/v2/aquaculture/dissolved_oxygen/dev",
        "other/v1/aquaculture/dissolved_oxygen/dev",
        "monitoring/v1/aquaculture/dissolved_oxygen/dev/extra",
    ],
)
def test_parse_rejects_malformed_topics(topic: str) -> None:
    with pytest.raises(InvalidTopicError):
        parse_telemetry_topic(topic)


def test_monitor_filters_select_the_intended_topics() -> None:
    # Same matching semantics the broker applies (MQTT 3.1.1 wildcards).
    assert topic_matches_sub(domain_filter("aquaculture"), TOPIC)
    assert not topic_matches_sub(domain_filter("poultry"), TOPIC)
    assert topic_matches_sub(ALL_TELEMETRY_FILTER, TOPIC)


@pytest.mark.parametrize("bad", ["", "a/#/b", "a/b#", "a/+b/c"])
def test_validate_filter_rejects_bad_wildcards(bad: str) -> None:
    with pytest.raises(InvalidTopicError):
        validate_filter(bad)


@pytest.mark.parametrize("good", ["#", "a/+/c", "monitoring/v1/+/ammonia/+", "a/b"])
def test_validate_filter_accepts_valid(good: str) -> None:
    assert validate_filter(good) == good
