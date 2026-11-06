"""Simulator entrypoint: ``python -m monitoring_mqtt.simulator``.

Starts a SimulationWorker hosting ``DEVICES_PER_WORKER`` virtual devices. Their types come
from the catalog at ``DEVICES_CONFIG``, optionally restricted by ``DEVICE_TYPES``
(comma-separated type names; Compose sets one type per worker service).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from monitoring_mqtt.core.config import (
    ConfigError,
    MqttSettings,
    env_float,
    env_int,
    env_str,
    log_level_from_env,
)
from monitoring_mqtt.core.lifecycle import shutdown_event
from monitoring_mqtt.core.logging import configure_logging, kv
from monitoring_mqtt.simulator.catalog import DEFAULT_CATALOG_PATH, CatalogError, load_catalog
from monitoring_mqtt.simulator.factory import build_devices, select_types
from monitoring_mqtt.simulator.identity import resolve_worker_id
from monitoring_mqtt.simulator.worker import SimulationWorker

logger = logging.getLogger("monitoring_mqtt.simulator")


def default_run_id() -> str:
    return datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")


def main() -> int:
    env = os.environ
    try:
        configure_logging(log_level_from_env())
        settings = MqttSettings.from_env()
        interval = env_float(env, "PUBLISH_INTERVAL_SECONDS", 5.0, minimum=0.1)
        count = env_int(env, "DEVICES_PER_WORKER", 1, minimum=0, maximum=10_000)
        seed = env_int(env, "SIMULATION_SEED", 42)
        run_id = env_str(env, "RUN_ID", default_run_id())
        catalog_path = Path(env_str(env, "DEVICES_CONFIG", str(DEFAULT_CATALOG_PATH)))
        catalog = load_catalog(catalog_path)
        type_names = [n.strip() for n in env.get("DEVICE_TYPES", "").split(",") if n.strip()]
        types = select_types(catalog, type_names)
        worker_id = resolve_worker_id()
    except (ConfigError, CatalogError, ValueError) as exc:
        logging.basicConfig()
        logger.error("invalid configuration: %s", exc)
        return 2

    devices = build_devices(types, count, worker_id, run_id, seed, settings)
    logger.info(
        "simulator starting %s",
        kv(
            worker_id=worker_id,
            devices=count,
            types=",".join(t.name for t in types),
            run_id=run_id,
            interval_s=interval,
            broker=f"{settings.host}:{settings.port}",
            qos=settings.qos,
        ),
    )
    for device in devices:
        logger.info(
            "device configured %s",
            kv(device_id=device.device_id, type=device.profile.device_type, site=device.site_id),
        )

    SimulationWorker(worker_id, devices, interval).run(shutdown_event())
    return 0


if __name__ == "__main__":
    sys.exit(main())
