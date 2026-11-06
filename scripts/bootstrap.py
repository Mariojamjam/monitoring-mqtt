#!/usr/bin/env python3
"""Install the ``monitoring-mqtt`` command on this machine.

Works the same on Linux, macOS and Windows. Requires only Python 3.9+ (standard library).

    python scripts/bootstrap.py          # Linux / macOS
    py scripts\\bootstrap.py              # Windows

Steps (safe to run again at any time):
1. find uv, or install it with the official installer after confirmation;
2. install Python 3.12 managed by uv (the system Python is never modified);
3. create the development environment from uv.lock;
4. install the CLI as a uv tool (editable, pinned to uv.lock versions);
5. make sure the uv tool directory is on PATH;
6. verify the command and run ``monitoring-mqtt doctor``.

Docker is checked by ``doctor`` but never installed.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path

MIN_PYTHON = (3, 9)
PROJECT_PYTHON = "3.12"
CLI_NAME = "monitoring-mqtt"
UV_INSTALLER_UNIX = "https://astral.sh/uv/install.sh"
UV_INSTALLER_WINDOWS = "https://astral.sh/uv/install.ps1"
# Some CDNs reject the default "Python-urllib" agent.
USER_AGENT = "monitoring-mqtt-bootstrap"
REPO_ROOT = Path(__file__).resolve().parent.parent
IS_WINDOWS = platform.system() == "Windows"


class BootstrapError(Exception):
    """A step failed; the message explains how to fix it."""


# --------------------------------------------------------------------------- output


def step(title: str) -> None:
    print(f"\n==> {title}", flush=True)


def info(message: str) -> None:
    print(f"    {message}", flush=True)


def run(args: Sequence[str], capture: bool = False) -> str:
    """Run a command (never through a shell) and raise BootstrapError on failure."""
    print(f"    $ {' '.join(str(a) for a in args)}", flush=True)
    try:
        result = subprocess.run(
            [str(a) for a in args],
            cwd=REPO_ROOT,
            check=False,
            text=True,
            capture_output=capture,
        )
    except FileNotFoundError as exc:
        raise BootstrapError(f"command not found: {args[0]}") from exc
    if result.returncode != 0:
        details = (result.stderr or "").strip() if capture else ""
        raise BootstrapError(
            f"'{' '.join(str(a) for a in args)}' exited with code {result.returncode}"
            + (f": {details}" if details else "")
        )
    return (result.stdout or "").strip()


# --------------------------------------------------------------------------- helpers


def executable_name(name: str) -> str:
    return f"{name}.exe" if IS_WINDOWS else name


def normalize(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expanduser(path)))


def is_on_path(directory: Path, path_value: str | None = None) -> bool:
    path_value = os.environ.get("PATH", "") if path_value is None else path_value
    target = normalize(str(directory))
    return any(normalize(entry) == target for entry in path_value.split(os.pathsep) if entry)


def uv_candidates(home: Path | None = None) -> list[Path]:
    """Locations used by the official uv installer (uv may not be on PATH yet)."""
    home = home or Path.home()
    exe = executable_name("uv")
    candidates = [home / ".local" / "bin" / exe, home / ".cargo" / "bin" / exe]
    xdg_bin = os.environ.get("XDG_BIN_HOME")
    if xdg_bin:
        candidates.insert(0, Path(xdg_bin) / exe)
    return candidates


def find_uv() -> Path | None:
    found = shutil.which("uv")
    if found:
        return Path(found)
    for candidate in uv_candidates():
        if candidate.is_file():
            return candidate
    return None


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        info("No interactive terminal; rerun with --yes to accept.")
        return False
    answer = input(f"    {question} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            destination.write_bytes(response.read())
    except (urllib.error.URLError, OSError) as exc:
        raise BootstrapError(
            f"could not download {url} ({exc}). Check the network or install uv manually: "
            "https://docs.astral.sh/uv/getting-started/installation/"
        ) from exc


def installer_command(script: Path) -> list[str]:
    if IS_WINDOWS:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    return ["sh", str(script)]


# --------------------------------------------------------------------------- steps


def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        raise BootstrapError(
            f"Python {'.'.join(map(str, MIN_PYTHON))}+ is required to run this script; "
            f"found {platform.python_version()}."
        )


def ensure_uv(assume_yes: bool) -> Path:
    step("Checking uv")
    uv = find_uv()
    if uv is not None:
        info(f"found {uv} ({run([uv, '--version'], capture=True)})")
        return uv

    url = UV_INSTALLER_WINDOWS if IS_WINDOWS else UV_INSTALLER_UNIX
    info("uv is not installed. It manages Python and the project dependencies.")
    info(f"The official installer ({url}) installs it for the current user only;")
    info("no administrator privileges are needed.")
    if not confirm("Install uv now?", assume_yes):
        raise BootstrapError(
            "uv is required. Install it from https://docs.astral.sh/uv/ and run this again."
        )

    suffix = ".ps1" if IS_WINDOWS else ".sh"
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / f"uv-installer{suffix}"
        info(f"downloading {url}")
        download(url, script)
        run(installer_command(script))

    uv = find_uv()
    if uv is None:
        raise BootstrapError("uv was installed but cannot be found; open a new terminal.")
    info(f"installed {uv}")
    return uv


def install_python(uv: Path) -> None:
    step(f"Installing Python {PROJECT_PYTHON} (managed by uv)")
    run([uv, "python", "install", PROJECT_PYTHON])


def sync_dev_environment(uv: Path) -> None:
    step("Creating the development environment from uv.lock")
    run([uv, "sync", "--frozen"])


def install_cli(uv: Path) -> None:
    step(f"Installing the '{CLI_NAME}' command")
    with tempfile.TemporaryDirectory() as tmp:
        constraints = Path(tmp) / "constraints.txt"
        # `uv tool install` does not read uv.lock; pin the locked versions explicitly.
        run(
            [
                uv,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--no-hashes",
                "--quiet",
                "--output-file",
                constraints,
            ]
        )
        run(
            [
                uv,
                "tool",
                "install",
                "--python",
                PROJECT_PYTHON,
                "--editable",
                REPO_ROOT,
                "--constraints",
                constraints,
                "--reinstall",
            ]
        )


def ensure_tool_dir_on_path(uv: Path) -> Path:
    step("Checking PATH")
    tool_bin = Path(run([uv, "tool", "dir", "--bin"], capture=True))
    if is_on_path(tool_bin):
        info(f"{tool_bin} is on PATH")
        return tool_bin
    run([uv, "tool", "update-shell"])
    info(f"Added {tool_bin} to PATH. Open a NEW terminal to use '{CLI_NAME}' by name.")
    return tool_bin


def verify(tool_bin: Path) -> None:
    step("Verifying the installation")
    cli = tool_bin / executable_name(CLI_NAME)
    if not cli.is_file():
        raise BootstrapError(f"{cli} not found after installation.")
    run([cli, "version"])
    print()
    doctor = subprocess.run([str(cli), "doctor"], cwd=REPO_ROOT, check=False)
    if doctor.returncode != 0:
        info("The CLI is installed, but 'doctor' reported problems (see above).")
        info("Docker Engine/Desktop with Compose V2 is needed to run the stack.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Install the {CLI_NAME} command.")
    parser.add_argument(
        "-y", "--yes", action="store_true", help="answer yes to prompts (non-interactive)"
    )
    args = parser.parse_args(argv)

    try:
        check_python()
        print(f"Installing {CLI_NAME} from {REPO_ROOT}")
        uv = ensure_uv(args.yes)
        install_python(uv)
        sync_dev_environment(uv)
        install_cli(uv)
        tool_bin = ensure_tool_dir_on_path(uv)
        verify(tool_bin)
    except BootstrapError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130

    print(f"\nDone. Run '{CLI_NAME} --help' to get started.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
