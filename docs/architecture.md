# Architecture

Monitoring MQTT follows the **Publish/Subscribe** paradigm. Virtual devices publish
telemetry to topics on a central MQTT broker. Subscribers declare their interest through
topic filters, and the broker routes each message to every matching subscription.
Publishers and subscribers never communicate directly.

This document describes what runs today in detail. For the overall picture, how every
part communicates (MQTT, HTTP, SQL), see [design.md](design.md).

> This document describes what is currently implemented. It grows as components are added.

## Components

| Component | Status | Role |
|---|---|---|
| `monitoring-mqtt` CLI | Implemented | Operates the stack: diagnostics, topology-driven start/stop, status, logs, unit and smoke tests |
| `broker` service | Implemented | MQTT broker (Eclipse Mosquitto 2.1); routes messages by topic. Named by its role, so the implementation could be swapped without touching clients |
| `device-aquaculture`, `device-poultry`, `device-cattle` services | Implemented | Worker containers, one service per device type. Each runs a `SimulationWorker` hosting M virtual devices of that type, and each device has its own MQTT connection |
| `monitor-aquaculture`, `monitor-poultry`, `monitor-cattle` services | Implemented | Generic monitor, one service per domain, subscribed to `monitoring/v1/{domain}/#`; N replicas from the topology |
| `monitor-all` service | Implemented (0 replicas by default) | Same monitor code, subscribed to `monitoring/v1/#` |
| `smoke` service (profile `tools`) | Implemented | One-off end-to-end check started by `monitoring-mqtt test smoke`; never part of `stack up` |
| `postgres`, `migrate`, `api`, `ingestion` services (role `storage`) | Implemented | History: PostgreSQL (volume `pgdata`), schema migrations, the FastAPI API (single owner of the database) and the MQTT → API bridge ([storage.md](storage.md), [api.md](api.md)) |
| `alerts` service (role `alerts`) | Implemented | Alert engine: applies `config/alerts.yaml` to every measurement and publishes alert events on the broker ([alerts.md](alerts.md)) |
| `dashboard` service (role `dashboard`) | Implemented | Streamlit dashboard on port 8501: history, alerts and run comparison, read from the API only; its own image ([dashboard.md](dashboard.md)) |
| `control` service (profile `tools`) | Implemented | One-off live command to one device, started by `monitoring-mqtt device ...`; goes through the broker to the worker hosting the device |

## Configuration

```text
config/simulation.yaml ──┐   (experiment: versioned)
config/devices.yaml ─────┼──► monitoring-mqtt stack up ──► variables ──► compose.yaml ──► containers
.env ────────────────────┘   (machine: roles, broker address, host port, smoke timeout; not versioned)
```

The CLI validates `config/simulation.yaml` against the device catalog before calling
Docker. It generates one `run_id` per `stack up` (unless the file sets one) and passes
every parameter to Compose as environment variables (`RUN_ID`, `MQTT_QOS`,
`MQTT_CLEAN_SESSION`, `DEVICE_*_REPLICAS`, …), which are the interface between Compose and
the processes. Services read only environment variables. Their built-in defaults equal the
default experiment, so they also run outside Compose.

Every `stack up` is a new run, so the worker containers are recreated with the new
`run_id`, which also appears as the container label `monitoring-mqtt.run-id`. Monitors are
recreated only when their own settings change.

## Topology and scale model

For each device type, `config/simulation.yaml` sets two numbers:

```text
N = worker containers of that type     (replicas)
M = virtual devices in each container  (devices_per_worker)
Devices of the type = N x M = publisher MQTT connections of that type
```

Example with oxygen at `{replicas: 2, devices_per_worker: 3}` and ammonia at
`{replicas: 3, devices_per_worker: 1}`:

```text
device-aquaculture (container 185f5a)   device-aquaculture (container 47fc92)
 ├─ aquaculture-185f5a-001 ●             ├─ aquaculture-47fc92-001 ●
 ├─ aquaculture-185f5a-002 ●             ├─ aquaculture-47fc92-002 ●
 └─ aquaculture-185f5a-003 ●             └─ aquaculture-47fc92-003 ●
device-poultry (24dc88)   device-poultry (414db5)   device-poultry (bbab29)
 └─ poultry-24dc88-001 ●   └─ poultry-414db5-001 ●   └─ poultry-bbab29-001 ●
                                          ● = own MQTT client and connection
```

The default topology is 1 x 1 per type, one container per device. That option isolates
best: each device has its own process and network address, and stopping a container
removes exactly one device. Raising M packs many devices into one process. That costs much
less memory per device, and the planned experiments compare both granularities.

- **How the topology is applied.** Compose has one worker service per type, named
  `device-{domain}`. Each service sets `DEVICE_TYPES`, and its `deploy.replicas` and
  `DEVICES_PER_WORKER` come from the variables `DEVICE_{DOMAIN}_REPLICAS` and
  `DEVICE_{DOMAIN}_DEVICES_PER_WORKER`. `monitoring-mqtt stack up` validates the topology
  against the device catalog and passes these variables to Compose. Changing the topology
  therefore never requires an image rebuild. Compose then adds or removes containers, and it
  recreates a service's containers only when M changes.
- **Identity.** Each device gets the identity `{domain}-{worker_id}-{index:03d}`, which is
  also its MQTT `client_id`. `worker_id` derives from the container ID, so containers never
  share identities. After starting, `stack up` checks that every service runs the expected
  number of containers and that no two containers share a worker id. If either check
  fails, it exits with an error. Across machines, where the CLI cannot see the other
  containers, each worker claims its id in the broker before creating its devices (see
  [Worker registry](#worker-registry)).
- **Scheduling.** A worker publishes from all its devices on a fixed-rate schedule, with
  start times spread evenly over one interval to avoid bursts. Before the first publication
  it waits, for at most 10 s, until its devices are connected.
- **Behaviour.** Each device can go through episodes: anomalies, pauses, delays,
  disconnections, being switched off. They come from the experiment's `behavior` section
  (random or scheduled) or from live commands, apply to that device only, and are announced
  on the simulation plane ([simulation.md](simulation.md)).
- **Failure granularity.** A device that fails only affects itself, because the worker
  catches the error and keeps the other devices running. A worker container that fails
  takes down all the devices it hosts.

### Subscribers

The `monitors` section of `config/simulation.yaml` sets the number of replicas of each
monitor kind: one per catalog domain (`aquaculture`, `poultry`, `cattle`) plus `all`. The
CLI passes them to Compose as `MONITOR_{KIND}_REPLICAS`. Each replica is a separate
container with its own MQTT client id, `monitor-{kind}-{container short id}`.

```text
                         ┌──► monitor-poultry (replica 1)   copy 1
 poultry-xxxx-001 ──► broker ──► monitor-poultry (replica 2)   copy 2
   (one PUBLISH)         ├──► ...
                         └──► monitor-poultry (replica 20)  copy 20
                              monitor-aquaculture × 20      no copy (filter does not match)
```

Every monitor also publishes its status (online with counters, offline through its Last
Will, stopped on a clean shutdown) on `monitoring/status/v1/monitors/{client_id}`. MQTT
cannot list subscribers, so this observability plane is how operator tools see who is
consuming. Publishers never read it: devices keep publishing with zero subscribers. See
[mqtt-contract.md](mqtt-contract.md#observability-plane).

MQTT 3.1.1 has no shared subscriptions, so N subscribers with the same filter receive N
copies of each message (fan-out). Adding monitor replicas raises the broker's delivery work
but does not spread the processing. This setup serves the planned experiment that measures
the cost of fan-out. In a verified run with 20 aquaculture and 20 poultry monitors, the
broker had 43 clients (3 devices and 40 monitors), and each ammonia event reached all 20
poultry monitors and none of the aquaculture ones.

### Worker registry

A worker's id is part of every device client id it hosts. Two workers with the same id,
for example on two machines, would make their devices take over each other's MQTT
connections. Before creating devices, each worker claims its id with a retained message
on `monitoring/registry/v1/workers/{worker_id}`, protected by a Last Will that removes the
claim if the worker dies:

```text
 worker start ──► control connection (Last Will: clear claim)
              ──► subscribe to its claim topic, wait 1 s
                    ├─ claimed by another worker ──► random id, try again
                    └─ free ──► publish retained claim, wait 1 s ──► create devices
 running      ──► later conflict: earlier start wins; the loser exits (code 3) and restarts
 clean stop   ──► clear claim
```

This is coordination through publish/subscribe only: the broker stores and routes the
retained claims, and the workers decide. It adds about 2 s to a worker's start. The
protocol is specified in [mqtt-contract.md](mqtt-contract.md#worker-registry), and
`monitoring-mqtt stack workers` lists the workers registered in the broker from every
machine.

## Data flow

```text
 device-* containers               broker                       monitor-* containers
 ┌──────────────────────┐  PUBLISH  ┌─────────┐  matching   ┌──────────────────────────┐
 │ VirtualDevice        │ ────────► │ broker  │ ──────────► │ Monitor                  │
 │  generator → payload │   QoS 1   │ routes  │  messages   │  validate payload+topic  │
 │  own MQTT client     │           │ by topic│             │  log, count invalid      │
 └──────────────────────┘           └─────────┘             └──────────────────────────┘
        monitoring/v1/aquaculture/dissolved_oxygen/{device_id}
```

1. A `VirtualDevice` asks its generator for a value and builds a `Measurement`.
2. The device publishes the JSON payload on its own topic through its own client.
3. The broker forwards the message to every subscription whose filter matches the topic.
4. The monitor validates the payload against the schema and the topic, then logs it with
   its observed latency.

The publisher has no knowledge of any subscriber, and the monitor has no knowledge of any
publisher. The topic and payload contract is described in [mqtt-contract.md](mqtt-contract.md).

## Code structure

| Package | Responsibility | May depend on |
|---|---|---|
| `monitoring_mqtt.core` | Contract (topics, payload model, worker registry, status, simulation plane, alerts, ingestion batches, durations), configuration, logging, MQTT client factory | Third-party libraries only |
| `monitoring_mqtt.storage` | Database schema, Alembic migrations, idempotent writes, queries | `core`. Only `api` imports it |
| `monitoring_mqtt.api` | FastAPI application: ingestion and query routes, Scalar reference | `core`, `storage` |
| `monitoring_mqtt.simulator` | Device catalog, generators, identities, worker id registration, behaviour (episodes), simulation plane client, `VirtualDevice`, `SimulationWorker`, simulator entrypoint | `core` |
| `monitoring_mqtt.consumers` | Subscribers: `monitor`, `ingestion` (MQTT → API, buffer, retries), `alerts` (rules, alert engine) | `core`. Never `storage` or `api` |
| `monitoring_mqtt.testing` | Runtime checks against a running stack (`smoke`) | `core` |
| `monitoring_mqtt.tools` | Auxiliary services: registry listing (`workers`) and the TCP `relay` | `core` |
| `monitoring_mqtt.tui` | Read-only terminal UI (Textual): observer client, state model, screens | `core` |
| `monitoring_mqtt.cli` | Operator commands that call `docker compose` and validate the topology | `core`, `simulator.catalog`. No other package imports `cli` |

`core` never imports any other package of the project. The simulator never knows about
consumers, and consumers never know about the simulator. They share only the contract in
`core`.

Each service is a module entrypoint that runs inside the shared application image:

| Service | Command |
|---|---|
| `device-aquaculture`, `device-poultry`, `device-cattle` | `python -m monitoring_mqtt.simulator` (differ only by `DEVICE_TYPES`) |
| `monitor-aquaculture`, `monitor-poultry`, `monitor-cattle`, `monitor-all` | `python -m monitoring_mqtt.consumers.monitor` (differ only by `MQTT_TOPIC_FILTER`) |
| `migrate` | `python -m monitoring_mqtt.storage.migrate` (runs once per `stack up`, then exits) |
| `api` | `python -m monitoring_mqtt.api` (uvicorn; published on `127.0.0.1:8000`) |
| `ingestion` | `python -m monitoring_mqtt.consumers.ingestion` (fixed client id, persistent session) |
| `alerts` | `python -m monitoring_mqtt.consumers.alerts` (fixed client id, persistent session) |
| `control` | `python -m monitoring_mqtt.tools.control` (one-off, profile `tools`) |
| `smoke` | `python -m monitoring_mqtt.testing.smoke` (one-off, profile `tools`) |
| `workers` | `python -m monitoring_mqtt.tools.workers` (one-off, profile `tools`) |
| `tui` | `python -m monitoring_mqtt.tui` (interactive one-off, profile `tools`; see [tui.md](tui.md)) |
| `broker-relay` | `python -m monitoring_mqtt.tools.relay` (host network; only for a tunnel on the loopback with Docker Engine on Linux) |

## Deployment view

```text
 Host
 ├── monitoring-mqtt CLI ──(docker compose)──► Docker Engine
 │
 └── Docker Engine
      └── Compose project "monitoring-mqtt"
           └── network monitoring-mqtt_default
                ├── broker               (Mosquitto, listens on 1883; 127.0.0.1:1883 on the host)
                ├── device-<domain> × N  ──► broker:1883   (M connections each)
                ├── monitor-<kind> × N   ──► broker:1883   (1 connection each)
                ├── alerts               ──► broker:1883   (rules; publishes alerts)
                ├── ingestion            ──► broker:1883, ──HTTP──► api:8000
                ├── api                  ──SQL──► postgres:5432   (127.0.0.1:8000 on the host)
                ├── postgres             (volume pgdata; not published on the host)
                └── migrate              (one-off: schema migrations before the API starts)
```

- **Fixed project name.** Every CLI invocation targets the Compose project `monitoring-mqtt`,
  so different shells, and a development container that shares the host Docker daemon,
  manage the same stack.
- **Internal addressing.** Containers reach the broker as `broker:1883` on the Compose
  network. Host access on the loopback interface is optional and intended for debugging.
- **Self-contained images.** The broker image embeds its configuration. The application image
  (`Dockerfile`) installs the package and its locked dependencies with `uv`, runs as a
  non-root user, and is shared by every Python service. There are no bind mounts.
- **Health.** The broker has a healthcheck that reads the retained `$SYS/broker/version`
  topic; PostgreSQL uses `pg_isready` and the API `GET /health`. `stack up` waits for them
  to pass. The API starts only after `migrate` succeeded, and the ingestion consumer only
  after the API is healthy.
- **Several machines.** The same project can be split across machines, for example the
  broker on a Raspberry Pi and devices on other computers. Each machine runs the parts
  listed in its `.env` and connects to the shared broker; see [distributed.md](distributed.md).

## Smoke test

`monitoring-mqtt test smoke` first checks that the broker container is running, when this
machine runs the broker. It then
starts the `smoke` service once (`docker compose run --rm --no-deps smoke`) on the Compose
network, so the test never relies on host port mapping and runs the same way from the host
or a Dev Container. The container performs these checks:

1. **Broker.** Two independent clients, a publisher and a subscriber, connect to the
   broker this machine uses (`broker:1883`, or `MQTT_BROKER_HOST`), and the subscriber
   subscribes to `monitoring/v1/#`.
2. **Probe round trip.** The publisher sends a valid probe measurement with a known
   `event_id` on `monitoring/v1/smoke/probe/smoke-<id>`. The check requires the broker's
   `PUBACK` and the arrival of that same `event_id` at the subscriber before the timeout
   (`SMOKE_TIMEOUT_SECONDS`, default 10 s). No monitor subscribes to the `smoke` domain, so
   the probe never reaches the running monitors.
3. **Live telemetry.** At least one valid measurement from the running devices must arrive
   within a window derived from the experiment: twice the publish interval plus 5 s (15 s
   by default). The window therefore grows with slow experiments and never gives a false
   failure. On a machine without the `devices` role the result is only reported, because
   the devices may run elsewhere.

The command exits with `0` only if every check passes, and with `1` otherwise. It also
exits with `1`, without starting a container, when the stack is not running.

## Failure behaviour

| Event | Behaviour |
|---|---|
| Broker not ready at startup | Services wait for the broker healthcheck. Clients also keep retrying on their own |
| Broker restart | Clients reconnect automatically (backoff 1–30 s). Monitors subscribe again on every connection because sessions are clean |
| Broker unreachable while publishing | The measurement is dropped and logged as a warning. The `sequence` gap stays visible to consumers |
| Subscriber stopped | Publishers are unaffected and keep publishing. A clean stop removes the monitor's status; a lost connection leaves it `offline` (Last Will) |
| Invalid message | The monitor logs it with the reason, counts it and keeps running |
| `SIGTERM` (`docker stop`) | Services disconnect cleanly and log summary counters. Workers remove their registry claim |
| Worker killed or cut off | The broker publishes the worker's Last Will, which removes its registry claim |
| Network cut (half-open connection) | Detected by keepalive within 1.5 x 30 s. A worker's unacknowledged QoS 1 messages are sent again after reconnection; a monitor with a clean session loses what was routed to it meanwhile |
| API or database down | The ingestion consumer keeps the events in its buffer and retries with backoff; nothing is lost while the buffer has room ([storage.md](storage.md#measured)) |
| Ingestion consumer or alert engine down | Persistent session: the broker queues their measurements (up to 1000) and delivers them on reconnection; the alert engine restores its open alerts first |
| Two workers with the same id | The later one switches to a random id before creating devices; a conflict found while running stops the later worker, which restarts with a new id |

Measured examples of each case, and how to provoke them: [failures.md](failures.md).

## The CLI is outside the data path

The CLI only translates commands into `docker compose` invocations. It never runs a
publisher or subscriber in its own process, and the stack keeps running after the CLI exits.

The terminal UI ([tui.md](tui.md)) is not in the data path either. It is a read-only
subscriber that observes telemetry, the worker registry, the monitor status and the
broker's `$SYS` statistics. Nothing in the stack depends on it.

## Security boundaries

The broker accepts anonymous connections. This is acceptable only because the environment
is an isolated lab: the port is bound to `127.0.0.1` by default, and a machine that exposes
it to others (`MQTT_BIND_ADDRESS`) must stay on an isolated network or a private tunnel.
Any deployment beyond that requires authentication, TLS and ACLs.

The stack can be split across machines (for example the broker on one machine and devices
on others) through the machine settings in `.env`; see [distributed.md](distributed.md).
The components and the contract are the same; only where each service runs changes.

A single broker is a single point of failure. The current architecture makes no claims of
high availability.
