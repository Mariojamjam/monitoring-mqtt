from pathlib import Path

import pytest

from monitoring_mqtt.cli import project
from monitoring_mqtt.cli.project import (
    ENV_VAR,
    ProjectNotFoundError,
    is_project_root,
    resolve_project_root,
)


def make_checkout(path: Path, name: str = "monitoring-mqtt") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setattr(project, "_install_location", lambda: None)


def test_is_project_root_checks_project_name(tmp_path: Path) -> None:
    assert is_project_root(make_checkout(tmp_path / "ok"))
    assert not is_project_root(make_checkout(tmp_path / "other", name="other"))
    assert not is_project_root(tmp_path / "missing")


def test_explicit_option_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = make_checkout(tmp_path / "explicit")
    monkeypatch.setenv(ENV_VAR, str(make_checkout(tmp_path / "env")))
    root = resolve_project_root(explicit)
    assert (root.path, root.source) == (explicit.resolve(), "option")


def test_invalid_explicit_option_fails(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFoundError):
        resolve_project_root(tmp_path)


def test_env_var_used_before_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_dir = make_checkout(tmp_path / "env")
    monkeypatch.setenv(ENV_VAR, str(env_dir))
    monkeypatch.setattr(project, "_install_location", lambda: tmp_path / "install")
    assert resolve_project_root().source == "env"


def test_install_location_used_before_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    installed = make_checkout(tmp_path / "install")
    monkeypatch.setattr(project, "_install_location", lambda: installed)
    root = resolve_project_root(cwd=make_checkout(tmp_path / "cwd"))
    assert (root.path, root.source) == (installed, "install")


def test_upward_search_from_subdirectory(tmp_path: Path) -> None:
    checkout = make_checkout(tmp_path / "repo")
    nested = checkout / "a" / "b"
    nested.mkdir(parents=True)
    root = resolve_project_root(cwd=nested)
    assert (root.path, root.source) == (checkout.resolve(), "cwd")


def test_not_found_raises(tmp_path: Path) -> None:
    with pytest.raises(ProjectNotFoundError):
        resolve_project_root(cwd=tmp_path)
