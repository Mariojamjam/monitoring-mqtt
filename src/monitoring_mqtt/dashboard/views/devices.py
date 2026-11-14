"""One device: its values over time against the normal band, thresholds and alerts."""

from __future__ import annotations

from datetime import UTC, datetime

import streamlit as st

from monitoring_mqtt.dashboard import charts, context, data
from monitoring_mqtt.dashboard.client import ApiUnavailableError

MAX_ROWS = 2000


def _choose_device() -> str | None:
    api, f = context.api(), context.filters()
    try:
        devices = api.devices(domain=f.domain, run_id=f.run_id)
    except ApiUnavailableError as exc:
        context.unavailable(exc)
        return None
    if not devices:
        st.info("No device stored for these filters yet.")
        return None
    ids = [d["device_id"] for d in devices]
    return st.selectbox("Device", ids, key="device")


def body(device_id: str) -> None:
    api, f = context.api(), context.filters()
    now = datetime.now(UTC)
    device = api.device(device_id=device_id)
    if device is None:
        st.info(f"{device_id} has no stored measurement.")
        return
    catalog = api.catalog()
    info = data.device_type_info(catalog, device["device_type"])
    unit = device["unit"] or ""

    cols = st.columns(5)
    normal = info["normal"] if info else None
    state = data.value_state(device["last_value"], normal)
    cols[0].metric(
        "Latest value",
        f"{device['last_value']:g} {unit}".strip(),
        border=True,
        delta=None if state in ("normal", "—") else state,
        delta_color="off",
        delta_arrow="off",
    )
    cols[1].metric("Measurements", f"{device['measurements']:,}", border=True)
    cols[2].metric("Open alerts", len(device["open_alerts"]), border=True)
    cols[3].metric("Worker", device["worker_id"], border=True)
    cols[4].metric("Site", device["site_id"], border=True)

    window = f.time_window(now)
    series = api.series(
        device_id=device_id, run_id=f.run_id, bucket=window.bucket, since=window.since
    )
    frame = data.series_frame(series)
    alerts = data.alerts_frame(
        api.alerts(device_id=device_id, run_id=f.run_id, since=window.since), now
    )
    if frame.empty:
        st.caption("No measurements in this window.")
    else:
        st.caption(
            f"{info['metric'] if info else device['metric']} per {window.bucket}: "
            "average, with the minimum and maximum of each bucket. Grey: normal band; "
            "dashed: alert thresholds; shaded: periods with an alert."
        )
        chart = charts.series_chart(
            frame,
            unit=device["unit"],
            normal=normal,
            rules=data.rules_for(catalog, device["device_type"]),
            alerts=alerts,
        )
        st.altair_chart(chart, width="stretch")

    st.subheader("Measurements")
    rows = data.measurements_frame(
        api.measurements(
            device_id=device_id, run_id=f.run_id, since=window.since, max_items=MAX_ROWS
        )
    )
    st.caption(
        "Newest first, with the three times: generated on the device, received by the "
        "ingestion consumer, stored by the database."
    )
    st.dataframe(
        rows,
        hide_index=True,
        column_order=[
            "generated_at",
            "value",
            "sequence",
            "mqtt_ms",
            "storage_ms",
            "received_at",
            "stored_at",
            "run",
        ],
        column_config={
            "generated_at": context.datetime_column("generated"),
            "received_at": context.datetime_column("received"),
            "stored_at": context.datetime_column("stored"),
            "mqtt_ms": st.column_config.NumberColumn("device → ingestion", format="%.0f ms"),
            "storage_ms": st.column_config.NumberColumn("ingestion → database", format="%.0f ms"),
        },
    )
    st.download_button(
        "Download CSV", rows.to_csv(index=False), file_name=f"{device_id}.csv", mime="text/csv"
    )


def render() -> None:
    st.title("Devices")
    device_id = _choose_device()
    if device_id is not None:
        context.live(lambda: body(device_id))
