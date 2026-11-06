# MQTT contract

Every service builds topics and payloads through the shared `monitoring_mqtt.core`
package (`core/topics.py` and `core/models.py`). The contract does not depend on any
particular container, interface or consumer.

## Telemetry topics

```text
monitoring/v1/{domain}/{metric}/{device_id}
```

| Segment | Example | Rule |
|---|---|---|
| `monitoring/v1` | — | Fixed root and contract version |
| `domain` | `aquaculture` | Production domain of the device |
| `metric` | `dissolved_oxygen` | Measured quantity |
| `device_id` | `aquaculture-3f9a1c-001` | Unique device identity |

Segments use lowercase letters, digits, `_` and `-`. They never contain `/`, `+` or `#`.

Example:

```text
monitoring/v1/aquaculture/dissolved_oxygen/aquaculture-3f9a1c-001
```

### Device identity

`device_id` is also the MQTT `client_id` of the device connection:

```text
{domain}-{worker_id}-{index:03d}
```

- `worker_id` comes from the `WORKER_ID` environment variable when it is set, which gives
  persistent identities for experiments. Otherwise it is the first 6 characters of the
  container hostname. Docker sets the hostname to the container ID, which differs between
  replicas.
- Before creating its devices, every worker claims its `worker_id` in the
  [worker registry](#worker-registry). If another worker, possibly on another machine,
  already holds that id, the worker switches to a random 6-character hexadecimal id.
- `index` is the device position inside its worker, starting at `001`.

### Device types

Device types are configured in `config/devices.yaml`:

| Type | Domain | Metric | Unit | Site prefix |
|---|---|---|---|---|
| `dissolved_oxygen_probe` | `aquaculture` | `dissolved_oxygen` | `mg/L` | `pond` |
| `ammonia_detector` | `poultry` | `ammonia` | `ppm` | `house` |
| `rumen_ph_bolus` | `cattle` | `rumen_ph` | `null` (dimensionless) | `herd` |

`site_id` numbers the devices of each type inside one worker container: `pond-01`,
`pond-02`, and so on. The numbering restarts in every container, so `site_id` is a label and
not an identity. `device_id` is always the unique identity.

## Subscription filters

| Subscriber | Filter | Service |
|---|---|---|
| Aquaculture monitor | `monitoring/v1/aquaculture/#` | `monitor-aquaculture` |
| Poultry monitor | `monitoring/v1/poultry/#` | `monitor-poultry` |
| Cattle monitor | `monitoring/v1/cattle/#` | `monitor-cattle` |
| Global monitor | `monitoring/v1/#` | `monitor-all` |
| Ammonia only, any domain | `monitoring/v1/+/ammonia/+` | (example filter, no service) |

Monitor client ids are `monitor-{kind}-{container short id}`, so they are unique per
replica.

The broker performs this filtering. A device publishes the same way whether or not any
subscriber is interested.

The domain `smoke` (`monitoring/v1/smoke/probe/smoke-<id>`) is reserved for the probe
messages of `monitoring-mqtt test smoke`. Domain monitors never subscribe to it. A global
subscriber (`monitoring/v1/#`) receives these probes and can tell them apart by their
domain.

## Payload (schema version 1)

UTF-8 JSON:

```json
{
  "schema_version": 1,
  "event_id": "f729c4df-9c18-42e8-b2ed-e67a9d647fd5",
  "run_id": "run-20261106T032058Z",
  "device_id": "aquaculture-3f9a1c-001",
  "device_type": "dissolved_oxygen_probe",
  "domain": "aquaculture",
  "site_id": "pond-01",
  "metric": "dissolved_oxygen",
  "value": 6.42,
  "unit": "mg/L",
  "sequence": 15,
  "timestamp": "2026-11-06T03:21:04.742000Z"
}
```

| Field | Type | Rule |
|---|---|---|
| `schema_version` | integer | Always `1` for this version |
| `event_id` | UUID | Unique per logical event. A retransmission of the same measurement keeps it |
| `run_id` | string | Identifies one simulation run. `stack up` generates one per run (`run-<UTC timestamp>`) and gives it to every worker, unless `config/simulation.yaml` sets it |
| `device_id` | segment | Device identity; must equal the last topic segment |
| `device_type` | string | Device profile, e.g. `dissolved_oxygen_probe` |
| `domain` | segment | Must equal the topic domain |
| `site_id` | string | Logical location: pond, house, animal group |
| `metric` | segment | Must equal the topic metric |
| `value` | number | Finite. `NaN` and infinity are rejected |
| `unit` | string or `null` | `null` for dimensionless metrics |
| `sequence` | integer ≥ 0 | Per-device counter within a run, see below |
| `timestamp` | ISO 8601 | Generation time, always normalized to UTC |

Unknown fields are rejected.

**Sequence gaps.** The counter increases for every generated measurement, including
measurements dropped because the broker was unreachable. A gap in `sequence` at a
consumer therefore shows a period when the device was offline or a message was lost.

## Validation by subscribers

A subscriber accepts a message only when:

1. the payload is valid JSON matching the schema above, and
2. `domain`, `metric` and `device_id` match the topic the message arrived on.

Invalid messages are logged with the reason and counted. They never stop the subscriber.

## Protocol settings

**Fixed by the v1 contract:**

| Parameter | Value |
|---|---|
| Protocol | MQTT 3.1.1 over TCP |
| Broker address (inside Compose) | `broker:1883` |
| Retain (telemetry) | `false` |
| Payload | UTF-8 JSON, schema version 1 |

**Experiment parameters** (`config/simulation.yaml`), with their defaults:

| Parameter | Key | Default |
|---|---|---|
| Publish / subscribe QoS | `mqtt.qos` | `1` |
| Clean session | `mqtt.clean_session` | `true` |
| Keepalive | `mqtt.keepalive_seconds` | 30 s |
| Reconnection backoff | `mqtt.reconnect.min_delay_seconds` / `max_delay_seconds` | 1 s to 30 s, exponential |
| Publish interval | `devices.publish_interval_seconds` | 5 s per device |

The smoke test always uses a clean session, whatever the experiment sets.

### What QoS 1 does and does not guarantee

- QoS 1 means **at-least-once** delivery on each MQTT hop (publisher to broker, broker to
  subscriber). Duplicates are possible, and consumers can detect them by `event_id`.
- A `PUBACK` only acknowledges the hop. It does not mean that any consumer processed or
  stored the event.
- Telemetry is not retained. With clean sessions (the default), a subscriber receives only
  messages published while it is connected and subscribed, and there is no history for late
  subscribers.
- With `clean_session: false`, the broker keeps the subscription of a disconnected client
  and queues its QoS 1 and QoS 2 messages. At most 1000 messages are queued per client, and
  the session expires after one hour of disconnection. The client receives the queued
  messages when it reconnects with the same client id. QoS 0 messages are not queued.

## Worker registry

Workers coordinate their ids through retained messages on the broker. The broker only
stores and routes them; the workers make every decision.

```text
monitoring/registry/v1/workers/{worker_id}
```

The registry lives outside `monitoring/v1/#`, so telemetry subscribers, including
`monitor-all`, never receive it. Claims always use QoS 1 and `retain=true`, whatever the
experiment uses for telemetry.

Claim payload (UTF-8 JSON):

```json
{
  "schema_version": 1,
  "worker_id": "3f9a1c",
  "instance_id": "6f1c2a8e-8a3d-4c55-9c1e-2b7b8f1d2a10",
  "hostname": "3f9a1c0d2e4b",
  "node": "pc-lab-1",
  "run_id": "run-20261106T120000Z",
  "device_types": ["dissolved_oxygen_probe"],
  "devices": 3,
  "started_at": "2026-11-06T12:00:00Z"
}
```

| Field | Meaning |
|---|---|
| `instance_id` | UUID generated at each process start |
| `hostname` | Container hostname; tells a restart of the same container from a different worker |
| `node` | Machine label (`NODE_NAME`); `null` when unknown |
| `started_at` | Process start in UTC; used to decide conflicts |

An empty retained payload on the topic removes a claim.

Protocol, over a dedicated control connection with client id `registry-{instance}`:

1. The worker connects with a **Last Will** that publishes an empty retained payload on its
   claim topic. If the connection is lost without a clean disconnect (crash, `SIGKILL`,
   keepalive timeout), the broker removes the claim.
2. It subscribes to its own claim topic and waits 1 s for a retained claim.
3. A claim from another hostname means the id is taken: the worker picks a random id and
   starts again from step 1, up to 5 attempts. A claim with its own hostname was left by a
   previous run of the same container, and the new claim replaces it.
4. Otherwise it publishes its retained claim and waits 1 s more, so two workers that start
   at the same moment see each other.
5. While running, a conflicting claim is decided by precedence: the earlier `started_at`
   wins, then the lower `instance_id`. The winner publishes its claim again. The loser stops
   with exit code `3`, and its container restarts and claims a new id.
6. On a clean shutdown, the worker publishes the empty retained payload.

When the broker cannot be reached at startup, the worker continues with its id unverified
and claims it as soon as it connects; any conflict is then decided by step 5. After a
broker restart, the retained claims are lost (persistence is off), and every worker
publishes its claim again when it reconnects.

`monitoring-mqtt stack workers` lists the current claims.

## Observability plane

MQTT has no way to ask the broker who subscribes to what. Subscribers therefore announce
their own status, using the usual MQTT presence pattern (retained status plus Last Will):

```text
monitoring/status/v1/monitors/{client_id}
```

Like the worker registry, this plane lives outside `monitoring/v1/#`, uses QoS 1 and is
meant for operators and observer tools. **Publishers never read it**: devices publish the
same way whether or not anyone consumes, so the data plane stays fully decoupled.

| Event | Published by | Payload | Retained |
|---|---|---|---|
| Connection, then every 5 s | the monitor | `state: online` with counters | yes |
| Connection lost without a clean disconnect | the broker (Last Will) | `state: offline`, no counters | yes |
| Clean shutdown (`SIGTERM`) | the monitor | `state: stopped` with final counters, then an empty payload that removes the status | no / empty retained |

Status payload (UTF-8 JSON):

```json
{
  "schema_version": 1,
  "client_id": "monitor-poultry-0279a8",
  "monitor": "poultry",
  "topic_filter": "monitoring/v1/poultry/#",
  "node": "pc-lab-2",
  "hostname": "0279a8f1c2d3",
  "state": "online",
  "started_at": "2026-11-06T12:00:00Z",
  "updated_at": "2026-11-06T12:05:00Z",
  "counters": {
    "received": 1532,
    "invalid": 2,
    "devices_seen": 20,
    "by_domain": {"poultry": 1532},
    "latency_ms": {"last": 1.8, "avg": 2.1, "p95": 3.4},
    "last_received_at": "2026-11-06T12:04:59.871Z",
    "recent": [
      {
        "topic": "monitoring/v1/poultry/ammonia/poultry-3f9a1c-001",
        "device_id": "poultry-3f9a1c-001",
        "value": 12.6,
        "unit": "ppm",
        "sequence": 42,
        "event_id": "9b2d7c1e-4f0a-4c8e-9a51-0d6e2f3b7a14",
        "received_at": "2026-11-06T12:04:59.871Z",
        "latency_ms": 1.8
      }
    ]
  }
}
```

- `node` is the machine label (`NODE_NAME`, set by the CLI to the hostname of the machine
  that started the stack). Worker claims carry the same field. It is `null` when unknown.
- `latency_ms` covers the latest 1000 messages. It is only meaningful when device and
  monitor clocks are synchronized.
- `recent` holds the latest 10 valid messages the monitor received, oldest first: what was
  actually delivered to this subscriber. It is refreshed with every status (every 5 s).
- An observer that subscribes to `monitoring/status/v1/monitors/+` receives the current
  status of every monitor at once (retained), then every change.

## Reserved topic families (not implemented yet)

```text
control/v1/{domain}/{device_id}/commands   # device commands
alerts/v1/{domain}/{severity}              # derived alerts
```
