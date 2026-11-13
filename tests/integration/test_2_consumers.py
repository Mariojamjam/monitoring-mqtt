"""An invalid message is counted and logged; the monitor keeps consuming."""

import json

from harness import Observer

TOPIC = "monitoring/v1/poultry/ammonia/poultry-fake01-001"


def poultry_counts(observer: Observer) -> dict[str, tuple[int, int]]:
    return {
        k: (s.counters.received, s.counters.invalid)
        for k, s in observer.online_monitors("poultry").items()
        if s.counters
    }


def test_invalid_payloads_do_not_stop_the_monitor(observer: Observer) -> None:
    before = poultry_counts(observer)
    assert len(before) == 2
    wrong_topic = {
        "schema_version": 1, "event_id": "8e7c6a1e-61a4-4b8b-9a3e-0d1f6f0e3b11",
        "run_id": "it", "device_id": "cattle-fake01-001", "device_type": "rumen_ph_bolus",
        "domain": "cattle", "site_id": "herd-01", "metric": "rumen_ph", "value": 6.3,
        "unit": None, "sequence": 1, "timestamp": "2026-11-12T12:00:00Z",
    }  # fmt: skip
    for payload in (
        b"not json",
        json.dumps({"value": "high"}).encode(),  # schema error
        json.dumps(wrong_topic).encode(),  # payload of another device than the topic says
    ):
        observer.publish(TOPIC, payload)

    def every_replica_counted_three_and_kept_receiving() -> bool:
        now = poultry_counts(observer)
        return len(now) == 2 and all(
            now[k][1] - before[k][1] == 3 and now[k][0] > before[k][0] for k in before
        )

    observer.wait_until(every_replica_counted_three_and_kept_receiving,
                        "3 invalid messages counted by each poultry replica")  # fmt: skip
