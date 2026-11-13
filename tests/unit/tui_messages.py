"""Builders of broker messages for the TUI tests."""

from datetime import UTC, datetime

from monitoring_mqtt.core.models import Measurement
from monitoring_mqtt.core.registry import WorkerClaim
from monitoring_mqtt.core.status import (
    LatencyStats,
    MonitorCounters,
    MonitorState,
    MonitorStatus,
    monitor_status_topic,
)
from monitoring_mqtt.tui.state import Message

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
E0 = T0.timestamp()


def telemetry(device_id: str, seq: int, at: float, domain: str = "poultry", **extra) -> Message:
    metric = {"poultry": "ammonia", "cattle": "rumen_ph", "aquaculture": "dissolved_oxygen"}
    m = Measurement.create(
        run_id="lab-01", device_id=device_id, device_type=f"{domain}_type", domain=domain,
        site_id="house-01", metric=metric[domain], value=extra.pop("value", 12.0), unit="ppm",
        sequence=seq, timestamp=datetime.fromtimestamp(at - 0.002, UTC),
    )  # fmt: skip
    return Message(m.topic, m.to_payload(), at)


def claim(worker_id: str, node: str = "pc-1", hostname: str | None = None, at: float = E0):
    c = WorkerClaim.create(
        worker_id=worker_id, hostname=hostname or f"{worker_id}000000", node=node,
        run_id="lab-01", device_types=["ammonia_detector"], devices=1, started_at=T0,
    )  # fmt: skip
    return Message(f"monitoring/registry/v1/workers/{worker_id}", c.to_payload(), at)


def status(
    client_id: str, topic_filter: str, at: float, state=MonitorState.ONLINE, received=0, **by
) -> Message:
    s = MonitorStatus(
        client_id=client_id, monitor=client_id.split("-")[1], topic_filter=topic_filter,
        node="pc-2", hostname="abc", state=state, started_at=T0,
        updated_at=datetime.fromtimestamp(at, UTC),
        counters=MonitorCounters(
            received=received, invalid=0, devices_seen=1, by_domain=by,
            latency_ms=LatencyStats(last=1, avg=1, p95=2),
        ) if state is not MonitorState.OFFLINE else None,
    )  # fmt: skip
    return Message(monitor_status_topic(client_id), s.to_payload(), at)
