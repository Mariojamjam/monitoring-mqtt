"""How containers reach the machine running Docker (``host.docker.internal``).

Docker Desktop (macOS, Windows, Linux) maps ``host.docker.internal`` to the host's
loopback, so a tunnel listening on ``127.0.0.1`` is reachable from containers. Docker
Engine on Linux maps it (``host-gateway``) to the default bridge gateway, usually
``172.17.0.1``; services listening only on ``127.0.0.1`` need the ``broker-relay``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass

Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]

BRIDGE_GATEWAY_FORMAT = "{{(index .IPAM.Config 0).Gateway}}"
TIMEOUT_SECONDS = 15


def run_docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
    )


class DockerHostError(RuntimeError):
    """The Docker daemon could not be inspected."""


@dataclass(frozen=True)
class DockerHost:
    desktop: bool
    bridge_gateway: str | None

    @property
    def needs_relay(self) -> bool:
        """Whether host loopback services need the relay to be reachable from containers."""
        return not self.desktop

    @property
    def alias_target(self) -> str | None:
        """What ``host.docker.internal`` reaches, as an address usable from the host."""
        return "127.0.0.1" if self.desktop else self.bridge_gateway


def inspect_docker_host(run: Runner = run_docker) -> DockerHost:
    try:
        info = run(["docker", "info", "--format", "{{.OperatingSystem}}"])
    except (OSError, subprocess.SubprocessError) as exc:
        raise DockerHostError(f"cannot run docker: {exc}") from exc
    if info.returncode != 0:
        raise DockerHostError((info.stderr.strip().splitlines() or ["docker info failed"])[-1])
    if "docker desktop" in info.stdout.lower():
        return DockerHost(desktop=True, bridge_gateway=None)
    bridge = run(["docker", "network", "inspect", "bridge", "--format", BRIDGE_GATEWAY_FORMAT])
    gateway = bridge.stdout.strip() if bridge.returncode == 0 else ""
    return DockerHost(desktop=False, bridge_gateway=gateway or None)
