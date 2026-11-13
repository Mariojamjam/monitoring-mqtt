"""TUI entrypoint: ``python -m monitoring_mqtt.tui`` (run by ``monitoring-mqtt tui``).

Connects to the broker given by the usual ``MQTT_*`` variables as a read-only observer.
``PUBLISH_INTERVAL_SECONDS`` tells it when a silent device counts as stale, and
``TUI_BROKER_LABEL`` how to name the broker on screen. The device catalog
(``DEVICES_CONFIG``) gives the normal band of each device type. Exit code 2 on invalid
configuration.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from monitoring_mqtt.core.config import ConfigError, MqttSettings, env_float, env_str
from monitoring_mqtt.simulator.catalog import DEFAULT_CATALOG_PATH, CatalogError, load_catalog
from monitoring_mqtt.tui.app import MonitoringApp
from monitoring_mqtt.tui.feed import Feed
from monitoring_mqtt.tui.state import Band, SystemState


def main() -> int:
    try:
        settings = MqttSettings.from_env()
        interval = env_float(os.environ, "PUBLISH_INTERVAL_SECONDS", 5.0, minimum=0.1)
        catalog = load_catalog(
            Path(env_str(os.environ, "DEVICES_CONFIG", str(DEFAULT_CATALOG_PATH)))
        )
    except (ConfigError, CatalogError) as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2
    # The screen belongs to the UI: library and client logs must never write over it.
    logging.basicConfig(handlers=[logging.NullHandler()], force=True)
    label = os.environ.get("TUI_BROKER_LABEL", "").strip() or f"{settings.host}:{settings.port}"
    feed = Feed(settings)
    bands = {
        t.name: Band(t.operating_range.minimum, t.operating_range.maximum)
        for t in catalog.device_types
        if t.operating_range
    }
    MonitoringApp(feed, SystemState(interval, feed.client_id, bands), label).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
