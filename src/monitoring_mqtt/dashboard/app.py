"""Dashboard entry script (``streamlit run``): navigation, sidebar, the selected page."""

from __future__ import annotations

import streamlit as st

from monitoring_mqtt.dashboard import context
from monitoring_mqtt.dashboard.views import alerts, devices, overview, runs

PAGES = [
    st.Page(overview.render, title="Overview", url_path="overview", default=True),
    st.Page(devices.render, title="Devices", url_path="devices"),
    st.Page(alerts.render, title="Alerts", url_path="alerts"),
    st.Page(runs.render, title="Runs", url_path="runs"),
]


def main() -> None:
    st.set_page_config(page_title="Monitoring MQTT", layout="wide")
    page = st.navigation(PAGES)
    st.session_state["filters"] = context.sidebar()
    page.run()


if __name__ == "__main__":
    main()
