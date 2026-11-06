"""Tests for the host bootstrap script (pure logic only; nothing is installed)."""

import importlib.util
import os
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bootstrap.py"
spec = importlib.util.spec_from_file_location("bootstrap", SCRIPT)
assert spec and spec.loader
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def test_is_on_path_normalizes_entries(tmp_path: Path) -> None:
    path_value = os.pathsep.join(["/usr/bin", f"{tmp_path}{os.sep}", ""])
    assert bootstrap.is_on_path(tmp_path, path_value)
    assert not bootstrap.is_on_path(tmp_path / "other", path_value)


def test_uv_candidates_follow_official_installer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("XDG_BIN_HOME", raising=False)
    candidates = bootstrap.uv_candidates(tmp_path)
    assert candidates[0].parent == tmp_path / ".local" / "bin"
    monkeypatch.setenv("XDG_BIN_HOME", str(tmp_path / "xdg"))
    assert bootstrap.uv_candidates(tmp_path)[0].parent == tmp_path / "xdg"


def test_find_uv_falls_back_to_install_location(tmp_path: Path, monkeypatch) -> None:
    uv = tmp_path / ".local" / "bin" / bootstrap.executable_name("uv")
    uv.parent.mkdir(parents=True)
    uv.write_text("")
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    monkeypatch.setattr(bootstrap.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_BIN_HOME", raising=False)
    assert bootstrap.find_uv() == uv


def test_confirm_never_blocks_without_tty(monkeypatch) -> None:
    assert bootstrap.confirm("?", assume_yes=True)
    monkeypatch.setattr(bootstrap.sys.stdin, "isatty", lambda: False)
    assert not bootstrap.confirm("?", assume_yes=False)


def test_declined_uv_install_fails(monkeypatch) -> None:
    monkeypatch.setattr(bootstrap, "find_uv", lambda: None)
    monkeypatch.setattr(bootstrap, "confirm", lambda question, assume_yes: False)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.ensure_uv(assume_yes=False)


def test_failed_command_raises() -> None:
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.run([bootstrap.sys.executable, "-c", "raise SystemExit(3)"])


def test_download_errors_become_bootstrap_errors(tmp_path: Path, monkeypatch) -> None:
    def fail(request, timeout):
        raise bootstrap.urllib.error.URLError("offline")

    monkeypatch.setattr(bootstrap.urllib.request, "urlopen", fail)
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.download("https://example.invalid/x", tmp_path / "x")
