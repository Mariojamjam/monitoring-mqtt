"""TUI entrypoint: ``python -m monitoring_mqtt.tui`` (run by ``monitoring-mqtt tui``).

Connects to the broker given by the usual ``MQTT_*`` variables as a read-only observer.
``PUBLISH_INTERVAL_SECONDS`` tells it when a silent device counts as stale, and
``TUI_BROKER_LABEL`` how to name the broker on screen. Exit code 2 on invalid configuration.
"""

from __future__ import annotations

import logging
import os
import sys

from monitoring_mqtt.core.config import ConfigError, MqttSettings, env_float
from monitoring_mqtt.tui.app import MonitoringApp
from monitoring_mqtt.tui.feed import Feed
from monitoring_mqtt.tui.state import SystemState


def main() -> int:
    try:
        settings = MqttSettings.from_env()
        interval = env_float(os.environ, "PUBLISH_INTERVAL_SECONDS", 5.0, minimum=0.1)
    except ConfigError as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    # The screen belongs to the UI: library and client logs must never write over it.
    logging.basicConfig(handlers=[logging.NullHandler()], force=True)
    label = os.environ.get("TUI_BROKER_LABEL", "").strip() or f"{settings.host}:{settings.port}"
    feed = Feed(settings)
    MonitoringApp(feed, SystemState(interval, feed.client_id), label).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
