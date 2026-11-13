"""TCP relay to a broker reachable only on the host's loopback: ``python -m ...tools.relay``.

A tunnel client often publishes the remote broker on ``127.0.0.1:<port>`` of the machine
running the devices. On Docker Engine for Linux, containers cannot reach the host's
loopback: ``host.docker.internal`` points to the Docker bridge gateway instead. The
``broker-relay`` service runs this relay in the host network namespace, listens on the
bridge gateway and forwards every connection to the loopback address, so the services
reach the tunnel as ``host.docker.internal:<port>``.

The relay forwards bytes only; it knows nothing about MQTT.

Environment: ``RELAY_LISTEN_HOST``, ``RELAY_LISTEN_PORT``, ``RELAY_TARGET_HOST`` (default
``127.0.0.1``), ``RELAY_TARGET_PORT``. Exit code 0 on SIGTERM/SIGINT, 2 on invalid
configuration, 1 if the listening address cannot be bound.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass

from monitoring_mqtt.core.config import ConfigError, env_int, env_str, log_level_from_env
from monitoring_mqtt.core.logging import configure_logging, kv

logger = logging.getLogger("monitoring_mqtt.relay")

CONNECT_TIMEOUT_SECONDS = 5.0
CHUNK_SIZE = 65_536


@dataclass(frozen=True)
class RelaySettings:
    listen_host: str
    listen_port: int
    target_host: str
    target_port: int

    @classmethod
    def from_env(cls) -> RelaySettings:
        env = os.environ
        return cls(
            listen_host=env_str(env, "RELAY_LISTEN_HOST"),
            listen_port=env_int(env, "RELAY_LISTEN_PORT", 1883, minimum=1, maximum=65535),
            target_host=env_str(env, "RELAY_TARGET_HOST", "127.0.0.1"),
            target_port=env_int(env, "RELAY_TARGET_PORT", 1883, minimum=1, maximum=65535),
        )


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Copy bytes until EOF or error, then close the destination."""
    try:
        while data := await reader.read(CHUNK_SIZE):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


class Relay:
    def __init__(self, settings: RelaySettings) -> None:
        self.settings = settings
        self.active = 0
        self.total = 0

    async def handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        peer = client_writer.get_extra_info("peername")
        target = f"{self.settings.target_host}:{self.settings.target_port}"
        try:
            target_reader, target_writer = await asyncio.wait_for(
                asyncio.open_connection(self.settings.target_host, self.settings.target_port),
                CONNECT_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError) as exc:
            logger.warning("relay target unreachable %s", kv(target=target, error=exc))
            client_writer.close()
            return
        self.active += 1
        self.total += 1
        logger.debug("relay connection opened %s", kv(peer=peer, active=self.active))
        try:
            await asyncio.gather(
                _pipe(client_reader, target_writer), _pipe(target_reader, client_writer)
            )
        finally:
            self.active -= 1
            logger.debug("relay connection closed %s", kv(peer=peer, active=self.active))

    async def serve(self, stop: asyncio.Event, started: asyncio.Event | None = None) -> None:
        server = await asyncio.start_server(
            self.handle, self.settings.listen_host, self.settings.listen_port
        )
        self.port = server.sockets[0].getsockname()[1]
        logger.info(
            "relay listening %s",
            kv(
                listen=f"{self.settings.listen_host}:{self.port}",
                target=f"{self.settings.target_host}:{self.settings.target_port}",
            ),
        )
        if started is not None:
            started.set()
        async with server:
            await stop.wait()
        logger.info("relay stopped %s", kv(connections=self.total))


async def _run(settings: RelaySettings) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await Relay(settings).serve(stop)
    except OSError as exc:
        logger.error(
            "cannot listen %s",
            kv(listen=f"{settings.listen_host}:{settings.listen_port}", error=exc),
        )
        return 1
    return 0


def main() -> int:
    try:
        configure_logging(log_level_from_env())
        settings = RelaySettings.from_env()
    except ConfigError as exc:
        logging.basicConfig()
        logger.error("invalid configuration: %s", exc)
        return 2
    return asyncio.run(_run(settings))


if __name__ == "__main__":
    sys.exit(main())
