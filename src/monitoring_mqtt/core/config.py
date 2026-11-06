"""Configuration read from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(ValueError):
    """Raised when an environment variable has an invalid value."""


def env_str(env: Mapping[str, str], name: str, default: str | None = None) -> str:
    value = env.get(name, "").strip()
    if value:
        return value
    if default is None:
        raise ConfigError(f"{name} is required")
    return default


def env_int(
    env: Mapping[str, str],
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ConfigError(f"{name}={value} out of range [{minimum}, {maximum}]")
    return value


def env_float(env: Mapping[str, str], name: str, default: float, minimum: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not value >= minimum:  # also rejects NaN
        raise ConfigError(f"{name} must be >= {minimum}, got {raw!r}")
    return value


def env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


@dataclass(frozen=True)
class MqttSettings:
    """Connection settings shared by every MQTT client of a service.

    Defaults match the default experiment in config/simulation.yaml, so services also run
    outside Compose (e.g. with ``uv run``) with sensible values.
    """

    host: str = "localhost"
    port: int = 1883
    qos: int = 1
    keepalive: int = 30
    clean_session: bool = True
    reconnect_min_delay: int = 1
    reconnect_max_delay: int = 30

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> MqttSettings:
        env = os.environ if env is None else env
        settings = cls(
            host=env_str(env, "MQTT_HOST", cls.host),
            port=env_int(env, "MQTT_PORT", cls.port, 1, 65535),
            qos=env_int(env, "MQTT_QOS", cls.qos, 0, 2),
            keepalive=env_int(env, "MQTT_KEEPALIVE", cls.keepalive, 5, 3600),
            clean_session=env_bool(env, "MQTT_CLEAN_SESSION", cls.clean_session),
            reconnect_min_delay=env_int(
                env, "MQTT_RECONNECT_MIN_DELAY_SECONDS", cls.reconnect_min_delay, 1, 3600
            ),
            reconnect_max_delay=env_int(
                env, "MQTT_RECONNECT_MAX_DELAY_SECONDS", cls.reconnect_max_delay, 1, 3600
            ),
        )
        if settings.reconnect_max_delay < settings.reconnect_min_delay:
            raise ConfigError(
                "MQTT_RECONNECT_MAX_DELAY_SECONDS must be >= MQTT_RECONNECT_MIN_DELAY_SECONDS"
            )
        return settings


def log_level_from_env(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    level = env_str(env, "LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL, got {level}")
    return level
