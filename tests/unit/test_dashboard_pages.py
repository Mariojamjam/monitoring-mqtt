"""Every page, run by Streamlit's AppTest (no browser) against a fake API."""

from pathlib import Path

import pytest
import streamlit as st
from dashboard_fakes import FakeApi
from streamlit.testing.v1 import AppTest

from monitoring_mqtt.dashboard import context

SCRIPT = str(Path(__file__).with_name("dashboard_page.py"))


def run_page(page: str, api: FakeApi, **state) -> AppTest:
    at = AppTest.from_file(SCRIPT, default_timeout=30)
    at.session_state["page"] = page
    at.session_state[context.API_KEY] = api
    for key, value in state.items():
        at.session_state[key] = value
    return at.run()


@pytest.mark.parametrize("page", ["overview", "devices", "alerts", "runs"])
def test_page_renders_without_errors(page: str) -> None:
    at = run_page(page, FakeApi())
    assert not at.exception, at.exception
    assert not at.warning
    assert at.title[0].value == page.capitalize()


def test_overview_numbers() -> None:
    at = run_page("overview", FakeApi())
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Measurements stored"] == "1,000"  # of the selected (latest) run
    assert metrics["Open alerts"] == "1"
    assert metrics["Device → database"] == "1.01 s"
    assert at.dataframe[0].value["state"].tolist() == ["high", "normal"]


def test_sidebar_filters_reach_the_queries() -> None:
    api = FakeApi()
    at = run_page("overview", api)
    assert api.last("devices")["run_id"] == "run-1"  # the newest run by default
    assert at.sidebar.selectbox(key="run").options == ["Current (run-1)", "run-0", "All"]
    at.sidebar.selectbox(key="domain").select("cattle").run()
    at.sidebar.selectbox(key="run").select("All").run()
    assert not at.exception
    assert api.last("devices") == {"domain": "cattle", "run_id": None}
    assert api.last("series")["domain"] == "cattle"


def test_device_page_and_csv() -> None:
    at = run_page("devices", FakeApi())
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Latest value"] == "31 ppm"  # the first device of the list
    assert len(at.dataframe[0].value) == 9


def test_alert_filters() -> None:
    api = FakeApi()
    at = run_page("alerts", api)
    assert {m.label: m.value for m in at.metric}["Open now"] == "1"
    at.selectbox(key="alert_severity").select("critical").run()
    assert api.last("alerts")["severity"] == "critical"
    assert {m.label: m.value for m in at.metric}["Alerts"] == "1"


def test_runs_comparison() -> None:
    at = run_page("runs", FakeApi())
    comparison = at.dataframe[1].value.set_index("metric")
    assert comparison.loc["Missing measurements (sequence gaps)", "difference"] == -7


@pytest.mark.parametrize("page", ["overview", "devices", "alerts", "runs"])
def test_api_down_shows_a_warning_not_an_error(page: str) -> None:
    api = FakeApi()
    api.down = True
    at = run_page(page, api)
    assert not at.exception, at.exception
    assert at.sidebar.error[0].value == "API unavailable"
    assert at.warning or at.info or page == "devices"


def test_cached_api_keeps_the_last_answer_during_an_outage(monkeypatch) -> None:
    fake = FakeApi()
    monkeypatch.setattr(context, "_client", lambda base_url: fake)
    st.cache_data.clear()
    at = AppTest.from_file(SCRIPT, default_timeout=30)
    at.session_state["page"] = "runs"
    at.run()
    assert not at.warning and len(at.dataframe[0].value) == 2
    fake.down = True
    st.cache_data.clear()
    at.run()
    assert not at.exception
    assert "showing the last data received" in at.warning[0].value
    assert len(at.dataframe[0].value) == 2  # still there
    st.cache_data.clear()
