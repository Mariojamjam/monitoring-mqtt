"""Metrics of one run, from what the probe and the resource sampler collected (pure).

Definitions (docs/experiments.md):

- **published / acked / failed**: what the devices handed to MQTT, what was confirmed
  (PUBACK with QoS 1; written to the socket with QoS 0) and what could not be sent, during
  the window (worker counters, end minus start);
- **monitor delivery**: unique messages the monitors received / messages published on the
  domains their filters match (a monitor of every domain expects every message);
- **duplicates**: deliveries of an ``event_id`` the same consumer had already received;
- **storage delivery**: measurements stored / (stored + missing sequence numbers), for the
  measurements generated in the window;
- **latency**: generation on the device → reception by the ingestion consumer (MQTT path)
  and → storage, as percentiles; across machines they include the clock difference;
- **recovery**: after a failure action, the time until every device that was publishing
  before it has delivered again.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

PUBLISHER_FIELDS = ("generated", "published", "acked", "failed")
# Services grouped by role for resource columns (replicas and device types summed).
ROLE_PREFIXES = {
    "broker": ("broker",),
    "devices": ("device-",),
    "monitors": ("monitor-",),
    "ingestion": ("ingestion",),
    "api": ("api",),
    "postgres": ("postgres",),
    "alerts": ("alerts",),
    "dashboard": ("dashboard",),
}
ACTIVE_BEFORE_SECONDS = 10.0  # a device "was publishing" if it delivered in this span


def domain_of(device_id: str) -> str:
    return device_id.split("-", 1)[0]


def _delta(end: float, start: float | None) -> float:
    """Counter growth; a restarted process starts again at 0, then the end value is all."""
    if start is None or end < start:
        return end
    return end - start


def publisher_totals(start: Mapping, end: Mapping) -> dict[str, dict[str, int]]:
    """Per domain (and ``all``): growth of each device counter during the window."""
    before = {d: c for w in start.get("workers", {}).values() for d, c in w["devices"].items()}
    totals: dict[str, dict[str, int]] = defaultdict(lambda: dict.fromkeys(PUBLISHER_FIELDS, 0))
    for worker in end.get("workers", {}).values():
        for device_id, counters in worker["devices"].items():
            previous = before.get(device_id, {})
            for name in PUBLISHER_FIELDS:
                grown = _delta(counters[name], previous.get(name))
                totals[domain_of(device_id)][name] += grown
                totals["all"][name] += grown
    # Counters are interpolated at the window's edges: whole messages in the totals.
    return {d: {k: round(v) for k, v in c.items()} for d, c in totals.items()}


def _matches(topic_filter: str, domain: str) -> bool:
    parts = topic_filter.split("/")
    # monitoring/v1/# (every domain) or monitoring/v1/<domain>/#
    return len(parts) >= 3 and parts[2] in ("#", "+", domain)


def monitor_delivery(
    start: Mapping, end: Mapping, published: Mapping[str, Mapping[str, int]]
) -> dict[str, Any]:
    """The monitors (``monitor-*``): what they received against what they should have."""
    received = duplicates = expected = 0
    count = 0
    for client_id, now in end.get("monitors", {}).items():
        if not client_id.startswith("monitor-"):
            continue
        before = start.get("monitors", {}).get(client_id, {})
        count += 1
        received += _delta(now["received"], before.get("received"))
        duplicates += _delta(now["duplicates"], before.get("duplicates"))
        expected += sum(
            totals["published"]
            for domain, totals in published.items()
            if domain != "all" and domain != "smoke" and _matches(now["filter"], domain)
        )
    received, duplicates, expected = round(received), round(duplicates), round(expected)
    unique = received - duplicates
    return {
        "monitors": count,
        "monitor_received": received,
        "monitor_duplicates": duplicates,
        "monitor_expected": expected,
        "monitor_delivery": round(unique / expected, 5) if expected else None,
    }


def ingestion_counters(start: Mapping, after: Mapping) -> dict[str, Any]:
    """The ingestion consumer during the window (and until it settled)."""
    before = start.get("monitors", {}).get("ingestion") or {}
    now = after.get("monitors", {}).get("ingestion") or {}
    if not now:
        return {"ingestion_duplicates": None, "ingestion_dropped": None}
    d0, d1 = before.get("delivery") or {}, now.get("delivery") or {}
    return {
        "ingestion_duplicates": _delta(now["duplicates"], before.get("duplicates")),
        "ingestion_dropped": _delta(d1.get("dropped", 0), d0.get("dropped")),
        "ingestion_api_errors": _delta(d1.get("api_errors", 0), d0.get("api_errors")),
    }


def broker_rates(samples: list[Mapping[str, float]]) -> dict[str, Any]:
    """Broker throughput from its cumulative counters, over the window."""
    rates: dict[str, Any] = {}
    for name in (
        "messages_received",
        "messages_sent",
        "bytes_received",
        "bytes_sent",
        "publish_dropped",
    ):
        points = [(s["t"], s[name]) for s in samples if name in s]
        if len(points) >= 2 and points[-1][0] > points[0][0]:
            # Sum of increments: a restarted broker starts its counters again at 0.
            grown = sum(b - a if b >= a else b for (_, a), (_, b) in itertools.pairwise(points))
            rates[f"broker_{name}_per_s"] = round(grown / (points[-1][0] - points[0][0]), 3)
        else:
            rates[f"broker_{name}_per_s"] = None
    clients = [s["clients_connected"] for s in samples if "clients_connected" in s]
    rates["broker_clients_max"] = int(max(clients)) if clients else None
    return rates


def storage_metrics(window: Mapping | None, duration: float) -> dict[str, Any]:
    if not window:
        return {"stored": None}
    stored, gaps = window["measurements"], window["sequence_gaps"]
    mqtt, total = window["generated_to_received_ms"], window["generated_to_stored_ms"]
    return {
        "stored": stored,
        "sequence_gaps": gaps,
        "storage_delivery": round(stored / (stored + gaps), 5) if stored + gaps else None,
        "stored_per_s": round(stored / duration, 3),
        "latency_mqtt_p50_ms": mqtt["p50"],
        "latency_mqtt_p95_ms": mqtt["p95"],
        "latency_mqtt_p99_ms": mqtt["p99"],
        "latency_storage_p50_ms": total["p50"],
        "latency_storage_p95_ms": total["p95"],
        "latency_storage_p99_ms": total["p99"],
        "latency_storage_max_ms": total["max"],
        "alerts_raised": sum(window["alerts_raised"].values()),
    }


def role_of(service: str) -> str | None:
    for role, prefixes in ROLE_PREFIXES.items():
        if any(service == p or (p.endswith("-") and service.startswith(p)) for p in prefixes):
            return role
    return None


def resource_metrics(samples: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """CPU (% of one core) and memory (MiB) per role: average and peak of the per-sample
    sums over the role's containers."""
    by_time: dict[tuple[str, float], dict[str, list[float]]] = defaultdict(
        lambda: {"cpu": [], "mem": []}
    )
    for s in samples:
        role = role_of(s["service"])
        if role is None:
            continue
        slot = by_time[(role, round(s["t"]))]
        slot["cpu"].append(s["cpu_percent"])
        slot["mem"].append(s["mem_mib"])
    per_role: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"cpu": [], "mem": []})
    for (role, _), slot in by_time.items():
        per_role[role]["cpu"].append(sum(slot["cpu"]))
        per_role[role]["mem"].append(sum(slot["mem"]))
    metrics: dict[str, Any] = {}
    for role in ROLE_PREFIXES:
        values = per_role.get(role)
        if not values:
            continue
        metrics[f"cpu_{role}_avg"] = round(sum(values["cpu"]) / len(values["cpu"]), 2)
        metrics[f"cpu_{role}_max"] = round(max(values["cpu"]), 2)
        metrics[f"mem_{role}_avg_mib"] = round(sum(values["mem"]) / len(values["mem"]), 1)
        metrics[f"mem_{role}_max_mib"] = round(max(values["mem"]), 1)
    return metrics


def recovery(
    arrivals: Mapping[str, list[float]], disrupted_at: float, restored_at: float
) -> dict[str, Any]:
    """Devices delivering before ``disrupted_at``: how long after ``restored_at`` the last
    of them delivered again (None if one never did), and the longest silence."""
    active = [
        d
        for d, times in arrivals.items()
        if any(disrupted_at - ACTIVE_BEFORE_SECONDS <= t < disrupted_at for t in times)
    ]
    if not active:
        return {"recovery_s": None, "silence_max_s": None, "devices_affected": 0}
    back: list[float] = []
    silences: list[float] = []
    for device in active:
        times = sorted(arrivals[device])
        before = max(t for t in times if t < disrupted_at)
        after = [t for t in times if t >= restored_at]
        if not after:
            return {"recovery_s": None, "silence_max_s": None, "devices_affected": len(active)}
        back.append(after[0] - restored_at)
        silences.append(after[0] - before)
    return {
        "recovery_s": round(max(back), 3),
        "silence_max_s": round(max(silences), 3),
        "devices_affected": len(active),
    }


def run_record(
    result: Mapping[str, Any],
    *,
    duration: float,
    resources: Iterable[Mapping[str, Any]] = (),
    actions: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """One flat row per run (CSV-friendly)."""
    start, end = result["start"], result["end"]
    after = result.get("after") or end
    published = publisher_totals(start, end)
    everything = published.get("all", dict.fromkeys(PUBLISHER_FIELDS, 0))
    record: dict[str, Any] = {
        "run_id": result["run_id"],
        "since": result["since"],
        "until": result["until"],
        "duration_s": duration,
        "devices": sum(len(w["devices"]) for w in end.get("workers", {}).values()),
        "settled": result.get("settled"),
        "generated": everything["generated"],
        "published": everything["published"],
        "acked": everything["acked"],
        "failed": everything["failed"],
        "published_per_s": round(everything["published"] / duration, 3),
        "acked_ratio": (
            round(everything["acked"] / everything["published"], 5)
            if everything["published"]
            else None
        ),
    }
    record.update(monitor_delivery(start, end, published))
    record.update(ingestion_counters(start, after))
    record.update(storage_metrics(result.get("window"), duration))
    record.update(broker_rates(result.get("broker") or []))
    record.update(resource_metrics(resources))
    arrivals = result.get("arrivals")
    for i, action in enumerate(actions, start=1):
        record[f"action{i}"] = action["describe"]
        if arrivals is not None:
            outcome = recovery(arrivals, action["started"], action["restored"])
            for key, value in outcome.items():
                record[f"action{i}_{key}"] = value
    return record


def saturation_reasons(
    records: list[Mapping[str, Any]], min_delivery: float, max_p95_ms: float
) -> list[str]:
    """Why a sweep point no longer keeps up (empty: it does). Uses the worst repetition."""
    reasons = []
    for key in ("storage_delivery", "monitor_delivery"):
        values = [r[key] for r in records if r.get(key) is not None]
        if values and min(values) < min_delivery:
            reasons.append(f"{key} {min(values):.4f} < {min_delivery}")
    p95 = [r["latency_storage_p95_ms"] for r in records if r.get("latency_storage_p95_ms")]
    if p95 and max(p95) > max_p95_ms:
        reasons.append(f"latency_storage_p95_ms {max(p95):.0f} > {max_p95_ms:g}")
    if any(r.get("containers_failed") for r in records):
        reasons.append("containers exited or were killed for lack of memory")
    if any(r.get("probe_error") for r in records):
        reasons.append("the probe could not measure the run")
    return reasons


def parse_time(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()
