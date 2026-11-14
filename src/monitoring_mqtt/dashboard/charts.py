"""Altair charts (they come with Streamlit; no extra dependency).

One measured series per chart, in one hue: the average as a line and the minimum-maximum
of each bucket as a band around it. Context is drawn in neutral grey (the normal band) or
in the status colours of the alert severities (thresholds, periods with an open alert),
always with a text label or tooltip, never colour alone.
"""

from __future__ import annotations

from typing import Any

import altair as alt
import pandas as pd

SERIES = "#2a78d6"  # categorical slot 1 (blue)
NEUTRAL = "#8a8985"
SEVERITY_ORDER = ["warning", "critical"]
SEVERITY_COLORS = ["#fab219", "#d03b3b"]  # status palette: warning, critical
HEIGHT = 260
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _utc_clock(frame: pd.DataFrame, *columns: str) -> pd.DataFrame:
    """Times as UTC wall-clock values: browsers would otherwise show them in their own
    time zone, while the tables, the API and the logs use UTC."""
    frame = frame.copy()
    for column in columns:
        if column in frame:
            frame[column] = pd.to_datetime(frame[column], utc=True).dt.tz_localize(None)
    return frame


def _severity(legend: bool = True) -> alt.Color:
    return alt.Color(
        "severity:N",
        scale=alt.Scale(domain=SEVERITY_ORDER, range=SEVERITY_COLORS),
        legend=alt.Legend(title="Severity", orient="bottom") if legend else None,
    )


def series_chart(
    frame: pd.DataFrame,
    *,
    unit: str | None,
    normal: dict[str, float] | None = None,
    rules: list[dict[str, Any]] | None = None,
    alerts: pd.DataFrame | None = None,
    height: int = HEIGHT,
) -> alt.LayerChart:
    """Average line with min-max band, normal band, thresholds and alert periods."""
    label = unit or "value"
    frame = _utc_clock(frame, "time")
    if alerts is not None:
        alerts = _utc_clock(alerts, "start", "end")
    x = alt.X("time:T", title="UTC", axis=alt.Axis(grid=False, labelOverlap=True, tickCount=8))
    # padding: room above and below, so the normal band's edges stay visible
    y = alt.Y("avg:Q", title=label, scale=alt.Scale(zero=False, padding=12))
    layers: list[alt.Chart] = []

    if normal is not None:  # behind everything: a full-width neutral band
        band = pd.DataFrame([{**normal, "what": "normal band"}])
        layers.append(
            alt.Chart(band)
            .mark_rect(color=NEUTRAL, opacity=0.14)
            .encode(
                y="minimum:Q",
                y2="maximum:Q",
                tooltip=[alt.Tooltip("what:N", title=" "), "minimum:Q", "maximum:Q"],
            )
        )
    if alerts is not None and not alerts.empty:
        layers.append(
            alt.Chart(alerts)
            .mark_rect(opacity=0.18, clip=True)
            .encode(
                x="start:T",
                x2="end:T",
                color=_severity(),
                tooltip=[
                    "rule:N",
                    "severity:N",
                    "state:N",
                    alt.Tooltip("start:T", format=TIME_FORMAT),
                    alt.Tooltip("end:T", format=TIME_FORMAT),
                    "duration_s:Q",
                ],
            )
        )
    if rules:
        thresholds = pd.DataFrame(rules)
        rule_base = alt.Chart(thresholds).encode(y="threshold:Q")
        layers.append(
            rule_base.mark_rule(strokeDash=[5, 4], strokeWidth=1.5).encode(
                color=_severity(legend=alerts is None or alerts.empty),
                tooltip=["id:N", "severity:N", "condition:N", "threshold:Q", "clear_at:Q"],
            )
        )
        layers.append(
            rule_base.mark_text(
                align="left", baseline="bottom", dx=4, dy=-2, fontSize=11, color=NEUTRAL
            ).encode(x=alt.value(0), text="id:N")
        )

    base = alt.Chart(frame).encode(x=x)
    layers.append(
        base.mark_area(color=SERIES, opacity=0.22).encode(y=alt.Y("min:Q", title=label), y2="max:Q")
    )
    layers.append(base.mark_line(color=SERIES, strokeWidth=2).encode(y=y))

    # Hover: a crosshair on the nearest bucket with its numbers.
    hover = alt.selection_point(
        fields=["time"], nearest=True, on="pointerover", empty=False, clear="pointerout"
    )
    tooltip = [
        alt.Tooltip("time:T", title="time (UTC)", format=TIME_FORMAT),
        alt.Tooltip("avg:Q", format=".2f"),
        alt.Tooltip("min:Q", format=".2f"),
        alt.Tooltip("max:Q", format=".2f"),
        alt.Tooltip("count:Q", title="measurements"),
    ]
    layers.append(
        base.mark_rule(color=NEUTRAL, strokeWidth=1)
        .encode(opacity=alt.condition(hover, alt.value(0.8), alt.value(0)), tooltip=tooltip)
        .add_params(hover)
    )
    layers.append(
        base.mark_point(color=SERIES, filled=True, size=70).encode(
            y=y, opacity=alt.condition(hover, alt.value(1), alt.value(0))
        )
    )
    return alt.layer(*layers).properties(height=height)


def alerts_timeline(alerts: pd.DataFrame, *, height_per_row: int = 30) -> alt.Chart:
    """One bar per alert, from raised to cleared (an open alert reaches now), one row per
    device and rule."""
    frame = _utc_clock(alerts, "start", "end")
    frame = frame.assign(track=frame["device"] + " · " + frame["rule"])
    rows = max(1, frame["track"].nunique())
    return (
        alt.Chart(frame)
        .mark_bar(cornerRadius=3, height={"band": 0.7})
        .encode(
            x=alt.X("start:T", title="UTC", axis=alt.Axis(tickCount=8)),
            x2="end:T",
            y=alt.Y("track:N", title=None, sort="ascending", axis=alt.Axis(labelLimit=320)),
            color=_severity(),
            tooltip=[
                "device:N",
                "rule:N",
                "severity:N",
                "state:N",
                alt.Tooltip("start:T", format=TIME_FORMAT),
                alt.Tooltip("end:T", format=TIME_FORMAT),
                alt.Tooltip("duration_s:Q", title="duration (s)"),
                "raised_value:Q",
                "cleared_value:Q",
            ],
        )
        .properties(height=min(900, 40 + rows * height_per_row))
    )
