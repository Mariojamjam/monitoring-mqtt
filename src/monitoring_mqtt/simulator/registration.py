"""Worker id claims through the broker (see ``core/registry.py`` for the contract).

Protocol, over one dedicated control connection per claimed id:

1. connect with a Last Will that clears the claim (empty retained payload);
2. subscribe to the claim topic and wait briefly for a retained claim;
3. a claim from another worker means the id is taken: pick another id;
4. otherwise publish our retained claim and keep listening for a short settle time, so
   two workers that start at the same moment see each other;
5. while running, a conflicting claim is resolved by precedence (earlier start wins);
   the loser stops, and its container restarts and claims a new id.

The broker only stores and routes the retained claims; the decision is made by workers.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import replace
from enum import StrEnum

import paho.mqtt.client as mqtt
from pydantic import ValidationError

from monitoring_mqtt.core import mqtt as mqtt_helpers
from monitoring_mqtt.core.config import MqttSettings
from monitoring_mqtt.core.identity import HOST_ID_LENGTH
from monitoring_mqtt.core.logging import kv
from monitoring_mqtt.core.registry import CLAIM_QOS, WorkerClaim

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 10.0
SETTLE_SECONDS = 1.0
MAX_ATTEMPTS = 5
RELEASE_TIMEOUT_SECONDS = 2.0


class Decision(StrEnum):
    IGNORE = "ignore"
    YIELD = "yield"  # the other worker keeps the id
    DEFEND = "defend"  # we keep the id and publish our claim again


class ClaimOutcome(StrEnum):
    CLAIMED = "claimed"
    TAKEN = "taken"
    UNVERIFIED = "unverified"  # broker unreachable; the claim is made once it connects


class WorkerIdUnavailableError(RuntimeError):
    """No free worker id was found."""


def resolve_conflict(mine: WorkerClaim, other: WorkerClaim, published: bool) -> Decision:
    """What to do when ``other`` arrives on our claim topic."""
    if other.instance_id == mine.instance_id:
        return Decision.IGNORE  # our own claim
    if not published:
        # Same hostname: a previous run of this container left its claim; ours replaces it.
        return Decision.IGNORE if other.hostname == mine.hostname else Decision.YIELD
    return Decision.DEFEND if mine.precedence < other.precedence else Decision.YIELD


def random_worker_id() -> str:
    return secrets.token_hex(HOST_ID_LENGTH // 2)


class Registration:
    """Holds one worker id claim over its own MQTT connection."""

    def __init__(
        self, claim: WorkerClaim, settings: MqttSettings, client: mqtt.Client | None = None
    ) -> None:
        self.claim = claim
        # Control connection: never leave a persistent session behind.
        self._settings = replace(settings, clean_session=True)
        self.conflict: WorkerClaim | None = None
        self._lock = threading.Lock()
        self._subscribed = threading.Event()
        self._taken = threading.Event()
        self._published = False
        self._publish_on_connect = False
        self._on_lost: Callable[[WorkerClaim], None] | None = None
        self.client = client or mqtt_helpers.create_client(
            f"registry-{claim.instance_id.hex[:12]}", self._settings, self._on_connected
        )
        self.client.will_set(claim.topic, None, qos=CLAIM_QOS, retain=True)
        self.client.on_subscribe = self._on_subscribe
        self.client.on_message = self._on_message

    @property
    def worker_id(self) -> str:
        return self.claim.worker_id

    # Network-thread callbacks -------------------------------------------------------

    def _on_connected(self, client: mqtt.Client) -> None:
        client.subscribe(self.claim.topic, qos=CLAIM_QOS)
        with self._lock:
            republish = (self._published or self._publish_on_connect) and not self._taken.is_set()
        if republish:
            # Late first connection, or the broker restarted and lost retained claims.
            self._publish()

    def _on_subscribe(self, client, userdata, mid, reason_codes, properties) -> None:
        if not any(code.is_failure for code in reason_codes):
            self._subscribed.set()

    def _on_message(self, client, userdata, message: mqtt.MQTTMessage) -> None:
        try:
            self.handle_payload(message.payload)
        except Exception:
            logger.exception("worker claim handling failed %s", kv(worker_id=self.worker_id))

    def handle_payload(self, payload: bytes) -> None:
        if not payload or self._taken.is_set():
            return  # cleared claim, or we already gave the id up
        try:
            other = WorkerClaim.from_payload(payload)
        except ValidationError:
            logger.warning("invalid worker claim ignored %s", kv(worker_id=self.worker_id))
            return
        with self._lock:
            published = self._published
            decision = resolve_conflict(self.claim, other, published)
            if decision is Decision.YIELD:
                self.conflict = other
                self._published = False
                self._taken.set()
        fields = kv(worker_id=self.worker_id, other_hostname=other.hostname)
        if decision is Decision.DEFEND:
            logger.warning("worker id conflict, keeping it (earlier claim) %s", fields)
            self._publish()
        elif decision is Decision.YIELD and published:
            logger.error("worker id conflict, giving it up (later claim) %s", fields)
            with self._lock:
                on_lost = self._on_lost
            if on_lost is not None:
                on_lost(other)

    def _publish(self) -> None:
        with self._lock:
            self._published = True
        self.client.publish(self.claim.topic, self.claim.to_payload(), qos=CLAIM_QOS, retain=True)

    # Main-thread API ------------------------------------------------------------------

    def acquire(
        self, connect_timeout: float = CONNECT_TIMEOUT_SECONDS, settle: float = SETTLE_SECONDS
    ) -> ClaimOutcome:
        """Try to claim the id; blocks for about ``2 x settle`` once connected."""
        mqtt_helpers.start(self.client, self._settings)
        if not self._subscribed.wait(connect_timeout):
            with self._lock:
                self._publish_on_connect = True
            if not self._subscribed.is_set():
                return ClaimOutcome.UNVERIFIED
            self._publish()  # connected right after the timeout; claim now
            return ClaimOutcome.UNVERIFIED
        if self._taken.wait(settle):  # a retained claim from another worker
            return ClaimOutcome.TAKEN
        self._publish()
        if self._taken.wait(settle):  # another worker claimed at the same moment and won
            return ClaimOutcome.TAKEN
        return ClaimOutcome.CLAIMED

    def watch(self, on_lost: Callable[[WorkerClaim], None]) -> None:
        """Call ``on_lost`` if another worker wins this id later."""
        with self._lock:
            self._on_lost = on_lost
            lost = self._taken.is_set()
        if lost and self.conflict is not None:
            on_lost(self.conflict)

    def stop(self) -> None:
        """Close the control connection without clearing the claim."""
        mqtt_helpers.stop(self.client)

    def release(self, timeout: float = RELEASE_TIMEOUT_SECONDS) -> None:
        """Clear our claim (clean shutdown) and close the control connection."""
        with self._lock:
            ours = self._published and not self._taken.is_set()
        if ours and self.client.is_connected():
            info = self.client.publish(self.claim.topic, None, qos=CLAIM_QOS, retain=True)
            try:
                info.wait_for_publish(timeout)
            except (RuntimeError, ValueError):
                logger.warning("worker claim not cleared %s", kv(worker_id=self.worker_id))
        self.stop()


def acquire_worker_id(
    initial_id: str,
    make_registration: Callable[[str], Registration],
    attempts: int = MAX_ATTEMPTS,
    new_id: Callable[[], str] = random_worker_id,
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
    settle: float = SETTLE_SECONDS,
) -> tuple[Registration, ClaimOutcome]:
    """Claim ``initial_id``, or random ids while it is taken by other workers."""
    worker_id = initial_id
    for _ in range(attempts):
        registration = make_registration(worker_id)
        outcome = registration.acquire(connect_timeout, settle)
        if outcome is ClaimOutcome.CLAIMED:
            logger.info("worker id claimed %s", kv(worker_id=worker_id))
            return registration, outcome
        if outcome is ClaimOutcome.UNVERIFIED:
            logger.warning(
                "worker id not verified, broker unreachable; claiming when it connects %s",
                kv(worker_id=worker_id),
            )
            return registration, outcome
        registration.stop()
        other = registration.conflict
        replacement = new_id()
        logger.warning(
            "worker id already claimed, switching %s",
            kv(
                worker_id=worker_id,
                claimed_by=other.hostname if other else "?",
                new_worker_id=replacement,
            ),
        )
        worker_id = replacement
    raise WorkerIdUnavailableError(f"no free worker id after {attempts} attempts")
