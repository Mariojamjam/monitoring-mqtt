"""What the observer knows about the system, built only from broker messages.

Pure model: no MQTT client and no Textual. The feed hands it raw messages; the UI reads
it. Sources:

- telemetry (``monitoring/v1/#``): devices, values, sequence gaps, observed rates;
- worker registry (``monitoring/registry/v1/workers/+``): workers and their machines;
- monitor status (``monitoring/status/v1/monitors/+``): who consumes, with counters;
- broker statistics (``$SYS/broker/#``): clients, message rates, uptime;
- simulation ground truth (``monitoring/sim/v1/devices/+``): episodes in effect per device
  (anomaly, pause, delay, disconnect, stop), announced by the workers.

The operating range of each device type comes from the device catalog, so values outside
the normal band can be highlighted.

What each monitor received comes from its own status (``counters.recent``). Which online
monitors a device's topic is routed to is computed with the broker's filter matching.
"""

from __future__ import annotations

import json
from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.core.registry import REGISTRY_PREFIX, WorkerClaim
from monitoring_mqtt.core.simulation import SIM_PREFIX, EpisodeKind, EpisodePhase, SimEvent
from monitoring_mqtt.core.status import (
    STATUS_INTERVAL_SECONDS,
    STATUS_PREFIX,
    MonitorState,
    MonitorStatus,
    ReceivedMessage,
)
from monitoring_mqtt.core.topics import SMOKE_DOMAIN, TELEMETRY_PREFIX, matches

HISTORY_POINTS = 60  # values kept per device
RATE_WINDOW_SECONDS = 10.0
FLOW_WINDOW_SECONDS = 60.0
EVENT_LIMIT = 500
STALE_INTERVALS = 2.5  # a device is stale after this many publish intervals without data
MONITOR_STALE_SECONDS = 3 * STATUS_INTERVAL_SECONDS
UNKNOWN_NODE = "unknown machine"
UNREGISTERED = "unregistered"
SYS_PREFIX = "$SYS/broker/"


@dataclass(frozen=True)
class Message:
    topic: str
    payload: bytes
    received_at: float  # epoch seconds


@dataclass(frozen=True)
class Connection:
    connected: bool
    at: float
    detail: str = ""


FeedItem = Message | Connection


class Health(StrEnum):
    ONLINE = "online"
    STALE = "stale"
    GONE = "gone"


class EventKind(StrEnum):
    TELEMETRY = "telemetry"
    INVALID = "invalid"
    GAP = "gap"
    RESTART = "restart"
    DUPLICATE = "duplicate"
    PROBE = "probe"
    WORKER_JOINED = "worker joined"
    WORKER_LEFT = "worker left"
    MONITOR_ONLINE = "monitor online"
    MONITOR_STOPPED = "monitor stopped"
    MONITOR_OFFLINE = "monitor offline"
    SIMULATED = "simulated"
    BROKER = "broker"


@dataclass(frozen=True)
class Event:
    at: float
    kind: EventKind
    source: str
    text: str
    domain: str | None = None
    payload: str | None = None


def _pretty(payload: bytes) -> str:
    try:
        return json.dumps(json.loads(payload), indent=2, ensure_ascii=False)
    except (ValueError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def worker_of(device_id: str, domain: str) -> str:
    """``{domain}-{worker_id}-{index}`` → ``worker_id``."""
    prefix = f"{domain}-"
    body = device_id[len(prefix) :] if device_id.startswith(prefix) else device_id
    return body.rsplit("-", 1)[0] if "-" in body else body


def _rate(arrivals: Iterable[float], now: float, window: float) -> float:
    return sum(1 for t in arrivals if t >= now - window) / window


@dataclass
class DeviceView:
    device_id: str
    device_type: str
    domain: str
    metric: str
    site_id: str
    unit: str | None
    worker_id: str
    run_id: str
    topic: str
    value: float
    sequence: int
    timestamp: datetime
    first_seen: float
    last_seen: float
    latency_ms: float
    payload: str
    received: int = 1
    missing: int = 0
    duplicates: int = 0
    restarts: int = 0
    values: deque[float] = field(default_factory=lambda: deque(maxlen=HISTORY_POINTS))
    arrivals: deque[float] = field(default_factory=lambda: deque(maxlen=1_000))

    def rate(self, now: float) -> float:
        return _rate(self.arrivals, now, RATE_WINDOW_SECONDS)


@dataclass(frozen=True)
class Band:
    """A device type's synthetic normal band (``operating_range`` in the catalog)."""

    minimum: float
    maximum: float

    def contains(self, value: float) -> bool:
        return self.minimum <= value <= self.maximum


@dataclass
class Episodes:
    """Simulated episodes in effect for one device, from the workers' ground truth."""

    value: SimEvent | None = None  # an anomaly
    availability: SimEvent | None = None  # pause, delay, disconnect or stop

    @property
    def active(self) -> list[SimEvent]:
        return [e for e in (self.availability, self.value) if e is not None]


@dataclass
class WorkerView:
    claim: WorkerClaim
    present: bool = True
    changed_at: float = 0.0

    @property
    def node(self) -> str:
        return self.claim.node or UNKNOWN_NODE

    @property
    def own_id(self) -> bool:
        """The worker id does not derive from its container (WORKER_ID or a collision)."""
        return not self.claim.hostname.startswith(self.claim.worker_id)


@dataclass
class MonitorView:
    status: MonitorStatus
    state: MonitorState
    seen_at: float
    rate: float = 0.0  # messages/s, from successive status counters

    @property
    def client_id(self) -> str:
        return self.status.client_id

    @property
    def node(self) -> str:
        return self.status.node or UNKNOWN_NODE

    @property
    def received(self) -> int:
        return self.status.counters.received if self.status.counters else 0

    @property
    def invalid(self) -> int:
        return self.status.counters.invalid if self.status.counters else 0

    @property
    def recent(self) -> list[ReceivedMessage]:
        """Messages this monitor reported as received, newest first."""
        counters = self.status.counters
        return list(reversed(counters.recent)) if counters else []

    def health(self, now: float) -> MonitorState | Health:
        if self.state is MonitorState.ONLINE and now - self.seen_at > MONITOR_STALE_SECONDS:
            return Health.STALE
        return self.state

    def receives(self, topic: str) -> bool:
        return self.state is MonitorState.ONLINE and matches(self.status.topic_filter, topic)


@dataclass
class BrokerView:
    values: dict[str, str] = field(default_factory=dict)
    connected: bool = False
    since: float | None = None
    detail: str = ""
    disconnects: int = 0

    def _number(self, key: str) -> float | None:
        raw = self.values.get(key, "").split(" ")[0]
        try:
            return float(raw)
        except ValueError:
            return None

    @property
    def version(self) -> str | None:
        return self.values.get("version")

    @property
    def uptime_seconds(self) -> float | None:
        return self._number("uptime")

    @property
    def clients(self) -> float | None:
        return self._number("clients/connected")

    @property
    def subscriptions(self) -> float | None:
        return self._number("subscriptions/count")

    @property
    def retained(self) -> float | None:
        return self._number("retained messages/count")

    def per_second(self, direction: str) -> float | None:
        """Broker-wide PUBLISH rate (``received``, ``sent`` or ``dropped``), 1-minute average.

        ``load/publish/*`` counts publications only; ``load/messages/*`` would also count
        acknowledgements and pings.
        """
        per_minute = self._number(f"load/publish/{direction}/1min")
        return None if per_minute is None else per_minute / 60


class SystemState:
    """Aggregated view of the system; mutated only by ``apply`` (single thread)."""

    def __init__(
        self,
        publish_interval: float = 5.0,
        observer_id: str = "",
        bands: dict[str, Band] | None = None,
    ) -> None:
        self.publish_interval = publish_interval
        self.observer_id = observer_id
        self.bands = bands or {}  # device type → operating range
        self.episodes: dict[str, Episodes] = {}  # device id → simulated episodes
        self.devices: dict[str, DeviceView] = {}
        self.workers: dict[str, WorkerView] = {}
        self.monitors: dict[str, MonitorView] = {}
        self.broker = BrokerView()
        self.events: deque[Event] = deque(maxlen=EVENT_LIMIT)  # everything, telemetry included
        # System changes kept apart: under load, telemetry would push them out of `events`.
        self.notable: deque[Event] = deque(maxlen=EVENT_LIMIT)
        self.invalid = 0
        self.probes = 0
        self.run_ids: Counter[str] = Counter()
        self.arrivals: dict[str, deque[float]] = {}  # per domain, for the flow view
        self.version = 0  # increases on every change; lets the UI skip redraws

    # Input ---------------------------------------------------------------------------

    def apply(self, item: FeedItem) -> None:
        self.version += 1
        if isinstance(item, Connection):
            self._connection(item)
        elif item.topic.startswith(f"{TELEMETRY_PREFIX}/"):
            self._telemetry(item)
        elif item.topic.startswith(f"{REGISTRY_PREFIX}/workers/"):
            self._claim(item)
        elif item.topic.startswith(f"{STATUS_PREFIX}/monitors/"):
            self._status(item)
        elif item.topic.startswith(f"{SIM_PREFIX}/devices/"):
            self._simulated(item)
        elif item.topic.startswith(SYS_PREFIX):
            self.broker.values[item.topic[len(SYS_PREFIX) :]] = item.payload.decode(
                "utf-8", errors="replace"
            )

    def apply_all(self, items: Iterable[FeedItem]) -> None:
        for item in items:
            self.apply(item)

    def _event(self, at: float, kind: EventKind, source: str, text: str, **extra) -> None:
        event = Event(at, kind, source, text, **extra)
        self.events.append(event)
        if kind is not EventKind.TELEMETRY:
            self.notable.append(event)

    def _connection(self, item: Connection) -> None:
        broker = self.broker
        if item.connected and not broker.connected:
            broker.connected, broker.since = True, item.at
            self._event(item.at, EventKind.BROKER, "observer", f"connected {item.detail}".strip())
        elif not item.connected and broker.connected:
            broker.connected, broker.since = False, item.at
            broker.disconnects += 1
            self._event(
                item.at, EventKind.BROKER, "observer", f"disconnected {item.detail}".strip()
            )
        broker.detail = item.detail

    def _telemetry(self, item: Message) -> None:
        try:
            m = Measurement.from_payload(item.payload)
            m.ensure_matches_topic(item.topic)
        except ValueError as exc:  # ValidationError, TopicMismatchError, InvalidTopicError
            self.invalid += 1
            reason = str(exc).splitlines()[0][:120]
            self._event(
                item.received_at, EventKind.INVALID, item.topic, reason,
                payload=_pretty(item.payload),
            )  # fmt: skip
            return
        if m.domain == SMOKE_DOMAIN:
            self.probes += 1
            self._event(item.received_at, EventKind.PROBE, m.device_id, "smoke test probe")
            return

        now = item.received_at
        latency = (now - m.timestamp.timestamp()) * 1000
        payload = _pretty(item.payload)
        self.run_ids[m.run_id] += 1
        self.arrivals.setdefault(m.domain, deque(maxlen=50_000)).append(now)
        device = self.devices.get(m.device_id)
        if device is None:
            device = self.devices[m.device_id] = DeviceView(
                device_id=m.device_id, device_type=m.device_type, domain=m.domain,
                metric=m.metric, site_id=m.site_id, unit=m.unit,
                worker_id=worker_of(m.device_id, m.domain), run_id=m.run_id, topic=item.topic,
                value=m.value, sequence=m.sequence, timestamp=m.timestamp,
                first_seen=now, last_seen=now, latency_ms=latency, payload=payload, received=0,
            )  # fmt: skip
        else:
            gap = m.sequence - device.sequence
            if gap == 0:
                device.duplicates += 1
                self._event(now, EventKind.DUPLICATE, m.device_id, f"seq {m.sequence} again")
            elif gap < 0:
                device.restarts += 1
                self._event(
                    now, EventKind.RESTART, m.device_id,
                    f"seq {device.sequence} → {m.sequence} (device restarted)", domain=m.domain,
                )  # fmt: skip
            elif gap > 1:
                device.missing += gap - 1
                self._event(
                    now, EventKind.GAP, m.device_id,
                    f"{gap - 1} missing (seq {device.sequence} → {m.sequence})", domain=m.domain,
                )  # fmt: skip
        device.value, device.sequence, device.timestamp = m.value, m.sequence, m.timestamp
        device.unit, device.run_id, device.site_id = m.unit, m.run_id, m.site_id
        device.last_seen, device.latency_ms, device.payload = now, latency, payload
        device.received += 1
        device.values.append(m.value)
        device.arrivals.append(now)
        unit = f" {m.unit}" if m.unit else ""
        self._event(
            now, EventKind.TELEMETRY, m.device_id,
            f"seq {m.sequence}  {m.metric} = {m.value:g}{unit}", domain=m.domain, payload=payload,
        )  # fmt: skip

    def _simulated(self, item: Message) -> None:
        try:
            event = SimEvent.from_payload(item.payload)
        except ValueError:
            return
        episodes = self.episodes.setdefault(event.device_id, Episodes())
        slot = "value" if event.kind is EpisodeKind.ANOMALY else "availability"
        if event.phase is EpisodePhase.START:
            setattr(episodes, slot, event)
        elif (current := getattr(episodes, slot)) is not None and current.kind is event.kind:
            setattr(episodes, slot, None)
        what = event.kind.value + (f" {event.direction}" if event.direction else "")
        detail = f"{what} {'started' if event.phase is EpisodePhase.START else 'ended'}"
        detail += f" ({event.source.value}"
        if event.phase is EpisodePhase.START and event.until is not None:
            detail += f", {(event.until - event.at).total_seconds():.0f} s"
        detail += ")"
        self._event(
            item.received_at, EventKind.SIMULATED, event.device_id, detail,
            payload=_pretty(item.payload),
        )  # fmt: skip

    def _claim(self, item: Message) -> None:
        worker_id = item.topic.rsplit("/", 1)[-1]
        known = self.workers.get(worker_id)
        if not item.payload:
            if known and known.present:
                known.present, known.changed_at = False, item.received_at
                self._event(item.received_at, EventKind.WORKER_LEFT, worker_id, "claim removed")
                # Its devices are gone: their simulated episodes ended with them.
                for device_id, episodes in self.episodes.items():
                    if any(e.worker_id == worker_id for e in episodes.active):
                        self.episodes[device_id] = Episodes()
            return
        try:
            claim = WorkerClaim.from_payload(item.payload)
        except ValueError:
            return
        if known is None or not known.present or known.claim.instance_id != claim.instance_id:
            types = ", ".join(claim.device_types)
            self._event(
                item.received_at, EventKind.WORKER_JOINED, worker_id,
                f"{claim.devices} x {types} on {claim.node or UNKNOWN_NODE}",
            )  # fmt: skip
        self.workers[worker_id] = WorkerView(claim, True, item.received_at)

    def _status(self, item: Message) -> None:
        client_id = item.topic.rsplit("/", 1)[-1]
        known = self.monitors.get(client_id)
        if not item.payload:  # removed after a clean stop
            if known and known.state is not MonitorState.STOPPED:
                known.state, known.seen_at = MonitorState.STOPPED, item.received_at
            return
        try:
            status = MonitorStatus.from_payload(item.payload)
        except ValueError:
            return
        restarted = known is not None and known.status.started_at != status.started_at
        rate = 0.0 if restarted or known is None else known.rate
        if known and not restarted and status.counters and known.status.counters:
            elapsed = (status.updated_at - known.status.updated_at).total_seconds()
            if elapsed > 0:
                rate = max(0.0, (status.counters.received - known.received) / elapsed)
        if restarted and status.state is MonitorState.ONLINE:
            # Same client id, new process: counters start again from zero.
            self._event(
                item.received_at, EventKind.MONITOR_ONLINE, client_id,
                f"{status.topic_filter} (restarted)",
            )  # fmt: skip
        elif known is None or known.state is not status.state:
            kind = {
                MonitorState.ONLINE: EventKind.MONITOR_ONLINE,
                MonitorState.STOPPED: EventKind.MONITOR_STOPPED,
                MonitorState.OFFLINE: EventKind.MONITOR_OFFLINE,
            }[status.state]
            self._event(item.received_at, kind, client_id, status.topic_filter)
        self.monitors[client_id] = MonitorView(status, status.state, item.received_at, rate)

    # Queries -------------------------------------------------------------------------

    def mode_of(self, device_id: str) -> Episodes:
        return self.episodes.get(device_id) or Episodes()

    def out_of_range(self, device: DeviceView) -> bool | None:
        """Whether the latest value is outside the normal band (None: no band known)."""
        band = self.bands.get(device.device_type)
        return None if band is None else not band.contains(device.value)

    def device_health(self, device: DeviceView, now: float) -> Health:
        if now - device.last_seen <= STALE_INTERVALS * self.publish_interval:
            return Health.ONLINE
        worker = self.workers.get(device.worker_id)
        return Health.GONE if worker is not None and not worker.present else Health.STALE

    def node_of(self, device: DeviceView) -> str:
        worker = self.workers.get(device.worker_id)
        return worker.node if worker else UNREGISTERED

    def consumers_of(self, device: DeviceView) -> list[MonitorView]:
        return [m for m in self.monitors.values() if m.receives(device.topic)]

    def online_monitors(self) -> list[MonitorView]:
        return [m for m in self.monitors.values() if m.state is MonitorState.ONLINE]

    def domains(self) -> list[str]:
        return sorted({d.domain for d in self.devices.values()})

    def domain_rate(self, domain: str, now: float) -> float:
        return _rate(self.arrivals.get(domain, ()), now, RATE_WINDOW_SECONDS)

    def observed_rate(self, now: float) -> float:
        return sum(self.domain_rate(domain, now) for domain in self.arrivals)

    def flow(self, domain: str, now: float, buckets: int = 30) -> list[int]:
        """Messages per bucket over the last ``FLOW_WINDOW_SECONDS`` (oldest first)."""
        width = FLOW_WINDOW_SECONDS / buckets
        counts = [0] * buckets
        start = now - FLOW_WINDOW_SECONDS
        for t in self.arrivals.get(domain, ()):
            if t >= start:
                counts[min(buckets - 1, int((t - start) / width))] += 1
        return counts

    def missing_total(self) -> int:
        return sum(d.missing for d in self.devices.values())

    @property
    def run_id(self) -> str | None:
        return self.run_ids.most_common(1)[0][0] if self.run_ids else None


def now_epoch() -> float:
    return datetime.now(UTC).timestamp()
