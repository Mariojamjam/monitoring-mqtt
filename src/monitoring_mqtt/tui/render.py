"""Rich renderables built from the observer state (no Textual, testable on their own)."""

from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Group
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from monitoring_mqtt.core.simulation import EpisodeKind
from monitoring_mqtt.core.status import MonitorState, ReceivedMessage
from monitoring_mqtt.tui.state import (
    UNREGISTERED,
    DeviceView,
    Health,
    MonitorView,
    SystemState,
)

SPARK = "▁▂▃▄▅▆▇█"
DOMAIN_COLORS = {"aquaculture": "cyan", "poultry": "yellow", "cattle": "magenta"}
EXTRA_COLORS = ("green", "blue", "bright_red", "bright_cyan")
STATE_STYLE = {
    Health.ONLINE: ("●", "green"),
    MonitorState.ONLINE: ("●", "green"),
    Health.STALE: ("◐", "yellow"),
    MonitorState.STOPPED: ("○", "bright_black"),
    MonitorState.OFFLINE: ("✕", "red"),
    Health.GONE: ("○", "red"),
}
PULSE_SECONDS = 1.0  # a device "lights up" this long after each message
COLLAPSE_DEVICES = 6  # workers with more devices show one summary line in the overview


def domain_color(domain: str) -> str:
    return DOMAIN_COLORS.get(domain) or EXTRA_COLORS[sum(map(ord, domain)) % len(EXTRA_COLORS)]


def sparkline(values: list[float], width: int | None = None) -> str:
    data = values[-width:] if width else values
    if not data:
        return ""
    low, high = min(data), max(data)
    span = high - low
    return "".join(SPARK[int((v - low) / span * (len(SPARK) - 1)) if span else 0] for v in data)


def state_mark(state: Health | MonitorState) -> Text:
    symbol, style = STATE_STYLE[state]
    return Text(symbol, style=style)


def ago(seconds: float) -> str:
    if seconds < 1:
        return "now"
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{secs:02d}s"


def value_text(device: DeviceView) -> str:
    unit = f" {device.unit}" if device.unit else ""
    return f"{device.value:g}{unit}"


def value_cell(state: SystemState, device: DeviceView) -> Text:
    """The latest value, red when outside the device type's normal band."""
    outside = state.out_of_range(device)
    return Text(value_text(device), style="bold red" if outside else "bold")


MODE_STYLE = {
    EpisodeKind.ANOMALY: "bold red",
    EpisodeKind.PAUSE: "yellow",
    EpisodeKind.DELAY: "magenta",
    EpisodeKind.DISCONNECT: "red",
    EpisodeKind.STOP: "bright_black",
}
ARROWS = {"high": "↑", "low": "↓"}


ALERT_STYLE = {"critical": "bold white on red", "warning": "bold yellow"}


def mode_text(state: SystemState, device_id: str) -> Text:
    """Simulated episodes (ground truth) and open alerts (detection), e.g.
    ``anomaly ↑  alert critical``: the cause next to what the alert engine saw."""
    text = Text()
    for event in state.mode_of(device_id).active:
        if text:
            text.append("  ")
        label = event.kind.value + (f" {ARROWS[event.direction]}" if event.direction else "")
        text.append(label, style=MODE_STYLE[event.kind])
    for alert in state.alerts_of(device_id):
        if text:
            text.append("  ")
        text.append(f"alert {alert.severity.value}", style=ALERT_STYLE[alert.severity.value])
    return text or Text("normal", style="dim")


def consumers_text(state: SystemState, device: DeviceView) -> Text:
    consumers = state.consumers_of(device)
    if not consumers:
        return Text("nobody", style="bold red")
    return Text(f"{len(consumers)} monitor" + ("s" if len(consumers) > 1 else ""), style="green")


def header(state: SystemState, broker_label: str, now: float) -> Text:
    broker = state.broker
    devices = list(state.devices.values())
    online = sum(state.device_health(d, now) is Health.ONLINE for d in devices)
    text = Text()
    text.append(" monitoring-mqtt ", style="bold reverse")
    text.append(f"  run {state.run_id or '-'}", style="bold")
    text.append(f"   broker {broker_label} ")
    if broker.connected:
        text.append("● connected", style="green")
    else:
        text.append("● disconnected", style="bold red")
    text.append(f"   devices {online}/{len(devices)}")
    text.append(f"   workers {sum(w.present for w in state.workers.values())}")
    text.append(f"   monitors {len(state.online_monitors())}")
    text.append(f"   {state.observed_rate(now):.1f} msg/s")
    if state.invalid:
        text.append(f"   invalid {state.invalid}", style="red")
    if state.missing_total():
        text.append(f"   missing {state.missing_total()}", style="yellow")
    if state.stored() is not None:
        text.append(f"   stored {state.stored()}", style="cyan")
    open_alerts = state.open_alerts()
    if open_alerts:
        critical = sum(a.severity == "critical" for a in open_alerts)
        text.append(f"   alerts {len(open_alerts)}", style="bold yellow")
        if critical:
            text.append(f" ({critical} critical)", style="bold red")
    text.append(f"   {datetime.fromtimestamp(now, UTC):%H:%M:%S} UTC", style="dim")
    return text


def publishers(state: SystemState, now: float, show_inactive: bool = True) -> Tree:
    """Machines → workers → devices, with the latest value and who consumes it."""
    tree = Tree("publishers", guide_style="dim", hide_root=True)
    by_node: dict[str, dict[str, list[DeviceView]]] = {}
    for device in sorted(state.devices.values(), key=lambda d: d.device_id):
        by_node.setdefault(state.node_of(device), {}).setdefault(device.worker_id, []).append(
            device
        )
    for worker_id, worker in state.workers.items():
        if worker.present or show_inactive:
            by_node.setdefault(worker.node, {}).setdefault(worker_id, [])
    if not by_node:
        tree.add(Text("no devices seen yet", style="dim"))
    for node in sorted(by_node):
        node_branch = tree.add(Text(f"🖥  {node}", style="bold"))
        for worker_id in sorted(by_node[node]):
            worker = state.workers.get(worker_id)
            if worker and not worker.present and not show_inactive:
                continue
            label = Text(f"worker {worker_id}", style="bold")
            if worker:
                label.append(f"  {', '.join(worker.claim.device_types)}", style="dim")
                if worker.own_id:
                    label.append("  own id", style="yellow")
                if not worker.present:
                    label.append("  gone", style="red")
            elif node == UNREGISTERED:
                label.append("  (no registry claim)", style="dim")
            worker_branch = node_branch.add(label)
            devices = by_node[node][worker_id]
            if len(devices) > COLLAPSE_DEVICES:
                worker_branch.add(_worker_summary(state, devices, now))
                continue
            for device in devices:
                health = state.device_health(device, now)
                line = Text()
                pulse = now - device.last_seen < PULSE_SECONDS and health is Health.ONLINE
                mark = state_mark(health)
                if pulse:  # same width, brighter: the device just published
                    mark = Text("●", style=f"bold {domain_color(device.domain)}")
                line.append_text(mark)
                line.append(f" {device.device_id}", style=domain_color(device.domain))
                line.append("  ")
                line.append_text(value_cell(state, device))
                if state.mode_of(device.device_id).active:
                    line.append("  ")
                    line.append_text(mode_text(state, device.device_id))
                line.append(f"  {sparkline(list(device.values), 8)}", style="dim")
                line.append(f"  seq {device.sequence}", style="dim")
                if device.missing:
                    line.append(f"  missing {device.missing}", style="yellow")
                line.append("  → ")
                line.append_text(consumers_text(state, device))
                worker_branch.add(line)
    return tree


def _worker_summary(state: SystemState, devices: list[DeviceView], now: float) -> Text:
    """One line for a worker with many devices (all of them are in the Devices tab)."""
    health = [state.device_health(d, now) for d in devices]
    online = sum(h is Health.ONLINE for h in health)
    line = Text()
    line.append_text(state_mark(Health.ONLINE if online == len(devices) else Health.STALE))
    line.append(f" {len(devices)} devices", style=domain_color(devices[0].domain))
    line.append(f"  {online} online", style="dim")
    line.append(f"  {sum(d.rate(now) for d in devices):.1f} msg/s", style="dim")
    missing = sum(d.missing for d in devices)
    if missing:
        line.append(f"  missing {missing}", style="yellow")
    line.append("  → ")
    line.append_text(consumers_text(state, devices[0]))
    line.append("  (tab 2 lists them)", style="dim")
    return line


def _monitor_line(monitor: MonitorView, now: float) -> Text:
    health = monitor.health(now)
    line = Text()
    line.append_text(state_mark(health))
    line.append(f" {monitor.client_id}")
    if health is MonitorState.ONLINE:
        line.append(f"  {monitor.rate:.1f}/s", style="green")
        line.append(f"  {monitor.received}", style="dim")
        if monitor.invalid:
            line.append(f"  ✕{monitor.invalid}", style="red")
    else:
        line.append(f"  {health}", style=STATE_STYLE[health][1])
    return line


def subscribers(state: SystemState, now: float, show_inactive: bool = True) -> Tree:
    """Machines → monitor kinds (filter) → replicas, with their reported counters."""
    tree = Tree("subscribers", guide_style="dim", hide_root=True)
    by_node: dict[str, dict[tuple[str, str], list[MonitorView]]] = {}
    for monitor in sorted(state.monitors.values(), key=lambda m: m.client_id):
        if monitor.state is not MonitorState.ONLINE and not show_inactive:
            continue
        key = (monitor.status.monitor, monitor.status.topic_filter)
        by_node.setdefault(monitor.node, {}).setdefault(key, []).append(monitor)
    if not by_node:
        tree.add(Text("no monitor status received", style="dim"))
    for node in sorted(by_node):
        node_branch = tree.add(Text(f"🖥  {node}", style="bold"))
        for (kind, topic_filter), members in sorted(by_node[node].items()):
            online = sum(m.state is MonitorState.ONLINE for m in members)
            label = Text(f"monitor-{kind}", style="bold")
            label.append(f"  {online}/{len(members)} online", style="dim")
            label.append("\nsubscribes ", style="dim").append(topic_filter, style="cyan")
            group = node_branch.add(label)
            for monitor in members:
                replica = group.add(_monitor_line(monitor, now))
                if monitor.state is MonitorState.ONLINE:
                    recent = monitor.recent
                    replica.add(
                        received_line(recent[0], now)
                        if recent
                        else Text("nothing received yet", style="dim")
                    )
    if state.observer_id:
        tree.add(Text(f"observer {state.observer_id} (this TUI, not counted)", style="dim"))
    return tree


def broker_summary(state: SystemState, broker_label: str, now: float) -> Table:
    broker = state.broker
    table = Table.grid(padding=(0, 1))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row("address", broker_label)
    connection = (
        Text("● connected", style="green")
        if broker.connected
        else Text("● disconnected", style="bold red")
    )
    table.add_row("observer", connection)
    if broker.since is not None:
        table.add_row("since", ago(now - broker.since))
    table.add_row("version", (broker.version or "-").replace("mosquitto version ", "Mosquitto "))
    table.add_row("uptime", duration(broker.uptime_seconds))
    table.add_row("clients", _number(broker.clients))
    received, sent = broker.per_second("received"), broker.per_second("sent")
    table.add_row("publish in", f"{received:.1f} msg/s" if received is not None else "-")
    table.add_row("publish out", f"{sent:.1f} msg/s" if sent is not None else "-")
    if received and sent is not None:
        table.add_row("out / in", f"x{sent / received:.1f}")
    dropped = broker.per_second("dropped")
    if dropped:
        table.add_row("dropped", Text(f"{dropped:.1f} msg/s", style="red"))
    table.add_row("subscriptions", _number(broker.subscriptions))
    table.add_row("retained", _number(broker.retained))
    if broker.disconnects:
        table.add_row("drops", Text(str(broker.disconnects), style="yellow"))
    return table


def _number(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def flow(state: SystemState, now: float) -> Table:
    """Per-domain message flow and where it goes."""
    table = Table.grid(padding=(0, 2))
    table.add_column(no_wrap=True)
    table.add_column(no_wrap=True)
    table.add_column(justify="right", no_wrap=True)
    table.add_column()
    domains = state.domains()
    if not domains:
        table.add_row(Text("waiting for telemetry…", style="dim"), "", "", "")
    for domain in domains:
        color = domain_color(domain)
        sample = next(d for d in state.devices.values() if d.domain == domain)
        consumers = state.consumers_of(sample)
        groups: dict[str, int] = {}
        for monitor in consumers:
            name = f"monitor-{monitor.status.monitor}"
            groups[name] = groups.get(name, 0) + 1
        target = (
            Text(
                ", ".join(f"{name}" + (f" x{n}" if n > 1 else "") for name, n in groups.items()),
                style="green",
            )
            if groups
            else Text("no subscriber: the broker drops it", style="bold red")
        )
        table.add_row(
            Text(domain, style=f"bold {color}"),
            Text(sparkline([float(c) for c in state.flow(domain, now)]), style=color),
            f"{state.domain_rate(domain, now):.1f} msg/s",
            Text("→ routed to ", style="dim").append_text(target),
        )
    return table


def received_line(message: ReceivedMessage, now: float) -> Text:
    """Compact "last received" line: ``← device  value unit  #seq  age``."""
    domain = message.topic.split("/")[2] if message.topic.count("/") >= 2 else ""
    unit = f" {message.unit}" if message.unit else ""
    age = ago(now - message.received_at.timestamp()).removesuffix(" ago")
    line = Text("← ", style="dim")
    line.append(message.device_id, style=domain_color(domain))
    line.append(f" {message.value:g}{unit}", style="bold")
    line.append(f" #{message.sequence} {age}", style="dim")
    return line


def monitor_detail(state: SystemState, monitor: MonitorView, now: float) -> Group:
    """One consumer: what it subscribes to and what it reported as received."""
    status = monitor.status
    health = monitor.health(now)
    head = Text()
    head.append_text(state_mark(health))
    head.append(f" {monitor.client_id}", style="bold")
    head.append(f"  {health}  on {monitor.node}", style="dim")
    subscribed = Text("subscribes  ", style="dim").append(status.topic_filter, style="bold cyan")
    counters = status.counters
    summary = Text(style="dim")
    if counters:
        summary.append(f"received {counters.received}  invalid {counters.invalid}  "
                       f"devices {counters.devices_seen}  {monitor.rate:.1f} msg/s")  # fmt: skip
        if counters.latency_ms:
            summary.append(f"  latency avg {counters.latency_ms.avg:.1f} ms "
                           f"p95 {counters.latency_ms.p95:.1f} ms")  # fmt: skip
        if counters.by_domain:
            summary.append("\nby domain  " + "  ".join(
                f"{domain} {count}" for domain, count in sorted(counters.by_domain.items())
            ))  # fmt: skip
    if status.delivery:
        d = status.delivery
        summary.append(
            f"\nstores via {d.api_url}  stored {d.stored}  duplicates {d.duplicates}  "
            f"buffered {d.buffered}  dropped {d.dropped}  API errors {d.api_errors}"
        )
    if status.alerts:
        a = status.alerts
        by = "  ".join(f"{s} {n}" for s, n in sorted(a.open_by_severity.items()))
        summary.append(f"\nalerts open {a.open} {by}  raised {a.raised}  cleared {a.cleared}")
    table = Table(
        title="Received (reported by the monitor, newest first)",
        title_justify="left",
        expand=True,
        show_edge=False,
    )
    for column in ("Received (UTC)", "Topic", "Value", "Seq", "Publisher at", "Latency"):
        table.add_column(column, no_wrap=column != "Topic")
    for message in monitor.recent:
        domain = message.topic.split("/")[2] if message.topic.count("/") >= 2 else ""
        device = state.devices.get(message.device_id)
        unit = f" {message.unit}" if message.unit else ""
        behind = device.sequence - message.sequence if device else 0
        table.add_row(
            message.received_at.strftime("%H:%M:%S.%f")[:-3],
            Text(message.topic, style=domain_color(domain)),
            Text(f"{message.value:g}{unit}", style="bold"),
            str(message.sequence),
            Text(f"{device.sequence}" + (f" (+{behind})" if behind > 0 else ""), style="dim")
            if device
            else "-",
            f"{message.latency_ms:.1f} ms",
        )
    if not monitor.recent:
        table.add_row(Text("nothing received yet", style="dim"), "", "", "", "", "")
    note = Text(
        "\nPublisher at = the latest sequence the TUI has seen from that device. The monitor "
        "reports its status every 5 s, so its newest message can be a few seconds behind.",
        style="dim",
    )
    return Group(head, subscribed, summary, Text(""), table, note)


def _band(state: SystemState, device: DeviceView) -> str:
    band = state.bands.get(device.device_type)
    if band is None:
        return "-"
    unit = f" {device.unit}" if device.unit else ""
    return f"{band.minimum:g} - {band.maximum:g}{unit}"


def alerts_table(state: SystemState, now: float) -> Table:
    """Open alerts, critical first."""
    table = Table(expand=True, show_edge=False)
    for column in ("Severity", "Rule", "Device", "Value", "Condition", "Open for"):
        table.add_column(column, no_wrap=True)
    for alert in state.open_alerts():
        unit = f" {alert.unit}" if alert.unit else ""
        table.add_row(
            Text(alert.severity.value, style=ALERT_STYLE[alert.severity.value]),
            alert.rule_id,
            Text(alert.device_id, style=domain_color(alert.domain)),
            f"{alert.value:g}{unit}",
            f"{alert.condition.value} {alert.threshold:g}",
            duration(now - alert.at.timestamp()),
        )
    if not state.alerts:
        table.add_row(Text("no open alerts", style="dim"), "", "", "", "", "")
    return table


def device_detail(state: SystemState, device: DeviceView, now: float) -> Group:
    info = Table.grid(padding=(0, 2))
    info.add_column(style="dim")
    info.add_column()
    health = state.device_health(device, now)
    info.add_row("state", Text(str(health), style=STATE_STYLE[health][1]))
    for label, value in [
        ("topic", device.topic),
        ("type", device.device_type),
        ("site", device.site_id),
        ("machine / worker", f"{state.node_of(device)} / {device.worker_id}"),
        ("run", device.run_id),
        ("value", value_cell(state, device)),
        ("normal range", _band(state, device)),
        ("simulated", mode_text(state, device.device_id)),
        ("history", sparkline(list(device.values))),
        ("sequence", f"{device.sequence} (received {device.received}, missing {device.missing}, "
                     f"duplicates {device.duplicates}, restarts {device.restarts})"),
        ("rate", f"{device.rate(now):.2f} msg/s"),
        ("last seen", ago(now - device.last_seen)),
        ("latency to observer", f"{device.latency_ms:.1f} ms"),
    ]:  # fmt: skip
        info.add_row(label, value)
    consumers = state.consumers_of(device)
    info.add_row(
        "consumed by",
        Text("\n".join(f"{m.client_id}  ({m.status.topic_filter})" for m in consumers))
        if consumers
        else Text("nobody (no online monitor filter matches)", style="red"),
    )
    return Group(
        Text(device.device_id, style=f"bold {domain_color(device.domain)}"),
        info,
        Text("\nlast payload", style="dim"),
        Text(device.payload),
    )
