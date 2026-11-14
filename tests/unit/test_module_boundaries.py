"""Architectural rules: which packages may import which (checked on the source)."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "monitoring_mqtt"

# package → project packages it may import (besides itself)
ALLOWED = {
    "core": set(),
    "storage": {"core"},
    "api": {"core", "storage", "simulator"},  # the catalog route reads the device catalog
    "simulator": {"core"},
    "consumers": {"core"},
    "tools": {"core", "simulator"},  # control tool reads the device catalog
    "tui": {"core", "simulator"},  # the UI reads the device catalog (normal bands)
    "testing": {"core"},
    "dashboard": {"core"},  # reads the API over HTTP: never storage, MQTT consumers or api
}


def imported_packages(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return {
        name.split(".")[1]
        for name in names
        if name.startswith("monitoring_mqtt.") and len(name.split(".")) > 1
    }


@pytest.mark.parametrize("package", sorted(ALLOWED))
def test_package_imports_only_what_it_may(package: str) -> None:
    allowed = ALLOWED[package] | {package}
    violations = {
        str(path.relative_to(PACKAGE)): sorted(imported_packages(path) - allowed)
        for path in (PACKAGE / package).rglob("*.py")
        if imported_packages(path) - allowed
    }
    assert violations == {}


def test_only_the_api_and_migrations_reach_the_database() -> None:
    users = {
        path.relative_to(PACKAGE).parts[0]
        for path in PACKAGE.rglob("*.py")
        if "storage" in imported_packages(path)
    }
    assert users <= {"storage", "api"}
