"""``monitoring-mqtt device``: live commands to one simulated device.

Each command runs the one-off ``control`` Compose service, which publishes the command on
the device's control topic and waits for the hosting worker's acknowledgement. The CLI
never talks to a worker directly; the command goes through the broker, like everything
else. The device's neighbours in the same worker are not affected.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console

from monitoring_mqtt.cli import compose
from monitoring_mqtt.cli.stack import _project_root, broker_env, load_machine_or_exit
from monitoring_mqtt.core.durations import parse_duration

CONTROL_SERVICE = "control"
ACCEPTED, REJECTED, INVALID, NO_ANSWER, UNREACHABLE = 0, 1, 2, 3, 4

app = typer.Typer(
    help="Send live commands to one simulated device (its neighbours keep publishing).",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)

DeviceArg = Annotated[
    str, typer.Argument(help="Device id, e.g. poultry-3f9a1c-002 ('stack workers', TUI).")
]
ForOption = Annotated[
    str | None,
    typer.Option("--for", help="How long, e.g. 30s or 2m (default: until 'device resume')."),
]

EFFECTS = {
    "stop": "switched off: disconnected, generates nothing",
    "pause": "connected but silent; its sequence continues without gaps",
    "delay": "measurements held back and sent together when the delay ends",
    "disconnect": "disconnected; measurements generated and dropped (sequence gaps)",
    "anomaly": "values leave the operating range and come back",
    "resume": "back to normal: every active episode ends",
}


def lasts_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return parse_duration(value)
    except ValueError as exc:
        err_console.print(f"[red]error:[/red] --for: {exc}")
        raise typer.Exit(code=INVALID) from exc


def send(ctx: typer.Context, device_id: str, action: str, *extra: str) -> None:
    root = _project_root(ctx)
    machine = load_machine_or_exit(root)
    args = ["run", "--rm", "-T", "--no-deps", CONTROL_SERVICE, device_id, action, *extra]
    try:
        result = compose.capture(root, *args, env=broker_env(root, machine))
    except compose.ComposeError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=exc.exit_code) from exc

    code = result.returncode
    ack = next(
        (json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")), None
    )
    if code == ACCEPTED and ack:
        console.print(
            f"[green]{action}[/green] {device_id}: accepted by worker {ack['worker_id']}. "
            f"{EFFECTS[action].capitalize()}; {ack['detail']}."
        )
    elif code == REJECTED and ack:
        err_console.print(f"[red]rejected:[/red] {device_id}: {ack['detail']}")
    elif code == NO_ANSWER:
        err_console.print(
            f"[red]error:[/red] no worker answered for {device_id}. Check the id with "
            "'monitoring-mqtt stack workers' or the terminal UI."
        )
    else:
        # Keep the tool's own message; drop Compose's container progress lines.
        lines = [
            line
            for line in result.stderr.splitlines()
            if line.strip() and not line.strip().startswith("Container ")
        ]
        err_console.print("\n".join(lines) or f"control exited with code {code}")
    raise typer.Exit(code=code)


def _duration_args(for_: str | None) -> list[str]:
    seconds = lasts_seconds(for_)
    return [] if seconds is None else ["--lasts", f"{seconds:g}"]


@app.command()
def stop(ctx: typer.Context, device_id: DeviceArg, for_: ForOption = None) -> None:
    """Switch the device off: it disconnects and generates nothing."""
    send(ctx, device_id, "stop", *_duration_args(for_))


@app.command()
def pause(ctx: typer.Context, device_id: DeviceArg, for_: ForOption = None) -> None:
    """Keep the device connected but silent."""
    send(ctx, device_id, "pause", *_duration_args(for_))


@app.command()
def delay(ctx: typer.Context, device_id: DeviceArg, for_: ForOption = None) -> None:
    """Hold the device's measurements back and send them together at the end."""
    send(ctx, device_id, "delay", *_duration_args(for_))


@app.command()
def disconnect(ctx: typer.Context, device_id: DeviceArg, for_: ForOption = None) -> None:
    """Disconnect the device; its measurements are generated and lost."""
    send(ctx, device_id, "disconnect", *_duration_args(for_))


@app.command()
def anomaly(
    ctx: typer.Context,
    device_id: DeviceArg,
    for_: ForOption = None,
    direction: Annotated[
        str, typer.Option("--direction", help="high or low (if the device type has it).")
    ] = "high",
) -> None:
    """Drive the device's values out of the operating range."""
    if direction not in ("high", "low"):
        err_console.print("[red]error:[/red] --direction must be high or low")
        raise typer.Exit(code=INVALID)
    send(ctx, device_id, "anomaly", *_duration_args(for_), "--direction", direction)


@app.command()
def resume(ctx: typer.Context, device_id: DeviceArg) -> None:
    """End every active episode of the device (manual, scheduled or random)."""
    send(ctx, device_id, "resume")
