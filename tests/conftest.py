"""Shared test fixtures.

Logic tests use the self-contained catalog below instead of ``config/devices.yaml``, so
editing the shipped configuration never breaks them. Its type names deliberately differ
from the shipped ones.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.simulator.catalog import DeviceCatalog

CATALOG_YAML = """\
schema_version: 1
device_types:
  - name: oxygen_probe
    domain: aquaculture
    metric: dissolved_oxygen
    unit: mg/L
    site_prefix: pond
    generator: {center: 6.5, noise: 1.0, minimum: 3.0, maximum: 10.0}
  - name: ammonia_sensor
    domain: poultry
    metric: ammonia
    unit: ppm
    site_prefix: house
    generator: {center: 12.0, noise: 4.0, minimum: 0.0, maximum: 50.0}
  - name: ph_bolus
    domain: cattle
    metric: rumen_ph
    unit: null
    site_prefix: herd
    generator: {center: 6.3, noise: 0.3, minimum: 5.0, maximum: 7.5}
"""

RULES_YAML = """\
schema_version: 1
rules:
  - {id: oxygen-low, device_type: oxygen_probe, below: 4.0, after: 2, clear_at: 4.5}
  - {id: ammonia-high, device_type: ammonia_sensor, above: 30.0, severity: critical}
"""


MACHINE_VARIABLES = (
    "STACK_ROLES",
    "MQTT_BROKER_HOST",
    "MQTT_BROKER_PORT",
    "MQTT_BIND_ADDRESS",
    "MQTT_HOST_PORT",
    "NODE_NAME",
    "MONITORING_MQTT_COMPOSE_PROJECT",
)


@pytest.fixture(autouse=True)
def _isolate_machine_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not depend on the developer's own machine settings."""
    for name in MACHINE_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def catalog() -> DeviceCatalog:
    return DeviceCatalog.model_validate(yaml.safe_load(CATALOG_YAML))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """Minimal checkout: project marker, compose file and the test catalog."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "monitoring-mqtt"\n')
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "devices.yaml").write_text(CATALOG_YAML)
    (tmp_path / "config" / "alerts.yaml").write_text(RULES_YAML)
    return tmp_path


@pytest.fixture
def measurement() -> Callable[..., Measurement]:
    """Factory for valid measurements; keyword arguments override fields."""

    def make(**overrides: object) -> Measurement:
        fields: dict[str, object] = {
            "run_id": "run-1",
            "device_id": "aquaculture-abc123-001",
            "device_type": "oxygen_probe",
            "domain": "aquaculture",
            "site_id": "pond-01",
            "metric": "dissolved_oxygen",
            "value": 6.5,
            "unit": "mg/L",
            "sequence": 1,
        }
        fields.update(overrides)
        return Measurement.create(**fields)

    return make
