"""List the workers registered in the broker: ``python -m monitoring_mqtt.tools.workers``.

Subscribes to the retained worker claims (``core/registry.py``) and prints one JSON object
per claim on stdout, then exits. It sees every worker connected to the broker, on any
machine. Logs go to stderr. Exit code 0, or 1 when the broker is unreachable.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import ConfigError, MqttSettings, env_float
from monitoring_mqtt.core.registry import CLAIM_QOS, WORKER_CLAIMS_FILTER, WorkerClaim

logger = logging.getLogger("monitoring_mqtt.workers")

# Retained claims arrive right after the subscription is acknowledged.
COLLECT_SECONDS = 1.0


def collect_claims(settings: MqttSettings, timeout: float) -> list[WorkerClaim] | None:
    """Current claims, or ``None`` if the broker could not be reached in ``timeout``."""
    claims: dict[str, WorkerClaim] = {}
    lock = threading.Lock()
    subscribed = threading.Event()

    def on_connected(client: mqtt.Client) -> None:
        client.subscribe(WORKER_CLAIMS_FILTER, qos=CLAIM_QOS)

    def on_subscribe(client, userdata, mid, reason_codes, properties) -> None:
        if not any(code.is_failure for code in reason_codes):
            subscribed.set()

    def on_message(client, userdata, message: mqtt.MQTTMessage) -> None:
        if not message.payload:
            return
        try:
            claim = WorkerClaim.from_payload(message.payload)
        except ValidationError:
            logger.warning("invalid claim on %s ignored", message.topic)
            return
        with lock:
            claims[claim.worker_id] = claim

    client = mqtt_helpers.create_client(
        f"workers-{uuid.uuid4().hex[:8]}", settings, on_connected=on_connected
    )
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    mqtt_helpers.start(client, settings)
    try:
        if not subscribed.wait(timeout):
            return None
        time.sleep(COLLECT_SECONDS)
        with lock:
            return sorted(claims.values(), key=lambda c: c.worker_id)
    finally:
        mqtt_helpers.stop(client)


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        settings = MqttSettings.from_env()
        timeout = env_float(os.environ, "SMOKE_TIMEOUT_SECONDS", 10.0, minimum=1.0)
    except ConfigError as exc:
        logger.error("invalid configuration: %s", exc)
        return 2
    claims = collect_claims(settings, timeout)
    if claims is None:
        logger.error("broker %s:%s unreachable in %ss", settings.host, settings.port, timeout)
        return 1
    for claim in claims:
        sys.stdout.write(claim.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
