"""Interest filtering is done by the broker; every device and replica is its own client."""

from harness import Observer


def domains_of(observer: Observer, kind: str) -> set[str]:
    monitors = observer.online_monitors(kind)
    return {d for status in monitors.values() if status.counters
            for d, n in status.counters.by_domain.items() if n}  # fmt: skip


def test_every_device_is_its_own_client(observer: Observer) -> None:
    devices = observer.devices()
    assert len(devices) == 5  # 2 oxygen probes in one worker, 2 ammonia workers, 1 bolus
    poultry_workers = {d.split("-")[1] for d in observer.devices("poultry")}
    assert len(poultry_workers) == 2  # two worker containers, two different worker ids
    assert len(observer.present_claims()) == 4  # one claim per worker container


def test_each_monitor_receives_only_its_domain(observer: Observer) -> None:
    for domain in ("aquaculture", "poultry", "cattle"):
        observer.wait_until(lambda d=domain: domains_of(observer, d) == {d}, f"{domain} only")
    observer.wait_until(
        lambda: domains_of(observer, "all") == {"aquaculture", "poultry", "cattle"},
        "monitor-all receives every domain",
    )


def test_every_replica_gets_its_own_copy(observer: Observer) -> None:
    def shared_event_ids() -> set[str]:
        replicas = list(observer.online_monitors("poultry").values())
        if len(replicas) != 2 or not all(r.counters for r in replicas):
            return set()
        ids = [{str(m.event_id) for m in r.counters.recent} for r in replicas]  # type: ignore[union-attr]
        return ids[0] & ids[1]

    # Both poultry replicas report the same messages: fan-out, not load sharing.
    observer.wait_until(shared_event_ids, "both poultry replicas received the same event ids")
