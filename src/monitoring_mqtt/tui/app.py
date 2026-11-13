"""Textual application: a read-only view of publishers, broker and subscribers."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from functools import partial
from typing import ClassVar, Protocol

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Input, Static, TabbedContent, TabPane

from monitoring_mqtt.core.status import MonitorState
from monitoring_mqtt.tui import render
from monitoring_mqtt.tui.state import Event, EventKind, FeedItem, SystemState, now_epoch

REFRESH_SECONDS = 0.5
EVENT_ROWS = 200
BROKER_HISTORY = 120  # samples (one per refresh) shown in the broker tab

TAB_TABLES = {"devices": "device-table", "monitors": "monitor-table", "events": "event-table"}
EVENT_STYLE = {
    EventKind.INVALID: "bold red",
    EventKind.GAP: "yellow",
    EventKind.RESTART: "yellow",
    EventKind.DUPLICATE: "yellow",
    EventKind.WORKER_LEFT: "red",
    EventKind.MONITOR_OFFLINE: "red",
    EventKind.MONITOR_STOPPED: "bright_black",
    EventKind.WORKER_JOINED: "green",
    EventKind.MONITOR_ONLINE: "green",
    EventKind.BROKER: "bold blue",
    EventKind.PROBE: "blue",
}


class FeedSource(Protocol):
    client_id: str

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def drain(self) -> list[FeedItem]: ...


class DetailScreen(ModalScreen[None]):
    """Read-only details of one row; Escape closes it."""

    BINDINGS: ClassVar = [Binding("escape", "dismiss", "Close"), Binding("q", "dismiss", "Close")]

    def __init__(self, title: str, body: RenderableType) -> None:
        super().__init__()
        self.title_text = title
        self.body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail") as box:
            box.border_title = self.title_text
            box.border_subtitle = "Esc to close"
            yield Static(self.body, id="detail-body")


def sync_rows(table: DataTable, rows: Sequence[tuple[str, tuple]]) -> None:
    """Update a table in place, keeping the selected row; rebuild only if rows changed."""
    keys = [key for key, _ in rows]
    current = [row_key.value for row_key in table.rows]
    cache: dict[str, tuple] = getattr(table, "_cells", {})
    if keys == current:
        columns = list(table.columns)
        for key, cells in rows:
            previous = cache.get(key)
            for index, value in enumerate(cells):
                if previous is None or previous[index] != value:
                    table.update_cell(key, columns[index], value)
    else:
        selected = current[table.cursor_row] if 0 <= table.cursor_row < len(current) else None
        table.clear()
        for key, cells in rows:
            table.add_row(*cells, key=key)
        if selected in keys:
            table.move_cursor(row=keys.index(selected))
    table._cells = dict(rows)  # type: ignore[attr-defined]


class MonitoringApp(App[None]):
    TITLE = "monitoring-mqtt"
    CSS = """
    #header { height: 1; background: $boost; }
    #panels { height: 1fr; }
    #pub-box { width: 3fr; border: round $primary; }
    #broker-box { width: 34; border: round $primary; padding: 0 1; }
    #sub-box { width: 2fr; border: round $primary; }
    #flow { height: auto; max-height: 14; border: round $primary; padding: 0 1; }
    #device-filter, #event-filter { dock: top; margin: 0 0 1 0; }
    DataTable { height: 1fr; }
    #monitor-table { height: auto; max-height: 40%; }
    #monitor-detail-box { height: 1fr; border: round $primary; padding: 0 1; }
    DetailScreen { align: center middle; }
    #detail { width: 90%; height: 85%; border: thick $primary; background: $surface;
              padding: 1 2; }
    """
    # The footer shows only the menu (P); every action is in it, with its shortcut.
    BINDINGS: ClassVar = [
        Binding("p", "command_palette", "Menu", key_display="P"),
        Binding("1", "tab('overview')", "Overview", show=False),
        Binding("2", "tab('devices')", "Devices", show=False),
        Binding("3", "tab('monitors')", "Monitors", show=False),
        Binding("4", "tab('events')", "Events", show=False),
        Binding("5", "tab('broker')", "Broker", show=False),
        Binding("slash", "filter", "Filter", show=False),
        Binding("space", "pause", "Pause events", show=False),
        Binding("t", "toggle_telemetry", "Telemetry in events", show=False),
        Binding("h", "toggle_inactive", "Hide inactive", show=False),
        Binding("a", "alerts", "Open alerts", show=False),
        Binding("escape", "leave_filter", "Back", show=False),
        Binding("q", "quit", "Quit", show=False),
    ]
    MENU: ClassVar = [
        (
            "Overview",
            "1",
            "Machines, workers, devices, broker, subscribers, flow",
            "tab('overview')",
        ),
        (
            "Devices",
            "2",
            "Every device: value, mode, alerts, sequence, consumers",
            "tab('devices')",
        ),
        ("Monitors", "3", "Subscribers and what each one received", "tab('monitors')"),
        ("Events", "4", "System changes, gaps, simulated episodes, alerts", "tab('events')"),
        ("Broker", "5", "Mosquitto statistics and publish rates", "tab('broker')"),
        ("Open alerts", "a", "Alerts raised and not cleared yet", "alerts"),
        ("Filter", "/", "Filter the Devices or Events table", "filter"),
        ("Pause events", "space", "Freeze or resume the Events table", "pause"),
        (
            "Telemetry in events",
            "t",
            "Include every telemetry message in Events",
            "toggle_telemetry",
        ),
        ("Hide inactive", "h", "Hide stopped monitors and departed workers", "toggle_inactive"),
        ("Quit", "q", "Leave the UI; the stack keeps running", "quit"),
    ]

    def __init__(
        self,
        feed: FeedSource,
        state: SystemState,
        broker_label: str,
        refresh_seconds: float = REFRESH_SECONDS,
        clock: Callable[[], float] = now_epoch,
    ) -> None:
        super().__init__()
        self.feed = feed
        self.state = state
        self.broker_label = broker_label
        self.refresh_seconds = refresh_seconds
        self.clock = clock
        self.paused = False
        self.include_telemetry = False
        self.show_inactive = True
        self.device_filter = ""
        self.event_filter = ""
        self.history: dict[str, deque[float]] = {
            name: deque(maxlen=BROKER_HISTORY) for name in ("observed", "in", "out")
        }
        self.selected_monitor: str | None = None
        self._event_snapshot: list[Event] = []
        self._events_drawn: tuple[int, str, bool] | None = None

    # Layout --------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with TabbedContent(initial="overview", id="tabs"):
            with TabPane("1 Overview", id="overview"):
                with Horizontal(id="panels"):
                    with VerticalScroll(id="pub-box"):
                        yield Static(id="publishers")
                    yield Static(id="broker-box")
                    with VerticalScroll(id="sub-box"):
                        yield Static(id="subscribers")
                yield Static(id="flow")
            with TabPane("2 Devices", id="devices"):
                yield Input(placeholder="/ filter devices (id, type, domain, machine)…",
                            id="device-filter")  # fmt: skip
                yield DataTable(id="device-table", cursor_type="row", zebra_stripes=True)
            with TabPane("3 Monitors", id="monitors"):
                yield DataTable(id="monitor-table", cursor_type="row", zebra_stripes=True)
                with VerticalScroll(id="monitor-detail-box"):
                    yield Static(id="monitor-detail")
            with TabPane("4 Events", id="events"):
                yield Input(placeholder="/ filter events (kind, device, text)…  p pauses",
                            id="event-filter")  # fmt: skip
                yield DataTable(id="event-table", cursor_type="row")
            with TabPane("5 Broker", id="broker"), VerticalScroll():
                yield Static(id="broker-body")
        yield Footer(show_command_palette=False)

    def on_mount(self) -> None:
        self.query_one("#pub-box").border_title = "Publishers"
        self.query_one("#broker-box").border_title = "Broker"
        self.query_one("#sub-box").border_title = "Subscribers"
        self.query_one("#flow").border_title = "Flow (last 60 s)"
        self.query_one("#monitor-detail-box").border_title = "Selected monitor"
        devices = self.query_one("#device-table", DataTable)
        for label in ("", "Device", "Type", "Domain", "Machine", "Worker", "Site", "Value",
                      "Mode", "Trend", "Seq", "Msg/s", "Missing", "Last seen",
                      "Consumed by"):  # fmt: skip
            devices.add_column(label, key=label or "state")
        monitors = self.query_one("#monitor-table", DataTable)
        for label in ("", "Client id", "Filter", "Machine", "Msg/s", "Received", "Invalid",
                      "Devices", "Latency avg", "Latency p95", "Up", "Updated"):  # fmt: skip
            monitors.add_column(label, key=label or "state")
        events = self.query_one("#event-table", DataTable)
        for label in ("Time (UTC)", "Kind", "Source", "Detail"):
            events.add_column(label, key=label)
        self.set_focus(None)  # keys go to the app bindings, not to a filter field
        self.feed.start()
        self.set_interval(self.refresh_seconds, self.tick)
        self.tick()

    def on_unmount(self) -> None:
        self.feed.stop()

    # Refresh -------------------------------------------------------------------------

    def tick(self) -> None:
        self.state.apply_all(self.feed.drain())
        now = self.clock()
        self.history["observed"].append(self.state.observed_rate(now))
        self.history["in"].append(self.state.broker.per_second("received") or 0.0)
        self.history["out"].append(self.state.broker.per_second("sent") or 0.0)
        try:
            self.refresh_view(now)
        except NoMatches:
            return  # the timer fired once more while the app shut down its widgets

    def refresh_view(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        self.query_one("#header", Static).update(render.header(self.state, self.broker_label, now))
        active = self.query_one("#tabs", TabbedContent).active
        getattr(self, f"_refresh_{active}")(now)

    def _refresh_overview(self, now: float) -> None:
        state = self.state
        self.query_one("#publishers", Static).update(
            render.publishers(state, now, self.show_inactive)
        )
        self.query_one("#broker-box", Static).update(
            render.broker_summary(state, self.broker_label, now)
        )
        self.query_one("#subscribers", Static).update(
            render.subscribers(state, now, self.show_inactive)
        )
        self.query_one("#flow", Static).update(render.flow(state, now))

    def _refresh_devices(self, now: float) -> None:
        state = self.state
        rows = []
        needle = self.device_filter.lower()
        for device in sorted(state.devices.values(), key=lambda d: (d.domain, d.device_id)):
            node = state.node_of(device)
            haystack = f"{device.device_id} {device.device_type} {device.domain} {node}".lower()
            if needle and needle not in haystack:
                continue
            color = render.domain_color(device.domain)
            rows.append((device.device_id, (
                render.state_mark(state.device_health(device, now)),
                Text(device.device_id, style=color),
                device.device_type,
                Text(device.domain, style=color),
                node,
                device.worker_id,
                device.site_id,
                render.value_cell(state, device),
                render.mode_text(state, device.device_id),
                render.sparkline(list(device.values), 16),
                str(device.sequence),
                f"{device.rate(now):.2f}",
                Text(str(device.missing), style="yellow" if device.missing else "dim"),
                render.ago(now - device.last_seen),
                render.consumers_text(state, device),
            )))  # fmt: skip
        sync_rows(self.query_one("#device-table", DataTable), rows)

    def _refresh_monitors(self, now: float) -> None:
        rows = []
        monitors = sorted(
            self.state.monitors.values(),
            key=lambda m: (m.state is not MonitorState.ONLINE, m.client_id),
        )
        for m in monitors:
            if m.state is not MonitorState.ONLINE and not self.show_inactive:
                continue
            counters = m.status.counters
            latency = counters.latency_ms if counters else None
            rows.append((m.client_id, (
                render.state_mark(m.health(now)),
                m.client_id,
                Text(m.status.topic_filter, style="cyan"),
                m.node,
                f"{m.rate:.1f}",
                str(m.received),
                Text(str(m.invalid), style="red" if m.invalid else "dim"),
                str(counters.devices_seen) if counters else "-",
                f"{latency.avg:.1f} ms" if latency else "-",
                f"{latency.p95:.1f} ms" if latency else "-",
                render.duration(now - m.status.started_at.timestamp()),
                render.ago(now - m.seen_at),
            )))  # fmt: skip
        sync_rows(self.query_one("#monitor-table", DataTable), rows)
        keys = [key for key, _ in rows]
        if self.selected_monitor not in keys:
            self.selected_monitor = keys[0] if keys else None
        detail = self.query_one("#monitor-detail", Static)
        monitor = self.state.monitors.get(self.selected_monitor or "")
        detail.update(
            render.monitor_detail(self.state, monitor, now)
            if monitor
            else Text("No monitor status received yet.", style="dim")
        )

    def _refresh_events(self, now: float) -> None:
        drawn = (self.state.version, self.event_filter, self.include_telemetry)
        if self.paused or drawn == self._events_drawn:
            return  # nothing new: keep the selection where the user left it
        self._events_drawn = drawn
        needle = self.event_filter.lower()
        selected = []
        source = self.state.events if self.include_telemetry else self.state.notable
        for event in reversed(source):
            if needle and needle not in f"{event.kind} {event.source} {event.text}".lower():
                continue
            selected.append(event)
            if len(selected) == EVENT_ROWS:
                break
        self._event_snapshot = selected
        rows = []
        for index, event in enumerate(selected):
            style = EVENT_STYLE.get(event.kind, render.domain_color(event.domain or ""))
            rows.append((f"e{id(event)}-{index}", (
                datetime.fromtimestamp(event.at, UTC).strftime("%H:%M:%S.%f")[:-3],
                Text(str(event.kind), style=style),
                Text(event.source, style=render.domain_color(event.domain) if event.domain
                     else ""),
                event.text,
            )))  # fmt: skip
        table = self.query_one("#event-table", DataTable)
        table.clear()
        for key, cells in rows:
            table.add_row(*cells, key=key)

    def _refresh_broker(self, now: float) -> None:
        stats = Table.grid(padding=(0, 2))
        stats.add_column(style="bold")
        stats.add_column()
        stats.add_column(style="dim")
        for name, label in [
            ("observed", "telemetry seen by this TUI"),
            ("in", "broker publications in (1 min avg)"),
            ("out", "broker publications out (1 min avg)"),
        ]:
            values = list(self.history[name])
            stats.add_row(
                label,
                Text(render.sparkline(values, 60), style="cyan"),
                f"{values[-1]:.1f} msg/s" if values else "-",
            )
        self.query_one("#broker-body", Static).update(
            Group(
                render.broker_summary(self.state, self.broker_label, now),
                Text(""),
                stats,
                Text(
                    "\nBroker figures come from Mosquitto's $SYS topics (updated every 10 s). "
                    "Messages out include the copies sent to this TUI.",
                    style="dim",
                ),
            )
        )

    # Actions -------------------------------------------------------------------------

    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab
        self._focus_main(tab)
        self.refresh_view()

    def _focus_main(self, tab: str) -> None:
        """Keep the keyboard on the tab's table so number keys keep switching tabs."""
        table_id = TAB_TABLES.get(tab)
        if table_id:
            self.query_one(f"#{table_id}", DataTable).focus()
        else:
            self.set_focus(None)

    def _show_detail(self, title: str, body: RenderableType, origin: DataTable) -> None:
        self.push_screen(DetailScreen(title, body), callback=lambda _: origin.focus())

    def get_system_commands(self, screen: Screen) -> Iterable[SystemCommand]:
        """The menu (P): every action of the UI with its shortcut, then Textual's own."""
        for title, key, help_text, action in self.MENU:
            yield SystemCommand(f"{title} ({key})", help_text, partial(self.run_action, action))
        for command in super().get_system_commands(screen):
            if command.title.lower() != "quit":
                yield command

    def action_alerts(self) -> None:
        body = render.alerts_table(self.state, self.clock())
        self.push_screen(DetailScreen(f"Open alerts ({len(self.state.alerts)})", body))

    def action_filter(self) -> None:
        active = self.query_one("#tabs", TabbedContent).active
        if active in ("devices", "events"):
            self.query_one(f"#{active[:-1]}-filter", Input).focus()

    def action_leave_filter(self) -> None:
        active = self.query_one("#tabs", TabbedContent).active
        if active in ("devices", "events"):
            self.query_one(f"#{active[:-1]}-table", DataTable).focus()

    def action_pause(self) -> None:
        self.paused = not self.paused
        self.notify("Events paused" if self.paused else "Events resumed")

    def action_toggle_telemetry(self) -> None:
        self.include_telemetry = not self.include_telemetry
        self.notify(
            "Events: telemetry included" if self.include_telemetry else "Events: system changes"
        )
        self.refresh_view()

    def action_toggle_inactive(self) -> None:
        self.show_inactive = not self.show_inactive
        self.notify("Showing inactive" if self.show_inactive else "Hiding stopped/offline")
        self.refresh_view()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        # Also covers tabs chosen with the mouse.
        self._focus_main(event.pane.id or "")
        self.refresh_view()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "device-filter":
            self.device_filter = event.value
        else:
            self.event_filter = event.value
        self.refresh_view()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "monitor-table" and event.row_key.value:
            self.selected_monitor = event.row_key.value
            self.refresh_view()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value or ""
        now = self.clock()
        table = event.data_table
        if table.id == "device-table" and key in self.state.devices:
            device = self.state.devices[key]
            body = render.device_detail(self.state, device, now)
            self._show_detail(device.device_id, body, table)
        elif table.id == "monitor-table" and key in self.state.monitors:
            monitor = self.state.monitors[key]
            body = Text(monitor.status.model_dump_json(indent=2))
            self._show_detail(monitor.client_id, body, table)
        elif event.data_table.id == "event-table":
            index = event.cursor_row
            if 0 <= index < len(self._event_snapshot):
                item = self._event_snapshot[index]
                body = Text(f"{item.kind}  {item.source}\n{item.text}\n\n")
                body.append(item.payload or "(no payload)")
                self._show_detail(str(item.kind), body, table)
