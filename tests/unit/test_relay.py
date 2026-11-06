import asyncio

from monitoring_mqtt.cli.dockerhost import inspect_docker_host
from monitoring_mqtt.tools.relay import Relay, RelaySettings


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(1024):
        writer.write(data)
        await writer.drain()
    writer.close()


async def _round_trip() -> tuple[bytes, int]:
    target = await asyncio.start_server(_echo, "127.0.0.1", 0)
    target_port = target.sockets[0].getsockname()[1]
    relay = Relay(RelaySettings("127.0.0.1", 0, "127.0.0.1", target_port))
    stop, started = asyncio.Event(), asyncio.Event()
    serving = asyncio.create_task(relay.serve(stop, started))
    await started.wait()

    reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
    writer.write(b"\x10\x0cMQTT")  # bytes are forwarded unchanged, whatever they are
    await writer.drain()
    reply = await asyncio.wait_for(reader.readexactly(6), 2)
    writer.close()
    stop.set()
    await serving
    target.close()
    return reply, relay.total


def test_relay_forwards_bytes_both_ways() -> None:
    reply, total = asyncio.run(_round_trip())
    assert reply == b"\x10\x0cMQTT"
    assert total == 1


async def _unreachable_target() -> bytes:
    probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    dead_port = probe.sockets[0].getsockname()[1]
    probe.close()
    await probe.wait_closed()
    relay = Relay(RelaySettings("127.0.0.1", 0, "127.0.0.1", dead_port))
    stop, started = asyncio.Event(), asyncio.Event()
    serving = asyncio.create_task(relay.serve(stop, started))
    await started.wait()
    reader, writer = await asyncio.open_connection("127.0.0.1", relay.port)
    data = await asyncio.wait_for(reader.read(), 2)  # the relay closes the client
    writer.close()
    stop.set()
    await serving
    return data


def test_relay_closes_clients_when_target_is_down() -> None:
    assert asyncio.run(_unreachable_target()) == b""


def _runner(operating_system: str, gateway: str = "172.17.0.1"):
    import subprocess

    def run(args):
        out = operating_system if args[1] == "info" else gateway
        return subprocess.CompletedProcess(args, 0, stdout=out + "\n", stderr="")

    return run


def test_relay_needed_only_on_docker_engine() -> None:
    engine = inspect_docker_host(_runner("Arch Linux"))
    assert engine.needs_relay and engine.bridge_gateway == "172.17.0.1"
    desktop = inspect_docker_host(_runner("Docker Desktop"))
    assert not desktop.needs_relay and desktop.alias_target == "127.0.0.1"
