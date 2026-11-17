"""Streamlit script for AppTest: the sidebar, then the page named in the session state
(``page``). The API comes from the session state too (``api``) when a test sets one."""

import streamlit as st

from monitoring_mqtt.dashboard import context
from monitoring_mqtt.dashboard.views import alerts, devices, overview, runs

PAGES = {"overview": overview, "devices": devices, "alerts": alerts, "runs": runs}

st.session_state["filters"] = context.sidebar()
PAGES[st.session_state.get("page", "overview")].render()
