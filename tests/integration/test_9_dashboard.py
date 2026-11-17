"""Dashboard: served by its container, every page renders from the real API, and it is
not in anyone's data path."""

import time
from pathlib import Path

import httpx
import pytest
from harness import Observer, Stack
from streamlit.testing.v1 import AppTest

PAGE_SCRIPT = str(Path(__file__).parents[1] / "unit" / "dashboard_page.py")


def test_dashboard_container_is_healthy(stack: Stack) -> None:
    assert httpx.get(f"{stack.dashboard_url}/_stcore/health", timeout=5).status_code == 200
    assert "<!doctype html>" in httpx.get(stack.dashboard_url, timeout=5).text.lower()


@pytest.mark.parametrize("page", ["overview", "devices", "alerts", "runs"])
def test_pages_render_from_the_real_api(stack: Stack, monkeypatch, page: str) -> None:
    # The same scripts as the container, run in this process against the test stack's API.
    monkeypatch.setenv("API_URL", stack.api_url)
    at = AppTest.from_file(PAGE_SCRIPT, default_timeout=60)
    at.session_state["page"] = page
    at.run()
    assert not at.exception, at.exception
    runs = [r["run_id"] for r in stack.api("/api/runs")]  # type: ignore[index]
    assert at.sidebar.selectbox(key="run").value == runs[0]  # the newest run by default
    # The suite's run (other tests start stray workers, whose runs may be newer).
    at.sidebar.selectbox(key="run").select("it-run").run()
    assert not at.exception, at.exception
    assert not at.warning, [w.value for w in at.warning]  # the API answered
    metrics = {m.label: m.value for m in at.metric}
    if page == "overview":
        assert int(metrics["Measurements stored"].replace(",", "")) > 0
        assert int(metrics["Devices"]) >= 5  # tests may add devices (e.g. a taken worker id)
    if page == "alerts":  # as many as the API has for the run (none if run alone)
        stored = stack.api("/api/alerts", run_id="it-run", limit=1000)["items"]  # type: ignore[index]
        if stored:
            assert int(metrics["Alerts"]) >= len(stored)
        else:
            assert at.info


def test_stopping_the_dashboard_affects_nothing_else(stack: Stack, observer: Observer) -> None:
    container = stack.container("dashboard")
    before = stack.api("/api/stats")["measurements"]  # type: ignore[index]
    assert stack.docker("stop", container).returncode == 0
    try:
        since = time.time()
        observer.wait_until(
            lambda: all(observer.samples(d, since) for d in observer.active()),
            "every device still delivered to the broker", 15,
        )  # fmt: skip
        observer.wait_until(
            lambda: stack.api("/api/stats")["measurements"] > before,  # type: ignore[index]
            "the history still growing", 15,
        )  # fmt: skip
    finally:
        stack.docker("start", container)
    observer.wait_until(lambda: _healthy(stack), "dashboard healthy again after start", 60)


def _healthy(stack: Stack) -> bool:
    try:
        return httpx.get(f"{stack.dashboard_url}/_stcore/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False
