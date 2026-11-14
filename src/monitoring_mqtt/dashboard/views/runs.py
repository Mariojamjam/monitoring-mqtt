"""Runs: what each `stack up` produced, and two runs side by side."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from monitoring_mqtt.dashboard import context, data


def body() -> None:
    api = context.api()
    runs = api.runs()
    if not runs:
        st.info("No run stored yet.")
        return
    table = pd.DataFrame(runs)
    for column in ("first_seen_at", "last_seen_at"):
        table[column] = pd.to_datetime(table[column])
    st.dataframe(
        table,
        hide_index=True,
        column_order=[
            "run_id",
            "first_seen_at",
            "last_seen_at",
            "devices",
            "measurements",
            "alerts",
        ],
        column_config={
            "first_seen_at": context.datetime_column("first measurement"),
            "last_seen_at": context.datetime_column("last measurement"),
        },
    )

    st.subheader("Compare two runs")
    ids = [r["run_id"] for r in runs]
    cols = st.columns(2)
    a = cols[0].selectbox("Run A", ids, index=min(1, len(ids) - 1), key="run_a")
    b = cols[1].selectbox("Run B", ids, index=0, key="run_b")
    first, second = api.run(run_id=a), api.run(run_id=b)
    if first is None or second is None:
        st.info("Run not found.")
        return
    st.dataframe(
        data.compare_runs(first, second),
        hide_index=True,
        column_config={
            "A": st.column_config.NumberColumn(a, format="%g"),
            "B": st.column_config.NumberColumn(b, format="%g"),
            "difference": st.column_config.NumberColumn("B - A", format="%+g"),
        },
    )
    st.caption(
        "Delays: from the device generating a measurement to the ingestion consumer "
        "receiving it (MQTT) and to the database storing it. Sequence gaps: "
        "measurements missing between each device's lowest and highest stored "
        "sequence number."
    )


def render() -> None:
    st.title("Runs")
    context.live(body)
