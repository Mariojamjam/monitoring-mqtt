# Failures and recovery

This page shows how to provoke each failure, how it is detected and what happens to the
data. The observations below come from real runs of the default experiment on one machine
(QoS 1, clean sessions, keepalive 30 s, one message every 5 s per device, reconnect backoff
1–30 s). Times depend on the machine and on when the last keepalive packet was sent, so
treat them as examples, not guarantees.

Follow any scenario live with `monitoring-mqtt tui` in another terminal (see
[tui.md](tui.md#scenarios)).

## Summary

| Failure | How to provoke it | Detected by | Detection time | Data |
|---|---|---|---|---|
| [Monitor stops](#monitor-stops-or-crashes) | `docker stop monitoring-mqtt-monitor-poultry-1` | Its own clean disconnect | Immediate | Publishers unaffected; the monitor misses what was published while it was down |
| [Monitor crashes](#monitor-stops-or-crashes) | `docker kill monitoring-mqtt-monitor-cattle-1` | The broker: TCP connection closed | Immediate | Same as above; status `offline` (Last Will) |
| [Worker stops or crashes](#worker-stops-or-crashes) | `docker stop` / `docker kill monitoring-mqtt-device-poultry-1` | The broker | Immediate | All its devices stop; `sequence` restarts at 1 when it comes back |
| [Broker restarts](#broker-restarts) | `monitoring-mqtt stack restart broker` | Every client | Immediate | About 1 s without a broker; a measurement due in that second is dropped |
| [Network cut](#network-cut-half-open-connection) (cable, Wi-Fi, firewall) | `docker network disconnect monitoring-mqtt_default <container>` | Keepalive | Up to 1.5 x keepalive (45 s) | Worker: buffered and delivered late. Monitor: messages lost |
| [API, database or ingestion down](#storage-path-api-database-ingestion-consumer) | `docker stop monitoring-mqtt-api-1` | The ingestion consumer (failed requests, 503) | Immediate | Buffered or queued by the broker; stored without gaps when it returns |
| [One device inside a worker](#one-device-inside-a-worker) | `monitoring-mqtt device stop <device_id> --for 30s` | Its own clean disconnect | Immediate | Only that device: silence, no gap. Its neighbours keep publishing |

## Monitor stops or crashes

```bash
docker stop monitoring-mqtt-monitor-poultry-1     # clean stop (SIGTERM)
docker kill monitoring-mqtt-monitor-cattle-1      # crash (SIGKILL)
docker start monitoring-mqtt-monitor-poultry-1    # bring it back
```

Observed:

- **Clean stop:** the monitor published `stopped`, then removed its status. **Crash:** the
  broker published the monitor's Last Will at once, so the status became `offline`. A
  killed process cannot send anything, but the kernel closes its TCP connection, and the
  broker sees that immediately.
- The devices kept publishing with no gap in their sequence. Publishers do not know who
  consumes (invariant: devices publish fine with zero subscribers).
- With clean sessions, the broker keeps nothing for a disconnected monitor. After
  `docker start`, the monitor subscribed again and received only new messages. With
  `mqtt.clean_session: false`, the broker queues QoS 1 messages for it and delivers them
  when it comes back (see the Limitations in the README).

## Worker stops or crashes

```bash
docker kill monitoring-mqtt-device-poultry-1
docker start monitoring-mqtt-device-poultry-1
```

Observed:

- The worker's registry claim was removed: by the worker itself on `docker stop`, by the
  broker (Last Will) on `docker kill`.
- Every device in the container stopped publishing. This is the failure granularity of a
  worker: one container, all its devices.
- On `docker start`, the worker claimed **the same worker id** about 2 s after starting
  (the registry check and settle time). Its hostname is the same, so it replaces its
  previous claim and its devices started again with
  `sequence` 1. Consumers see this as a restart, not as lost messages.

## Broker restarts

```bash
monitoring-mqtt stack restart broker
```

Observed:

- Every client lost its connection at the same instant. The broker had about 1 s of
  downtime. While shutting down, it published the Last Will of every client, so for that
  second all monitors appeared `offline` and all workers left the registry.
- All clients reconnected 1 s later (the first reconnect delay). Monitors subscribed
  again, workers claimed their ids again, and monitors published `online` again.
- One device had a publication due during that second. The device knew it was
  disconnected, so it dropped the measurement and logged
  `measurement dropped, broker unavailable`. Consumers see a gap of one in its `sequence`.
  The other devices lost nothing.

The broker is a single point of failure. While it is down, nothing is delivered.

## Network cut (half-open connection)

When the network disappears without closing the connection, neither side is told.
Detection then depends on the MQTT keepalive: a side that hears nothing for 1.5 x
keepalive (45 s) closes the connection.

```bash
docker network disconnect monitoring-mqtt_default monitoring-mqtt-device-poultry-1
docker network connect monitoring-mqtt_default monitoring-mqtt-device-poultry-1
```

The worker and a monitor were cut at the same time. The worker's network came back after
20 s, and the monitor's after 65 s.

| | Worker (`device-poultry`) | Monitor (`monitor-aquaculture`) |
|---|---|---|
| Broker detects it (Last Will) | 32 s | 29 s |
| Client detects it (`Keep alive timeout`) | 46–49 s | 47 s |
| Reconnected | 1 s after detecting it | 13 s after its network came back (backoff had grown) |
| Messages | **None lost**: 10 measurements delivered in a burst on reconnection, with latency from 3 s to 48 s | **16 lost** (80 s of telemetry); it resumed with new messages only |

Why the two sides differ:

- **Worker:** the device did not know it was disconnected, so it kept publishing. Paho
  kept the unacknowledged QoS 1 messages and sent them again after it reconnected. This
  is at-least-once delivery working: late, but complete. When the device *knows* it is
  disconnected (the broker restart above), it drops the measurement instead.
- **Monitor:** the broker ended the monitor's clean session when it detected the cut, and
  discarded what had been routed to it.

The old TCP connection does not recover even when the network comes back quickly. The
client still has to time out and reconnect.

## Storage path: API, database, ingestion consumer

```bash
docker stop monitoring-mqtt-api-1        # or -postgres-1, -ingestion-1
docker start monitoring-mqtt-api-1
```

Default experiment, each stopped for 30 s (three devices, 18 measurements expected):

| Stopped | What keeps the events | Stored | Gaps | Longest delay to the database |
|---|---|---|---|---|
| `api` | the ingestion consumer's buffer (retries with backoff) | 21 | 0 | 34.8 s |
| `postgres` | the same buffer: the API answers 503 meanwhile | 18 | 0 | 28.1 s |
| `ingestion` | the broker: persistent session, queued QoS 1 messages | 18 | 0 | 31.1 s |

Everything published during the outage was stored, in order, within 4 s of the restart.
An unplanned 5-minute API outage (189 measurements buffered) ended the same way: no gap.
Devices, monitors and the alert engine were not affected by any of them. Limits: what the
ingestion buffer holds is lost if its process dies, and the broker queues at most 1000
messages per consumer. Details in [storage.md](storage.md).

Use `docker stop/start` on the container here. `docker compose start <service>` would also
start the service's dependencies, such as `migrate`.

## Restart policy

Worker, monitor and broker containers use `restart: unless-stopped`.

- If the process exits by itself, Docker restarts the container. An example is a worker that
  loses its id to another worker and exits with code 3.
- `docker stop` and `docker kill` count as manual stops. The container stays down until
  `docker start` or the next `monitoring-mqtt stack up`.

`monitoring-mqtt stack restart [SERVICE...]` restarts containers as they are, with the
same run id and settings. To apply a changed `config/simulation.yaml`, run `stack up`;
this starts a new run.

## One device inside a worker

```bash
monitoring-mqtt stack up -c config/examples/device-fault.yaml   # scheduled: 002 off, 003 disconnected
monitoring-mqtt device stop poultry-<worker>-002 --for 30s      # or live, by hand
```

Observed with `config/examples/device-fault.yaml` (one worker, three ammonia detectors):

| Device | Episode | Sequence received |
|---|---|---|
| `001` | none | 1 → 15, no gap: the neighbours are unaffected |
| `002` | `stop` at 30 s for 30 s | 1 → 8 with 30 s of silence and no gap: nothing is generated while off |
| `003` | `disconnect` at 40 s for 20 s | 1 → 8, then 13: measurements 9–12 were generated and lost |

Each device has its own MQTT connection, so one device can be stopped or disconnected
without touching the others. Every episode is announced on the simulation plane
([simulation.md](simulation.md#ground-truth)). The other episode kinds (pause, delay,
anomaly) and random instability are described in [simulation.md](simulation.md).
