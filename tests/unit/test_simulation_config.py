import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from monitoring_mqtt.cli.simulation import (
    SimulationError,
    compose_env,
    load_simulation,
    resolve_run_id,
    smoke_env,
    smoke_window_seconds,
)
from monitoring_mqtt.simulator.behavior_config import BehaviorConfig

REPO = Path(__file__).resolve().parents[2]
HEADER = "schema_version: 1\nworkers:\n"


def project_with(tmp_path: Path, topology: str) -> Path:
    (tmp_path / "config").mkdir()
    shutil.copy(REPO / "config" / "devices.yaml", tmp_path / "config" / "devices.yaml")
    shutil.copy(REPO / "config" / "alerts.yaml", tmp_path / "config" / "alerts.yaml")
    (tmp_path / "config" / "simulation.yaml").write_text(topology)
    return tmp_path


def test_repository_default_is_one_container_per_device_and_monitor_per_domain() -> None:
    plan = load_simulation(REPO).plan
    assert [(w.service, w.replicas, w.devices_per_worker) for w in plan.workers] == [
        ("device-aquaculture", 1, 1),
        ("device-poultry", 1, 1),
        ("device-cattle", 1, 1),
    ]
    assert [(m.service, m.topic_filter, m.replicas) for m in plan.monitors] == [
        ("monitor-aquaculture", "monitoring/v1/aquaculture/#", 1),
        ("monitor-poultry", "monitoring/v1/poultry/#", 1),
        ("monitor-cattle", "monitoring/v1/cattle/#", 1),
        ("monitor-all", "monitoring/v1/#", 0),
    ]


def test_plan_and_compose_variables(tmp_path: Path) -> None:
    root = project_with(
        tmp_path,
        "schema_version: 1\nworkers:\n"
        "  dissolved_oxygen_probe: {replicas: 5, devices_per_worker: 10}\n",
    )
    sim = load_simulation(root)
    plan = sim.plan
    assert [w.devices for w in plan.workers] == [50, 0, 0]
    assert [m.replicas for m in plan.monitors] == [0, 0, 0, 0]  # no monitors section
    env = compose_env(sim, "run-x")
    topology_env = {k: v for k, v in env.items() if k.startswith(("DEVICE_", "MONITOR_"))}
    assert topology_env == {
        "DEVICE_AQUACULTURE_REPLICAS": "5",
        "DEVICE_AQUACULTURE_DEVICES_PER_WORKER": "10",
        "DEVICE_POULTRY_REPLICAS": "0",
        "DEVICE_POULTRY_DEVICES_PER_WORKER": "1",
        "DEVICE_CATTLE_REPLICAS": "0",
        "DEVICE_CATTLE_DEVICES_PER_WORKER": "1",
        "MONITOR_AQUACULTURE_REPLICAS": "0",
        "MONITOR_POULTRY_REPLICAS": "0",
        "MONITOR_CATTLE_REPLICAS": "0",
        "MONITOR_ALL_REPLICAS": "0",
    }


def test_monitor_replicas(tmp_path: Path) -> None:
    root = project_with(
        tmp_path,
        HEADER + "  ammonia_detector: {replicas: 1, devices_per_worker: 1}\n"
        "monitors:\n  poultry: {replicas: 40}\n  all: {replicas: 2}\n",
    )
    env = compose_env(load_simulation(root), "run-x")
    assert env["MONITOR_POULTRY_REPLICAS"] == "40"
    assert env["MONITOR_ALL_REPLICAS"] == "2"
    assert env["MONITOR_CATTLE_REPLICAS"] == "0"


@pytest.mark.parametrize(
    ("topology", "message"),
    [
        (
            "schema_version: 1\nworkers:\n  toaster: {replicas: 1, devices_per_worker: 1}\n",
            "unknown device type",
        ),
        (
            HEADER + "  ammonia_detector: {replicas: -1, devices_per_worker: 1}\n",
            "invalid simulation config",
        ),
        (
            HEADER + "  ammonia_detector: {replicas: 1, devices_per_worker: 0}\n",
            "invalid simulation config",
        ),
        ("schema_version: 2\nworkers: {}\n", "invalid simulation config"),
        (
            HEADER + "  ammonia_detector: {replicas: 1, devices_per_worker: 1}\n"
            "monitors:\n  goats: {replicas: 1}\n",
            "unknown monitor kind",
        ),
        (
            HEADER + "  ammonia_detector: {replicas: 1, devices_per_worker: 1}\n"
            "monitors:\n  poultry: {replicas: -1}\n",
            "invalid simulation config",
        ),
        ("workers: [unclosed", "invalid YAML"),
    ],
)
def test_invalid_topologies(tmp_path: Path, topology: str, message: str) -> None:
    with pytest.raises(SimulationError, match=message):
        load_simulation(project_with(tmp_path, topology))


def test_missing_topology_file(tmp_path: Path) -> None:
    with pytest.raises(SimulationError, match="cannot read"):
        load_simulation(tmp_path)


def test_omitted_sections_use_defaults(tmp_path: Path) -> None:
    sim = load_simulation(
        project_with(
            tmp_path, HEADER + "  ammonia_detector: {replicas: 1, devices_per_worker: 1}\n"
        )
    )
    env = compose_env(sim, "run-x")
    assert {
        k: env[k]
        for k in (
            "RUN_ID",
            "SIMULATION_SEED",
            "LOG_LEVEL",
            "PUBLISH_INTERVAL_SECONDS",
            "MQTT_QOS",
            "MQTT_KEEPALIVE",
            "MQTT_CLEAN_SESSION",
            "MQTT_RECONNECT_MIN_DELAY_SECONDS",
            "MQTT_RECONNECT_MAX_DELAY_SECONDS",
        )
    } == {
        "RUN_ID": "run-x",
        "SIMULATION_SEED": "42",
        "LOG_LEVEL": "INFO",
        "PUBLISH_INTERVAL_SECONDS": "5",
        "MQTT_QOS": "1",
        "MQTT_KEEPALIVE": "30",
        "MQTT_CLEAN_SESSION": "true",
        "MQTT_RECONNECT_MIN_DELAY_SECONDS": "1",
        "MQTT_RECONNECT_MAX_DELAY_SECONDS": "30",
    }


EXPERIMENT = (
    "schema_version: 1\n"
    "run: {run_id: qos0-trial-1, seed: 7, log_level: warning}\n"
    "mqtt:\n  qos: 0\n  keepalive_seconds: 10\n  clean_session: false\n"
    "  reconnect: {min_delay_seconds: 2, max_delay_seconds: 8}\n"
    "devices: {publish_interval_seconds: 0.5}\n"
    "workers:\n  ammonia_detector: {replicas: 1, devices_per_worker: 1}\n"
)


def test_experiment_parameters_reach_compose(tmp_path: Path) -> None:
    sim = load_simulation(project_with(tmp_path, EXPERIMENT))
    env = compose_env(sim, resolve_run_id(sim))
    assert env["RUN_ID"] == "qos0-trial-1"  # configured run id is reused
    assert env["SIMULATION_SEED"] == "7"
    assert env["LOG_LEVEL"] == "WARNING"
    assert env["PUBLISH_INTERVAL_SECONDS"] == "0.5"
    assert env["MQTT_QOS"] == "0"
    assert env["MQTT_KEEPALIVE"] == "10"
    assert env["MQTT_CLEAN_SESSION"] == "false"
    assert env["MQTT_RECONNECT_MIN_DELAY_SECONDS"] == "2"
    assert env["MQTT_RECONNECT_MAX_DELAY_SECONDS"] == "8"


def test_dashboard_refresh_reaches_compose(tmp_path: Path) -> None:
    text = HEADER + "  ammonia_detector: {replicas: 1, devices_per_worker: 1}\n"
    sim = load_simulation(project_with(tmp_path, text + "dashboard: {refresh: 0s}\n"))
    env = compose_env(sim, "r")
    assert (env["DASHBOARD_REPLICAS"], env["DASHBOARD_REFRESH_SECONDS"]) == ("1", "0")
    off = tmp_path / "off"
    off.mkdir()
    sim = load_simulation(project_with(off, text + "dashboard: {enabled: false}\n"))
    assert compose_env(sim, "r")["DASHBOARD_REPLICAS"] == "0"


def test_run_id_generated_once_when_null() -> None:
    sim = load_simulation(REPO)
    assert sim.run.run_id is None
    now = datetime(2026, 11, 6, 3, 4, 5, tzinfo=UTC)
    assert resolve_run_id(sim, now=now) == "run-20261106T030405Z"


def test_smoke_window_follows_publish_interval(tmp_path: Path) -> None:
    assert smoke_window_seconds(load_simulation(REPO)) == 15  # 2 x 5 s + 5 s
    slow = EXPERIMENT.replace("publish_interval_seconds: 0.5", "publish_interval_seconds: 60")
    sim = load_simulation(project_with(tmp_path, slow))
    assert smoke_window_seconds(sim) == 125
    env = smoke_env(sim)
    assert env["SMOKE_TELEMETRY_WINDOW_SECONDS"] == "125"
    assert env["MQTT_QOS"] == "0"
    assert "RUN_ID" not in env


@pytest.mark.parametrize(
    "section",
    [
        "mqtt: {qos: 3}",
        "mqtt: {keepalive_seconds: 1}",
        "mqtt: {reconnect: {min_delay_seconds: 10, max_delay_seconds: 5}}",
        "mqtt: {unknown: 1}",
        "devices: {publish_interval_seconds: 0}",
        "run: {log_level: verbose}",
        "run: {run_id: 'bad id with spaces'}",
        "dashboard: {refresh: 5}",  # durations need a unit
        "dashboard: {refresh: -1s}",
    ],
)
def test_invalid_experiment_parameters(tmp_path: Path, section: str) -> None:
    text = HEADER + "  ammonia_detector: {replicas: 1, devices_per_worker: 1}\n" + section + "\n"
    with pytest.raises(SimulationError, match="invalid simulation config"):
        load_simulation(project_with(tmp_path, text))


BEHAVIOUR = (
    HEADER
    + "  ammonia_detector: {replicas: 1, devices_per_worker: 3}\n"
    + """
behavior:
  anomalies: {every: 2m, lasts: 30s, only: [ammonia_detector]}
  faults:
    - {device: "poultry-*-002", at: 1m, lasts: 30s, action: stop}
"""
)


def test_behaviour_reaches_the_workers(tmp_path: Path) -> None:
    sim = load_simulation(project_with(tmp_path, BEHAVIOUR))
    env = compose_env(sim, "run-1")
    assert BehaviorConfig.from_env(env["SIMULATION_BEHAVIOR"]) == sim.behavior
    assert (
        sim.behavior.describe() == "anomalies every 2m for 30s (both) on ammonia_detector; 1 fault"
    )


def test_no_behaviour_by_default() -> None:
    sim = load_simulation(REPO)
    assert compose_env(sim, "run-1")["SIMULATION_BEHAVIOR"] == ""
    assert sim.behavior.describe().startswith("none")


@pytest.mark.parametrize(
    ("behaviour", "message"),
    [
        ("anomalies: {every: 2m, only: [toaster]}", "unknown device type"),
        ("anomalies: {every: 30}", "with a unit"),
        ("faults: [{device: x, at: 1m, action: explode}]", "invalid simulation config"),
    ],
)
def test_invalid_behaviour_is_rejected_before_starting(
    tmp_path: Path, behaviour: str, message: str
) -> None:
    text = HEADER + "  ammonia_detector: {replicas: 1, devices_per_worker: 1}\nbehavior:\n  "
    with pytest.raises(SimulationError, match=message):
        load_simulation(project_with(tmp_path, text + behaviour + "\n"))


def test_anomalies_need_types_with_targets(tmp_path: Path) -> None:
    project = project_with(tmp_path, BEHAVIOUR)
    catalog = project / "config" / "devices.yaml"
    catalog.write_text(catalog.read_text().replace("    anomaly: {high: 40.0}", ""))
    with pytest.raises(SimulationError, match="anomaly targets"):
        load_simulation(project)
