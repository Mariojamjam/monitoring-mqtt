"""Markdown reference of every command and option, generated from the CLI itself.

The versioned copy is regenerated with::

    uv run python -m monitoring_mqtt.cli.reference > docs/reference/cli.md

and a unit test fails when it is out of date.
"""

from __future__ import annotations

import sys

import typer
from typer.cli import get_docs_for_click
from typer.core import MARKUP_MODE_KEY

from monitoring_mqtt.cli.main import app

PROGRAM = "monitoring-mqtt"
TITLE = "Command line reference"
NOTE = (
    "Generated from the CLI by `uv run python -m monitoring_mqtt.cli.reference`: do not edit "
    "by hand. For commands grouped by task, see [commands.md](../commands.md)."
)


def markdown() -> str:
    """Every command, argument and option as Markdown (the same text as `--help`)."""
    command = typer.main.get_command(app)
    ctx = typer.Context(command, obj={MARKUP_MODE_KEY: app.rich_markup_mode})
    body = get_docs_for_click(obj=command, ctx=ctx, name=PROGRAM, title=TITLE).strip()
    heading, _, rest = body.partition("\n")
    return f"{heading}\n\n{NOTE}\n{rest}\n"


def main() -> int:
    sys.stdout.write(markdown())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
