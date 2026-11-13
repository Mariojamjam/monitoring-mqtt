"""Send a live command to one device: ``python -m monitoring_mqtt.tools.control``.

    python -m monitoring_mqtt.tools.control DEVICE_ID ACTION [--lasts SECONDS]
                                            [--direction high|low]

Publishes the command on the device's control topic (``core/simulation.py``), waits for the
hosting worker's acknowledgement and prints it as one JSON object on stdout.

Exit codes: 0 accepted, 1 rejected by the worker, 2 invalid arguments or configuration,
3 no worker answered (the device does not exist or its worker is down), 4 broker
unreachable.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import uuid
from pathlib import Path

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import ConfigError, MqttSettings, env_float, env_str
from monitoring_mqtt.core.simulation import (
    CONTROL_ACTIONS,
    SIM_QOS,
    ControlAck,
    ControlCommand,
    ack_topic,
    control_topic,
)
from monitoring_mqtt.simulator.catalog import DEFAULT_CATALOG_PATH, CatalogError, load_catalog
from monitoring_mqtt.simulator.identity import worker_of_device

logger = logging.getLogger("monitoring_mqtt.control")

ACCEPTED, REJECTED, INVALID, NO_ANSWER, UNREACHABLE = 0, 1, 2, 3, 4
# The worker answers as soon as it receives the command.
DEFAULT_ANSWER_SECONDS = 5.0


def send(
    settings: MqttSettings, worker_id: str, command: ControlCommand, timeout: float
) -> tuple[int, ControlAck | None]:
    """Publish ``command`` and wait for its acknowledgement."""
    subscribed = threading.Event()
    answered = threading.Event()
    result: list[ControlAck] = []

    def on_connected(client: mqtt.Client) -> None:
        client.subscribe(ack_topic(command.request_id), qos=SIM_QOS)

    def on_subscribe(client, userdata, mid, reason_codes, properties) -> None:
        if not any(code.is_failure for code in reason_codes):
            subscribed.set()

    def on_message(client, userdata, message: mqtt.MQTTMessage) -> None:
        try:
            result.append(ControlAck.from_payload(message.payload))
            answered.set()
        except ValidationError:
            logger.warning("invalid acknowledgement ignored")

    client = mqtt_helpers.create_client(
        f"control-{uuid.uuid4().hex[:8]}", settings, on_connected=on_connected
    )
    client.on_subscribe = on_subscribe
    client.on_message = on_message
    mqtt_helpers.start(client, settings)
    try:
        if not subscribed.wait(timeout):
            return UNREACHABLE, None
        client.publish(
            control_topic(worker_id, command.device_id), command.to_payload(), qos=SIM_QOS
        )
        if not answered.wait(timeout):
            return NO_ANSWER, None
        ack = result[0]
        return (ACCEPTED if ack.accepted else REJECTED), ack
    finally:
        mqtt_helpers.stop(client)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="control", description=__doc__)
    parser.add_argument("device_id")
    parser.add_argument("action", choices=CONTROL_ACTIONS)
    parser.add_argument("--lasts", type=float, default=None, help="seconds (default: until resume)")
    parser.add_argument("--direction", choices=("high", "low"), default="high")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(message)s")
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return INVALID if exc.code else ACCEPTED
    try:
        settings = MqttSettings.from_env()
        timeout = env_float(os.environ, "CONTROL_TIMEOUT_SECONDS", DEFAULT_ANSWER_SECONDS, 1.0)
        catalog = load_catalog(Path(env_str(os.environ, "DEVICES_CONFIG",
                                            str(DEFAULT_CATALOG_PATH))))  # fmt: skip
        worker_id = worker_of_device(args.device_id, {t.domain for t in catalog.device_types})
        command = ControlCommand(
            device_id=args.device_id,
            action=args.action,
            lasts_seconds=args.lasts,
            direction=args.direction,
        )
    except (ConfigError, CatalogError, ValueError) as exc:
        logger.error("%s", exc)
        return INVALID
    code, ack = send(settings, worker_id, command, timeout)
    if ack is not None:
        sys.stdout.write(ack.model_dump_json() + "\n")
    elif code == NO_ANSWER:
        logger.error("no answer from worker %s in %ss", worker_id, timeout)
    else:
        logger.error("broker %s:%s unreachable in %ss", settings.host, settings.port, timeout)
    return code


if __name__ == "__main__":
    sys.exit(main())
