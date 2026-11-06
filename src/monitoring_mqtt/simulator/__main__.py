"""Simulator entrypoint: ``python -m monitoring_mqtt.simulator``.

Starts a SimulationWorker hosting ``DEVICES_PER_WORKER`` virtual devices. Their types come
from the catalog at ``DEVICES_CONFIG``, optionally restricted by ``DEVICE_TYPES``
(comma-separated type names; Compose sets one type per worker service).

Before creating devices, the worker claims its ``worker_id`` in the broker registry, so
workers on different machines never share device client ids (``registration.py``).

Exit codes: 0 after a clean shutdown, 2 on invalid configuration, 3 when no worker id could
be claimed or another worker won it while running (the container restarts with a new id).
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
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
from monitoring_mqtt.core.registry import WorkerClaim
from monitoring_mqtt.core.status import node_name
from monitoring_mqtt.simulator.catalog import DEFAULT_CATALOG_PATH, CatalogError, load_catalog
from monitoring_mqtt.simulator.factory import build_devices, select_types
from monitoring_mqtt.simulator.identity import resolve_worker_id
from monitoring_mqtt.simulator.registration import (
    Registration,
    WorkerIdUnavailableError,
    acquire_worker_id,
)
from monitoring_mqtt.simulator.worker import SimulationWorker

logger = logging.getLogger("monitoring_mqtt.simulator")

WORKER_ID_CONFLICT_EXIT = 3


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

    hostname = socket.gethostname()
    node = node_name()

    def registration_for(candidate: str) -> Registration:
        claim = WorkerClaim.create(
            worker_id=candidate,
            hostname=hostname,
            node=node,
            run_id=run_id,
            device_types=[t.name for t in types],
            devices=count,
        )
        return Registration(claim, settings)

    stop = shutdown_event()
    try:
        registration, _ = acquire_worker_id(worker_id, registration_for)
    except WorkerIdUnavailableError as exc:
        logger.error("cannot start worker: %s", exc)
        return WORKER_ID_CONFLICT_EXIT
    worker_id = registration.worker_id
    lost = threading.Event()

    def on_lost(other: WorkerClaim) -> None:
        logger.error(
            "worker id lost to another worker, stopping %s",
            kv(worker_id=worker_id, other_hostname=other.hostname),
        )
        lost.set()
        stop.set()

    registration.watch(on_lost)

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

    try:
        SimulationWorker(worker_id, devices, interval).run(stop)
    finally:
        registration.release()
    return WORKER_ID_CONFLICT_EXIT if lost.is_set() else 0


if __name__ == "__main__":
    sys.exit(main())
