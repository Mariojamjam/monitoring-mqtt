"""Integration fixtures: one isolated stack for the whole session.

The stack is started through the real CLI (``stack up --build`` with
``tests/integration/experiment.yaml``) under the Compose project ``monitoring-mqtt-it``
and removed at the end, also when tests fail. The user's own stack is never touched.

The tests reach the broker on a free port of 127.0.0.1. Inside the Dev Container, whose
localhost is not the host's, the container joins the test network and uses ``broker:1883``.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from collections.abc import Iterator

import pytest
from harness import EXPERIMENT, NETWORK, PROJECT, Observer, Stack, free_port, in_dev_container

from monitoring_mqtt.cli.compose import PROJECT_OVERRIDE_VARIABLE


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        item.add_marker(pytest.mark.integration)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(["docker", "info"], capture_output=True, check=False)
    return result.returncode == 0


@pytest.fixture(scope="session")
def stack() -> Iterator[Stack]:
    if not _docker_available():
        pytest.skip("Docker is not available")
    port = free_port()
    env = {
        **os.environ,
        PROJECT_OVERRIDE_VARIABLE: PROJECT,
        # Machine settings: one machine with every role, whatever the developer's .env says.
        "STACK_ROLES": "broker,devices,monitors",
        "MQTT_BROKER_HOST": "",
        "MQTT_BROKER_PORT": "",
        "MQTT_BIND_ADDRESS": "127.0.0.1",
        "MQTT_HOST_PORT": str(port),
        "NODE_NAME": "it",
    }
    # In-process Compose calls (from the cli package) must also target the isolated stack.
    os.environ[PROJECT_OVERRIDE_VARIABLE] = PROJECT
    stack = Stack(env, "127.0.0.1", port)
    joined = False
    try:
        up = stack.cli("stack", "up", "--build", "--config", str(EXPERIMENT))
        assert up.returncode == 0, f"stack up failed:\n{up.stdout}\n{up.stderr}"
        if in_dev_container():
            me = socket.gethostname()
            joined = stack.docker("network", "connect", NETWORK, me).returncode == 0
            stack.host, stack.port = "broker", 1883
        yield stack
    finally:
        if joined:
            stack.docker("network", "disconnect", NETWORK, socket.gethostname())
        if os.environ.get("IT_KEEP_STACK") != "1":
            stack.compose("--profile", "tools", "down", "--volumes", "--remove-orphans")


@pytest.fixture(scope="session")
def observer(stack: Stack) -> Iterator[Observer]:
    obs = Observer(stack.host, stack.port)
    obs.start()
    # Every device and every monitor is up before the first test.
    obs.wait_until(lambda: len(obs.telemetry) >= 5, "telemetry from the 5 devices", 60)
    obs.wait_until(
        lambda: sum(1 for s in obs.statuses.values() if s and s.state == "online") >= 5,
        "status from the 5 monitors",
        60,
    )
    yield obs
    obs.stop()
