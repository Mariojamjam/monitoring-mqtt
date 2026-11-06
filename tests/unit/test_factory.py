import pytest

from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.simulator.catalog import DeviceCatalog, DeviceType
from monitoring_mqtt.simulator.factory import build_devices, select_types


def fake_device(**kwargs):
    return kwargs  # capture constructor arguments without creating MQTT clients


def build(types: list[DeviceType], count: int) -> list[dict]:
    return build_devices(types, count, "abc123", "run-1", 42, MqttSettings(), fake_device)


def test_round_robin_with_per_type_site_numbering(catalog: DeviceCatalog) -> None:
    devices = build(select_types(catalog, None), 4)
    assert [d["device_id"] for d in devices] == [
        "aquaculture-abc123-001",
        "poultry-abc123-002",
        "cattle-abc123-003",
        "aquaculture-abc123-004",
    ]
    assert [d["profile"].metric for d in devices] == [
        "dissolved_oxygen",
        "ammonia",
        "rumen_ph",
        "dissolved_oxygen",
    ]
    assert [d["site_id"] for d in devices] == ["pond-01", "house-01", "herd-01", "pond-02"]


def test_worker_restricted_to_one_type(catalog: DeviceCatalog) -> None:
    # Production layout: each Compose worker service hosts a single device type.
    devices = build(select_types(catalog, ["ammonia_sensor"]), 3)
    assert [d["device_id"] for d in devices] == [
        "poultry-abc123-001",
        "poultry-abc123-002",
        "poultry-abc123-003",
    ]
    assert [d["site_id"] for d in devices] == ["house-01", "house-02", "house-03"]


def test_generators_reproducible_per_seed_and_independent_per_device(
    catalog: DeviceCatalog,
) -> None:
    probes = select_types(catalog, ["oxygen_probe"])

    def series(devices: list[dict], index: int) -> list[float]:
        return [devices[index]["generator"].next_value() for _ in range(5)]

    assert series(build(probes, 2), 0) == series(build(probes, 2), 0)
    assert series(build(probes, 2), 0) != series(build(probes, 2), 1)


def test_select_types_rejects_unknown(catalog: DeviceCatalog) -> None:
    with pytest.raises(ValueError, match="unknown device type"):
        select_types(catalog, ["toaster"])
