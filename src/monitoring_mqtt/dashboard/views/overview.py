"""Overview: how the system is doing now, per domain."""

from __future__ import annotations

from datetime import UTC, datetime

import streamlit as st

from monitoring_mqtt.dashboard import charts, context, data


def _ago(at: datetime | None, now: datetime) -> str:
    if at is None:
        return "—"
    seconds = max(0.0, (now - at).total_seconds())
    return f"{seconds:.0f} s ago" if seconds < 120 else f"{seconds / 60:.0f} min ago"


def body() -> None:
    api, f = context.api(), context.filters()
    now = datetime.now(UTC)
    stats = api.stats()
    run = api.run(run_id=f.run_id) if f.run_id else None
    catalog = api.catalog()

    open_alerts = data.alerts_frame(
        api.alerts(state="active", domain=f.domain, run_id=f.run_id), now
    )
    critical = int((open_alerts["severity"] == "critical").sum())
    cols = st.columns(5)
    cols[0].metric(
        "Measurements stored",
        f"{(run or stats)['measurements']:,}",
        border=True,
        help="In the selected run" if run else "In every run",
    )
    cols[1].metric("Devices", (run or stats)["devices"], border=True)
    cols[2].metric(
        "Open alerts",
        len(open_alerts),
        border=True,
        delta=f"{critical} critical" if critical else None,
        delta_color="inverse",
        delta_arrow="off",
    )
    delay = stats["generated_to_stored_ms"]
    cols[3].metric(
        "Device → database",
        "—" if delay is None else f"{delay / 1000:.2f} s",
        border=True,
        help="Average over the latest 1000 measurements",
    )
    cols[4].metric("Last stored", _ago(data.parse_time(stats["last_stored_at"]), now), border=True)

    window = f.time_window(now)
    shown = [f.domain] if f.domain else data.domains(catalog)
    st.subheader("Domains")
    st.caption(
        f"Average of every device of the domain per {window.bucket}, with its minimum "
        "and maximum; grey: the normal band."
    )
    for col, domain in zip(st.columns(len(shown)), shown, strict=True):
        info = data.domain_type(catalog, domain)
        series = api.series(
            domain=domain, run_id=f.run_id, bucket=window.bucket, since=window.since
        )
        frame = data.series_frame(series)
        with col:
            st.markdown(f"**{domain}** · {info['metric'] if info else ''}")
            if frame.empty:
                st.caption("No measurements in this window.")
                continue
            chart = charts.series_chart(
                frame,
                unit=info["unit"] if info else None,
                normal=info["normal"] if info else None,
                height=200,
            )
            st.altair_chart(chart, width="stretch")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Devices")
        devices = api.devices(domain=f.domain, run_id=f.run_id)
        frame = data.devices_frame(devices, catalog, now)
        st.dataframe(
            frame,
            hide_index=True,
            column_order=[
                "device",
                "value",
                "unit",
                "state",
                "seen_s_ago",
                "open_alerts",
                "worker",
                "site",
            ],
            column_config={
                "seen_s_ago": st.column_config.NumberColumn("seen", format="%.0f s ago"),
                "open_alerts": st.column_config.NumberColumn("open alerts"),
                "state": st.column_config.TextColumn(help="Against the normal band"),
            },
        )
    with right:
        st.subheader("Open alerts")
        if open_alerts.empty:
            st.caption("No open alert.")
        else:
            st.dataframe(
                open_alerts,
                hide_index=True,
                column_order=["device", "rule", "severity", "raised_value", "duration_s"],
                column_config={
                    "duration_s": st.column_config.NumberColumn("open for", format="%.0f s")
                },
            )


def render() -> None:
    st.title("Overview")
    context.live(body)
