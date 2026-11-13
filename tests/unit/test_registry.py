from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from monitoring_mqtt.cli.stack import parse_claims
from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.core.registry import WORKER_CLAIMS_FILTER, WorkerClaim, worker_claim_topic
from monitoring_mqtt.core.topics import ALL_TELEMETRY_FILTER, matches
from monitoring_mqtt.simulator import registration as reg
from monitoring_mqtt.simulator.registration import (
    ClaimOutcome,
    Decision,
    Registration,
    WorkerIdUnavailableError,
    acquire_worker_id,
    resolve_conflict,
)

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
OK = [SimpleNamespace(is_failure=False)]


def claim(worker_id: str = "abc123", hostname: str = "abc123def456", **fields) -> WorkerClaim:
    return WorkerClaim.create(
        worker_id=worker_id,
        hostname=hostname,
        run_id="run-1",
        device_types=["oxygen_probe"],
        devices=2,
        **{"started_at": T0, **fields},
    )


# Contract ---------------------------------------------------------------------------


def test_claim_topic_is_outside_telemetry() -> None:
    topic = worker_claim_topic("abc123")
    assert topic == "monitoring/registry/v1/workers/abc123"
    assert matches(WORKER_CLAIMS_FILTER, topic)
    assert not matches(ALL_TELEMETRY_FILTER, topic)  # monitors never see claims


def test_claim_round_trip_and_validation() -> None:
    original = claim()
    assert WorkerClaim.from_payload(original.to_payload()) == original
    with pytest.raises(ValidationError):
        claim(worker_id="Bad/Id")


def test_earlier_start_has_precedence() -> None:
    early, late = claim(), claim(started_at=T0 + timedelta(seconds=1))
    assert early.precedence < late.precedence


def test_parse_claims_skips_noise() -> None:
    text = "Container starting\n" + claim().model_dump_json() + "\n{broken\n"
    assert [c.worker_id for c in parse_claims(text)] == ["abc123"]


# Conflict resolution ----------------------------------------------------------------


def test_resolve_conflict() -> None:
    mine = claim()
    other_host = claim(hostname="abc123ffffff", started_at=T0 + timedelta(seconds=5))
    previous_run = claim(started_at=T0 - timedelta(minutes=5))  # same hostname
    assert resolve_conflict(mine, mine, published=True) is Decision.IGNORE
    assert resolve_conflict(mine, other_host, published=False) is Decision.YIELD
    assert resolve_conflict(mine, previous_run, published=False) is Decision.IGNORE
    assert resolve_conflict(mine, other_host, published=True) is Decision.DEFEND
    later_mine = claim(started_at=T0 + timedelta(seconds=9))
    assert resolve_conflict(later_mine, other_host, published=True) is Decision.YIELD


# Protocol with a fake client --------------------------------------------------------


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes | None, int, bool]] = []
        self.subscriptions: list[str] = []
        self.will = None
        self.connected = True
        self.on_publish_hook = None

    def will_set(self, topic, payload, qos, retain) -> None:
        self.will = (topic, payload, qos, retain)

    def subscribe(self, topic, qos) -> None:
        self.subscriptions.append(topic)

    def publish(self, topic, payload, qos, retain):
        self.published.append((topic, payload, qos, retain))
        if self.on_publish_hook:
            self.on_publish_hook()
        return SimpleNamespace(wait_for_publish=lambda timeout: None)

    def is_connected(self) -> bool:
        return self.connected


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch):
    """Patch start/stop: start simulates a connection (or none) and optional retained claims."""
    state = SimpleNamespace(reachable=True, retained=[], stopped=[])

    def start(client, settings) -> None:
        registration = client.owner
        if not state.reachable:
            return
        registration._on_connected(client)
        registration._on_subscribe(client, None, 1, OK, None)
        for payload in state.retained:
            registration.handle_payload(payload)

    monkeypatch.setattr(reg.mqtt_helpers, "start", start)
    monkeypatch.setattr(reg.mqtt_helpers, "stop", lambda client: state.stopped.append(client))
    return state


def make(worker_id: str = "abc123", **fields) -> Registration:
    client = FakeClient()
    registration = Registration(claim(worker_id, **fields), MqttSettings(), client=client)
    client.owner = registration
    return registration


def claims_published(registration: Registration) -> list[bytes | None]:
    return [payload for _, payload, _, _ in registration.client.published]


def test_free_id_is_claimed_with_retained_claim_and_last_will(broker) -> None:
    registration = make()
    assert registration.acquire(settle=0.01) is ClaimOutcome.CLAIMED
    topic, payload, qos, retain = registration.client.published[-1]
    assert topic == worker_claim_topic("abc123") and qos == 1 and retain
    assert WorkerClaim.from_payload(payload) == registration.claim
    assert registration.client.will == (topic, None, 1, True)  # broker clears it on loss


def test_id_claimed_by_another_worker_is_taken(broker) -> None:
    broker.retained = [
        claim(hostname="abc123ffffff", started_at=T0 - timedelta(hours=1)).to_payload()
    ]
    registration = make()
    assert registration.acquire(settle=0.01) is ClaimOutcome.TAKEN
    assert registration.client.published == []
    assert registration.conflict.hostname == "abc123ffffff"


def test_previous_claim_of_the_same_container_is_replaced(broker) -> None:
    broker.retained = [claim(started_at=T0 - timedelta(hours=1)).to_payload()]
    assert make().acquire(settle=0.01) is ClaimOutcome.CLAIMED


def test_simultaneous_claim_lost_during_settle(broker) -> None:
    registration = make(started_at=T0 + timedelta(seconds=1))
    earlier = claim(hostname="abc123ffffff").to_payload()
    registration.client.on_publish_hook = lambda: registration.handle_payload(earlier)
    assert registration.acquire(settle=0.05) is ClaimOutcome.TAKEN


def test_late_conflict_winner_republishes_and_loser_is_notified(broker) -> None:
    winner = make()
    assert winner.acquire(settle=0.01) is ClaimOutcome.CLAIMED
    newcomer = claim(hostname="abc123ffffff", started_at=T0 + timedelta(seconds=30))
    winner.handle_payload(newcomer.to_payload())
    assert len(claims_published(winner)) == 2  # claim restored as the retained message

    loser = make(started_at=T0 + timedelta(seconds=30), hostname="abc123eeeeee")
    assert loser.acquire(settle=0.01) is ClaimOutcome.CLAIMED
    lost = []
    loser.watch(lost.append)
    loser.handle_payload(winner.claim.to_payload())
    assert [c.hostname for c in lost] == ["abc123def456"]
    loser.release()
    assert claims_published(loser)[-1] is not None  # the loser never clears the winner's claim


def test_unreachable_broker_claims_on_later_connection(broker) -> None:
    broker.reachable = False
    registration = make()
    assert registration.acquire(connect_timeout=0.01, settle=0.01) is ClaimOutcome.UNVERIFIED
    assert registration.client.published == []
    registration._on_connected(registration.client)
    assert WorkerClaim.from_payload(claims_published(registration)[-1]) == registration.claim


def test_claim_is_restored_after_broker_restart(broker) -> None:
    registration = make()
    registration.acquire(settle=0.01)
    registration._on_connected(registration.client)  # reconnect: retained claims were lost
    assert len(claims_published(registration)) == 2


def test_release_clears_own_claim(broker) -> None:
    registration = make()
    registration.acquire(settle=0.01)
    registration.release()
    assert claims_published(registration)[-1] is None
    assert broker.stopped == [registration.client]


def test_acquire_worker_id_switches_to_a_free_id(broker) -> None:
    taken = claim(hostname="abc123ffffff", started_at=T0 - timedelta(hours=1)).to_payload()
    broker.retained = [taken]
    made: list[str] = []

    def factory(worker_id: str) -> Registration:
        made.append(worker_id)
        if worker_id != "abc123":
            broker.retained = []
        return make(worker_id)

    registration, outcome = acquire_worker_id(
        "abc123", factory, new_id=lambda: "f00baa", settle=0.01
    )
    assert outcome is ClaimOutcome.CLAIMED
    assert registration.worker_id == "f00baa"
    assert made == ["abc123", "f00baa"]


def test_acquire_worker_id_gives_up(broker) -> None:
    broker.retained = [claim(hostname="zzz", started_at=T0 - timedelta(hours=1)).to_payload()]
    with pytest.raises(WorkerIdUnavailableError):
        acquire_worker_id("abc123", make, attempts=2, new_id=lambda: "abc123", settle=0.01)
