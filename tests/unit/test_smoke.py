import uuid

from monitoring_mqtt.testing.smoke import SMOKE_DOMAIN, Check, Observation, build_probe, report


def test_probe_uses_its_own_domain() -> None:
    # No monitor subscribes to this domain, so the probe never disturbs the stack.
    assert build_probe("abcd1234").domain == SMOKE_DOMAIN
    assert build_probe("abcd1234").topic.startswith(f"monitoring/v1/{SMOKE_DOMAIN}/")


def test_observation_detects_the_probe_only_by_event_id() -> None:
    probe = build_probe("abcd1234")
    observation = Observation(probe.event_id)

    other_probe = build_probe("ffff0000")
    observation.record(other_probe.topic, other_probe.to_payload())
    assert not observation.probe_received.is_set()

    observation.record(probe.topic, probe.to_payload())
    assert observation.probe_received.is_set()
    assert not observation.telemetry_received.is_set()  # probes are not telemetry


def test_observation_counts_live_telemetry_and_invalid_messages(measurement) -> None:
    observation = Observation(uuid.uuid4())
    m = measurement()
    observation.record(m.topic, m.to_payload())
    observation.record(m.topic, b"not json")
    observation.record("monitoring/v1/cattle/rumen_ph/x-001", m.to_payload())  # topic mismatch

    assert observation.telemetry_received.is_set()
    assert (observation.telemetry_messages, observation.invalid_messages) == (1, 2)


def test_report_exit_codes() -> None:
    assert report([Check("a", True, ""), Check("b", True, "")]) == 0
    assert report([Check("a", True, ""), Check("b", False, "")]) == 1


def test_report_ignores_failed_optional_checks() -> None:
    assert report([Check("probe", True, ""), Check("telemetry", False, "", required=False)]) == 0
    assert report([Check("probe", False, ""), Check("telemetry", True, "", required=False)]) == 1
