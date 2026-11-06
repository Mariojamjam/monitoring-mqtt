import pytest

from monitoring_mqtt.core.identity import host_short_id, sanitize_segment
from monitoring_mqtt.simulator.identity import device_id, resolve_worker_id


def test_host_short_id_uses_container_hostname_prefix() -> None:
    assert host_short_id("b61d73a9c0ff") == "b61d73"


def test_sanitize_segment() -> None:
    assert sanitize_segment("Worker A!") == "worker-a"
    with pytest.raises(ValueError):
        sanitize_segment("!!!")


def test_worker_id_prefers_env() -> None:
    assert resolve_worker_id({"WORKER_ID": "LabA"}, hostname="b61d73a9c0ff") == "laba"
    assert resolve_worker_id({}, hostname="b61d73a9c0ff") == "b61d73"


def test_device_id_format() -> None:
    assert device_id("aquaculture", "3f9a1c", 1) == "aquaculture-3f9a1c-001"
    assert device_id("cattle", "3f9a1c", 12) == "cattle-3f9a1c-012"
    with pytest.raises(ValueError):
        device_id("aquaculture", "3f9a1c", 0)
