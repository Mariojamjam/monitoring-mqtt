import socket
from pathlib import Path

import pytest

from monitoring_mqtt.cli.machine import (
    ROLES,
    MachineError,
    load_machine,
    machine_from_env,
    parse_env_file,
)


def test_env_file_syntax() -> None:
    text = """
# comment
STACK_ROLES=devices,monitors   # inline comment
export MQTT_BROKER_HOST="pi.local"
MQTT_BROKER_PORT='1884'
EMPTY=
not a variable
"""
    assert parse_env_file(text) == {
        "STACK_ROLES": "devices,monitors",
        "MQTT_BROKER_HOST": "pi.local",
        "MQTT_BROKER_PORT": "1884",
        "EMPTY": "",
    }


def test_defaults_are_the_single_machine_stack() -> None:
    machine = machine_from_env({})
    assert machine.roles == frozenset(ROLES)
    assert not machine.remote_broker
    assert not machine.distributed
    env = machine.compose_env()
    assert env.pop("NODE_NAME") == socket.gethostname()  # default machine label
    assert env == {
        "MQTT_BROKER_HOST": "broker",
        "MQTT_BROKER_PORT": "1883",
        "BROKER_REPLICAS": "1",
        "MQTT_BIND_ADDRESS": "127.0.0.1",
        "MQTT_HOST_PORT": "1883",
        "BROKER_RELAY_REPLICAS": "0",
        "RELAY_LISTEN_HOST": "",
        "RELAY_TARGET_HOST": "127.0.0.1",
        "RELAY_PORT": "1883",
        "API_BIND_ADDRESS": "127.0.0.1",
        "API_HOST_PORT": "8000",
        "DASHBOARD_BIND_ADDRESS": "127.0.0.1",
        "DASHBOARD_HOST_PORT": "8501",
        "DASHBOARD_API_URL": "http://api:8000",
    }
    assert machine.api_url == "http://127.0.0.1:8000"
    assert machine.dashboard_url == "http://127.0.0.1:8501"


def test_dashboard_on_its_own_machine_reads_a_remote_api() -> None:
    remote = {"STACK_ROLES": "dashboard", "MQTT_BROKER_HOST": "pi.local"}
    with pytest.raises(MachineError, match="DASHBOARD_API_URL"):
        machine_from_env(remote)
    machine = machine_from_env({**remote, "DASHBOARD_API_URL": "http://10.0.0.5:8000/",
                                "DASHBOARD_BIND_ADDRESS": "0.0.0.0"})  # fmt: skip
    env = machine.compose_env()
    assert env["DASHBOARD_API_URL"] == "http://10.0.0.5:8000"
    assert env["DASHBOARD_BIND_ADDRESS"] == "0.0.0.0"
    assert machine.dashboard_url == "http://127.0.0.1:8501"
    with pytest.raises(MachineError, match="not an http"):
        machine_from_env({**remote, "DASHBOARD_API_URL": "api:8000"})


def test_broker_only_machine_exposed_to_the_network() -> None:
    machine = machine_from_env({"STACK_ROLES": "broker", "MQTT_BIND_ADDRESS": "0.0.0.0"})
    assert machine.role_list == "broker"
    assert machine.distributed
    assert machine.compose_env()["MQTT_BIND_ADDRESS"] == "0.0.0.0"


def test_devices_machine_with_remote_broker() -> None:
    machine = machine_from_env(
        {"STACK_ROLES": " Devices ", "MQTT_BROKER_HOST": "192.168.0.50", "MQTT_BROKER_PORT": "1884"}
    )
    assert machine.runs("devices") and not machine.runs("broker")
    assert machine.distributed
    env = machine.compose_env()
    assert env["MQTT_BROKER_HOST"] == "192.168.0.50"
    assert env["MQTT_BROKER_PORT"] == "1884"
    assert env["BROKER_REPLICAS"] == "0"
    assert machine.describe_broker() == "remote 192.168.0.50:1884"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"STACK_ROLES": "broker,toaster"}, "unknown role"),
        ({"MQTT_BROKER_HOST": "pi.local"}, "includes 'broker'"),
        ({"STACK_ROLES": "devices"}, "set MQTT_BROKER_HOST"),
        ({"MQTT_BROKER_PORT": "1884"}, "MQTT_BROKER_HOST is empty"),
        ({"STACK_ROLES": "devices", "MQTT_BROKER_HOST": "bad host!"}, "not a hostname"),
        ({"STACK_ROLES": "devices", "MQTT_BROKER_HOST": "pi", "MQTT_BROKER_PORT": "0"}, "range"),
        ({"MQTT_HOST_PORT": "http"}, "not a port"),
        ({"MQTT_BIND_ADDRESS": "everywhere"}, "not an IP address"),
    ],
)
def test_invalid_settings(env: dict[str, str], message: str) -> None:
    with pytest.raises(MachineError, match=message):
        machine_from_env(env)


def test_process_environment_overrides_env_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("STACK_ROLES=devices\nMQTT_BROKER_HOST=pi.local\n")
    assert load_machine(tmp_path, environ={}).broker_host == "pi.local"
    overridden = load_machine(tmp_path, environ={"MQTT_BROKER_HOST": "10.0.0.7"})
    assert overridden.broker_host == "10.0.0.7"
    assert overridden.role_list == "devices"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_broker_is_a_tunnel_on_this_machine(host: str) -> None:
    machine = machine_from_env(
        {"STACK_ROLES": "devices", "MQTT_BROKER_HOST": host, "MQTT_BROKER_PORT": "11883"}
    )
    assert machine.tunnel_on_host
    assert machine.describe_broker() == f"tunnel on this machine {host}:11883"
    env = machine.compose_env("172.17.0.1")
    # Containers cannot use their own loopback: they go through the Docker host alias.
    assert env["MQTT_BROKER_HOST"] == "host.docker.internal"
    assert env["MQTT_BROKER_PORT"] == "11883"
    assert env["BROKER_RELAY_REPLICAS"] == "1"
    assert env["RELAY_LISTEN_HOST"] == "172.17.0.1"
    assert env["RELAY_TARGET_HOST"] == host
    assert env["RELAY_PORT"] == "11883"
    assert machine.compose_env(None)["BROKER_RELAY_REPLICAS"] == "0"  # Docker Desktop


def test_remote_broker_is_not_a_tunnel() -> None:
    machine = machine_from_env({"STACK_ROLES": "devices", "MQTT_BROKER_HOST": "192.168.0.50"})
    assert not machine.tunnel_on_host


def test_node_name_override() -> None:
    assert machine_from_env({"NODE_NAME": " raspberry-pi "}).compose_env()["NODE_NAME"] == (
        "raspberry-pi"
    )
    with pytest.raises(MachineError, match="NODE_NAME"):
        machine_from_env({"NODE_NAME": "x" * 65})
