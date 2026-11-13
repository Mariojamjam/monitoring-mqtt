"""Machine settings: which parts of the stack run here and where the broker is.

These are deployment settings, not experiment parameters, so they live in the user's
``.env`` (git-ignored) or in the process environment, which takes precedence like in
Docker Compose:

- ``STACK_ROLES``: comma-separated subset of ``broker``, ``devices``, ``monitors``,
  ``storage`` (PostgreSQL, API and ingestion consumer) and ``alerts`` (alert engine)
  (default: all of them, the single-machine setup);
- ``MQTT_BROKER_HOST`` / ``MQTT_BROKER_PORT``: a broker running elsewhere (another
  machine, a tunnel endpoint, ``host.docker.internal``). Empty means the stack's own broker.
  A loopback address (``127.0.0.1``, ``localhost``) means a tunnel listening on this
  machine: services reach it as ``host.docker.internal``, through the ``broker-relay``
  service where Docker needs it (see ``cli/dockerhost.py``);
- ``MQTT_BIND_ADDRESS`` / ``MQTT_HOST_PORT``: where this machine publishes its broker port
  (default ``127.0.0.1:1883``; ``0.0.0.0`` exposes it to the network);
- ``API_BIND_ADDRESS`` / ``API_HOST_PORT``: where this machine publishes the API
  (default ``127.0.0.1:8000``);
- ``NODE_NAME``: label of this machine in worker claims and monitor status (default: the
  hostname where the CLI runs).
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from monitoring_mqtt.core.status import NODE_NAME_MAX_LENGTH

ENV_FILE = ".env"
BROKER, DEVICES, MONITORS = "broker", "devices", "monitors"
STORAGE, ALERTS = "storage", "alerts"
ROLES = (BROKER, DEVICES, MONITORS, STORAGE, ALERTS)
API_PORT = 8000
LOCAL_BROKER_HOST = "broker"  # Compose service name, reachable inside the stack network
BROKER_PORT = 1883  # port inside the stack network (contract)
DEFAULT_BIND_ADDRESS = "127.0.0.1"
DOCKER_HOST_ALIAS = "host.docker.internal"  # the machine running the containers

_LABEL = r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?"
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class MachineError(ValueError):
    """Invalid or inconsistent machine settings."""


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines like Docker Compose: comments, quotes, ``export``."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if raw[:1] in ("'", '"') and raw.endswith(raw[0]) and len(raw) >= 2:
            value = raw[1:-1]
        else:
            value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
        values[key] = value
    return values


def machine_environment(
    project_root: Path, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """``.env`` values overridden by the process environment (Compose precedence)."""
    path = project_root / ENV_FILE
    try:
        from_file = parse_env_file(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except OSError as exc:
        raise MachineError(f"cannot read {path}: {exc}") from exc
    return {**from_file, **(os.environ if environ is None else environ)}


@dataclass(frozen=True)
class Machine:
    roles: frozenset[str]
    broker_host: str | None  # None: the stack's own broker
    broker_port: int
    bind_address: str
    host_port: int
    node: str = "unknown"
    api_bind_address: str = DEFAULT_BIND_ADDRESS
    api_host_port: int = API_PORT

    @property
    def api_url(self) -> str:
        """The API as seen from this machine (the CLI, a browser, curl)."""
        host = "127.0.0.1" if self.api_bind_address in ("0.0.0.0", "::") else self.api_bind_address
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{self.api_host_port}"

    def runs(self, role: str) -> bool:
        return role in self.roles

    @property
    def remote_broker(self) -> bool:
        return self.broker_host is not None

    @property
    def tunnel_on_host(self) -> bool:
        """The broker is a tunnel endpoint on this machine's loopback."""
        if self.broker_host is None:
            return False
        if self.broker_host.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.broker_host).is_loopback
        except ValueError:
            return False

    @property
    def distributed(self) -> bool:
        """Other machines may take part: remote broker, or local broker exposed beyond loopback."""
        if self.remote_broker:
            return True
        return not ipaddress.ip_address(self.bind_address).is_loopback

    @property
    def role_list(self) -> str:
        return ", ".join(role for role in ROLES if role in self.roles)

    def describe_broker(self) -> str:
        if self.tunnel_on_host:
            return f"tunnel on this machine {self.broker_host}:{self.broker_port}"
        if self.remote_broker:
            return f"remote {self.broker_host}:{self.broker_port}"
        return (
            f"local ({LOCAL_BROKER_HOST}:{BROKER_PORT}, published on "
            f"{self.bind_address}:{self.host_port})"
        )

    @property
    def broker_label(self) -> str:
        """Short broker name for on-screen headers."""
        if self.tunnel_on_host:
            return f"tunnel {self.broker_host}:{self.broker_port}"
        if self.remote_broker:
            return f"{self.broker_host}:{self.broker_port}"
        return f"{LOCAL_BROKER_HOST}:{BROKER_PORT} (local)"

    def compose_env(self, relay_listen: str | None = None) -> dict[str, str]:
        """Variables compose.yaml interpolates for the broker address and services.

        ``relay_listen`` is the address the ``broker-relay`` listens on, when this machine
        needs it (a tunnel on the loopback with Docker Engine on Linux).
        """
        if self.tunnel_on_host:
            services_host = DOCKER_HOST_ALIAS  # a container's loopback is its own
        else:
            services_host = self.broker_host or LOCAL_BROKER_HOST
        return {
            "MQTT_BROKER_HOST": services_host,
            "MQTT_BROKER_PORT": str(self.broker_port if self.remote_broker else BROKER_PORT),
            "BROKER_REPLICAS": "1" if self.runs(BROKER) else "0",
            "MQTT_BIND_ADDRESS": self.bind_address,
            "MQTT_HOST_PORT": str(self.host_port),
            "BROKER_RELAY_REPLICAS": "1" if relay_listen else "0",
            "RELAY_LISTEN_HOST": relay_listen or "",
            "RELAY_TARGET_HOST": self.broker_host if self.tunnel_on_host else "127.0.0.1",
            "RELAY_PORT": str(self.broker_port),
            "NODE_NAME": self.node,
            "API_BIND_ADDRESS": self.api_bind_address,
            "API_HOST_PORT": str(self.api_host_port),
        }


def _port(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise MachineError(f"{name}={raw!r} is not a port number") from None
    if not 1 <= value <= 65535:
        raise MachineError(f"{name}={value} is out of range (1-65535)")
    return value


def _roles(raw: str) -> frozenset[str]:
    names = [name.strip().lower() for name in raw.split(",") if name.strip()]
    if not names:
        return frozenset(ROLES)
    unknown = sorted(set(names) - set(ROLES))
    if unknown:
        raise MachineError(f"STACK_ROLES has unknown role(s) {unknown}; valid roles: {list(ROLES)}")
    return frozenset(names)


def _broker_host(raw: str) -> str | None:
    host = raw.strip()
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME.match(host):
            raise MachineError(
                f"MQTT_BROKER_HOST={host!r} is not a hostname or IP address"
            ) from None
    return host


def _bind_address(raw: str, name: str = "MQTT_BIND_ADDRESS") -> str:
    address = raw.strip() or DEFAULT_BIND_ADDRESS
    try:
        ipaddress.ip_address(address)
    except ValueError:
        raise MachineError(f"{name}={address!r} is not an IP address") from None
    return address


def _node(raw: str) -> str:
    value = raw.strip() or socket.gethostname()
    if len(value) > NODE_NAME_MAX_LENGTH:
        raise MachineError(f"NODE_NAME is longer than {NODE_NAME_MAX_LENGTH} characters")
    return value


def machine_from_env(env: Mapping[str, str]) -> Machine:
    roles = _roles(env.get("STACK_ROLES", ""))
    host = _broker_host(env.get("MQTT_BROKER_HOST", ""))
    if host is None and env.get("MQTT_BROKER_PORT", "").strip():
        raise MachineError("MQTT_BROKER_PORT is set but MQTT_BROKER_HOST is empty")
    if host is not None and BROKER in roles:
        raise MachineError(
            f"MQTT_BROKER_HOST={host} points to another broker, but STACK_ROLES includes "
            "'broker'; remove one of them"
        )
    if host is None and BROKER not in roles:
        raise MachineError(
            "STACK_ROLES does not include 'broker', so set MQTT_BROKER_HOST to the broker "
            "this machine should use"
        )
    return Machine(
        roles=roles,
        broker_host=host,
        broker_port=_port(env, "MQTT_BROKER_PORT", BROKER_PORT),
        bind_address=_bind_address(env.get("MQTT_BIND_ADDRESS", "")),
        host_port=_port(env, "MQTT_HOST_PORT", BROKER_PORT),
        node=_node(env.get("NODE_NAME", "")),
        api_bind_address=_bind_address(env.get("API_BIND_ADDRESS", ""), "API_BIND_ADDRESS"),
        api_host_port=_port(env, "API_HOST_PORT", API_PORT),
    )


def load_machine(project_root: Path, environ: Mapping[str, str] | None = None) -> Machine:
    return machine_from_env(machine_environment(project_root, environ))
