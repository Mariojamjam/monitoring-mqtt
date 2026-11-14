"""Alerts: when they opened and closed, per rule and device."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

import streamlit as st

from monitoring_mqtt.dashboard import charts, context, data
from monitoring_mqtt.dashboard.client import ApiUnavailableError

STATES = {"Open and cleared": None, "Open": "active", "Cleared": "cleared"}
SEVERITIES = ["warning", "critical"]
MAX_ALERTS = 5000


def body(state: str | None, severity: str | None, rule: str | None) -> None:
    api, f = context.api(), context.filters()
    now = datetime.now(UTC)
    window = f.time_window(now)
    alerts = data.alerts_frame(
        api.alerts(
            state=state,
            severity=severity,
            rule_id=rule,
            domain=f.domain,
            run_id=f.run_id,
            since=window.since,
            max_items=MAX_ALERTS,
        ),
        now,
    )
    if alerts.empty:
        st.info(
            "No alert for these filters. Alerts are raised when a device leaves its normal "
            "band: try `monitoring-mqtt device anomaly <device_id>`."
        )
        return
    opened = int(alerts["open"].sum())
    cols = st.columns(4)
    cols[0].metric("Alerts", len(alerts), border=True)
    cols[1].metric("Open now", opened, border=True)
    cols[2].metric("Critical", int((alerts["severity"] == "critical").sum()), border=True)
    cleared = alerts.loc[~alerts["open"], "duration_s"]
    cols[3].metric(
        "Mean duration",
        f"{cleared.mean():.0f} s" if len(cleared) else "—",
        border=True,
        help="Of the cleared alerts",
    )

    st.subheader("Timeline")
    st.caption("Each bar is one alert, from raised to cleared; an open alert reaches now.")
    st.altair_chart(charts.alerts_timeline(alerts), width="stretch")

    st.subheader("Per rule")
    st.dataframe(
        data.rule_summary(alerts),
        hide_index=True,
        column_config={
            "mean_duration_s": st.column_config.NumberColumn("mean duration", format="%.0f s"),
            "max_duration_s": st.column_config.NumberColumn("max duration", format="%.0f s"),
        },
    )

    st.subheader("Alerts")
    st.dataframe(
        alerts.sort_values("start", ascending=False),
        hide_index=True,
        column_order=[
            "start",
            "device",
            "rule",
            "severity",
            "state",
            "condition",
            "threshold",
            "raised_value",
            "cleared_value",
            "duration_s",
        ],
        column_config={
            "start": context.datetime_column("raised at"),
            "duration_s": st.column_config.NumberColumn("duration", format="%.0f s"),
        },
    )
    st.download_button(
        "Download CSV", alerts.to_csv(index=False), file_name="alerts.csv", mime="text/csv"
    )


def render() -> None:
    st.title("Alerts")
    cols = st.columns(3)
    state = cols[0].selectbox("State", list(STATES), key="alert_state")
    severity = cols[1].selectbox("Severity", [context.ALL, *SEVERITIES], key="alert_severity")
    rules = [context.ALL]
    with contextlib.suppress(ApiUnavailableError):
        rules += [r["id"] for r in context.api().catalog()["alert_rules"]]
    rule = cols[2].selectbox("Rule", rules, key="alert_rule")
    context.live(
        lambda: body(
            STATES[state],
            None if severity == context.ALL else severity,
            None if rule == context.ALL else rule,
        )
    )
