"""Resolution of the project root directory used by stack and test commands.

Order of precedence:
1. explicit ``--project-dir`` option;
2. ``MONITORING_MQTT_PROJECT_DIR`` environment variable;
3. the checkout the package is installed from (editable install);
4. upward search starting at the current working directory.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import monitoring_mqtt

PROJECT_NAME = "monitoring-mqtt"
ENV_VAR = "MONITORING_MQTT_PROJECT_DIR"


class ProjectNotFoundError(Exception):
    """Raised when no valid project root can be resolved."""


@dataclass(frozen=True)
class ProjectRoot:
    path: Path
    source: str  # how it was resolved: option, env, install, cwd


def is_project_root(path: Path) -> bool:
    """Return True if ``path`` contains this project's ``pyproject.toml``."""
    pyproject = path / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return data.get("project", {}).get("name") == PROJECT_NAME


def _install_location() -> Path | None:
    # src/monitoring_mqtt/__init__.py -> repository root is two levels above the package.
    package_dir = Path(monitoring_mqtt.__file__).resolve().parent
    candidate = package_dir.parent.parent
    return candidate if is_project_root(candidate) else None


def _search_upwards(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if is_project_root(candidate):
            return candidate
    return None


def resolve_project_root(explicit: Path | None = None, cwd: Path | None = None) -> ProjectRoot:
    """Resolve the project root or raise ``ProjectNotFoundError`` with a fix hint."""
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if not is_project_root(path):
            raise ProjectNotFoundError(f"--project-dir {path} is not a {PROJECT_NAME} checkout.")
        return ProjectRoot(path, "option")

    env_value = os.environ.get(ENV_VAR)
    if env_value:
        path = Path(env_value).expanduser().resolve()
        if not is_project_root(path):
            raise ProjectNotFoundError(f"{ENV_VAR}={path} is not a {PROJECT_NAME} checkout.")
        return ProjectRoot(path, "env")

    installed = _install_location()
    if installed is not None:
        return ProjectRoot(installed, "install")

    found = _search_upwards((cwd or Path.cwd()).resolve())
    if found is not None:
        return ProjectRoot(found, "cwd")

    raise ProjectNotFoundError(
        f"Could not locate the {PROJECT_NAME} project directory. "
        f"Run the command inside the repository, pass --project-dir PATH, "
        f"or set {ENV_VAR}=PATH."
    )
