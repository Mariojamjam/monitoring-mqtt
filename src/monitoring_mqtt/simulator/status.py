"""The worker's retained status (``core/status.py``): what its devices generated, published
and got acknowledged, refreshed every few seconds on the worker's plane connection.

Observers (the TUI, the experiment probe) compare it with what consumers received and
what was stored. Devices themselves never read it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import paho.mqtt.client as mqtt

from monitoring_mqtt.core.logging import kv
from monitoring_mqtt.core.status import (
    STATUS_INTERVAL_SECONDS,
    STATUS_QOS,
    MonitorState,
    PublisherCounters,
    WorkerStatus,
)

logger = logging.getLogger(__name__)
CLEAR_TIMEOUT_SECONDS = 2.0


class WorkerStatusReporter:
    def __init__(
        self,
        *,
        worker_id: str,
        run_id: str,
        hostname: str,
        node: str | None,
        qos: int,
        counters: Callable[[], Sequence[PublisherCounters]],
        client: mqtt.Client,
        interval: float = STATUS_INTERVAL_SECONDS,
    ) -> None:
        self.worker_id = worker_id
        self.run_id = run_id
        self.hostname = hostname
        self.node = node
        self.qos = qos
        self.counters = counters
        self.client = client
        self.interval = interval
        self.started_at = datetime.now(UTC)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def status(self, state: MonitorState) -> WorkerStatus:
        return WorkerStatus(
            worker_id=self.worker_id,
            run_id=self.run_id,
            node=self.node,
            hostname=self.hostname,
            state=state,
            qos=self.qos,
            started_at=self.started_at,
            updated_at=datetime.now(UTC),
            devices=list(self.counters()) if state is MonitorState.ONLINE else [],
        )

    def install_will(self) -> None:
        """Call before connecting: the broker announces ``offline`` if the worker vanishes."""
        offline = self.status(MonitorState.OFFLINE)
        self.client.will_set(offline.topic, offline.to_payload(), qos=STATUS_QOS, retain=True)

    def publish(self) -> None:
        if self.client.is_connected():
            status = self.status(MonitorState.ONLINE)
            self.client.publish(status.topic, status.to_payload(), qos=STATUS_QOS, retain=True)

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.interval):
                try:
                    self.publish()
                except Exception:
                    logger.exception("worker status not published %s", kv(worker=self.worker_id))

        self._thread = threading.Thread(target=loop, name="worker-status", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Publish the final counters, then remove the retained status (clean shutdown)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval)
        if not self.client.is_connected():
            return
        try:
            final = self.status(MonitorState.STOPPED)
            self.client.publish(final.topic, final.to_payload(), qos=STATUS_QOS, retain=False)
            cleared = self.client.publish(final.topic, None, qos=STATUS_QOS, retain=True)
            cleared.wait_for_publish(CLEAR_TIMEOUT_SECONDS)
        except (RuntimeError, ValueError):
            logger.warning("worker status not cleared %s", kv(worker=self.worker_id))
