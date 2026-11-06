"""Monitoring MQTT: Publish/Subscribe lab for chemical-monitoring IoT devices."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("monitoring-mqtt")
except PackageNotFoundError:  # pragma: no cover - running from a non-installed tree
    __version__ = "0.0.0+unknown"
