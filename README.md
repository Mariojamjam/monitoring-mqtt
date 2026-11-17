# Monitoring MQTT

A Distributed Systems lab built on the **Publish/Subscribe** paradigm. Virtual IoT devices
that specialize in chemical measurements for animal production publish telemetry over
**MQTT** to an **Eclipse Mosquitto** broker. Independent applications subscribe only to the
topics they are interested in.

| Domain | Virtual device | Metric | Unit |
|---|---|---|---|
| Aquaculture | Dissolved oxygen probe | `dissolved_oxygen` | `mg/L` |
| Poultry | Ammonia detector | `ammonia` | `ppm` |
| Cattle | Rumen pH meter | `rumen_ph` | dimensionless |

All values are **synthetic**. They are not veterinary or clinical reference values.

> **Status:** v0.3.0. The three device types publish through Mosquitto. A versioned
> experiment file sets how many worker containers and devices run per type. Independent
> monitors receive only the domain they subscribe to, and a smoke test verifies the flow end
> to end. Parts of the stack can run on different machines, a read-only terminal UI shows
> who publishes and who consumes, and failures and recovery are documented with measured
> results. Every measurement and alert is stored in PostgreSQL through a FastAPI API (the
> only component that touches the database), and an alert engine publishes derived alerts
> on the broker, evaluated against the simulation's ground truth. A Streamlit dashboard
> shows the history: series against the normal band and the alert thresholds, alert
> timelines and runs side by side, read from the API only. Versioned experiment scenarios
> (scale, worker granularity, publish frequency, QoS, fan-out, failures) run from the CLI,
> are measured from the outside and turn into tables and charts.

## Getting started

There are two supported ways to work with the project. Both give you the same
`monitoring-mqtt` command and control the same Docker stack.

| | Host installation | Dev Container |
|---|---|---|
| You need | Python 3.9+ and Docker with Compose V2 | Docker and an editor or CLI that supports Dev Containers |
| Operating systems | Linux, macOS, Windows | Linux, macOS, Windows |
| Setup | `python scripts/bootstrap.py` | "Reopen in Container" |

### Option A: install the CLI on your machine

```bash
git clone https://github.com/Mariojamjam/monitoring-mqtt.git
cd monitoring-mqtt
python scripts/bootstrap.py        # on Windows: py scripts\bootstrap.py
```

The bootstrap script uses only the Python standard library and can be run again at any
time. It does the following:

1. Installs [uv](https://docs.astral.sh/uv/) for the current user if it is missing. It asks
   first; pass `--yes` to skip the question.
2. Installs Python 3.12 managed by uv. Your system Python is not modified.
3. Installs `monitoring-mqtt` as a user-level command, with the versions pinned in `uv.lock`.
4. Adds the uv tool directory to your `PATH` if needed.
5. Runs `monitoring-mqtt doctor`.

No administrator rights are needed, and Docker is checked but never installed. If the
script changed your `PATH`, open a new terminal. The command is installed in editable mode
and points at this checkout. If you move the checkout, run the bootstrap again.

### Option B: Dev Container

Open the repository in VS Code with the Dev Containers extension and choose **Reopen in
Container**. Any tool that supports the Dev Container specification also works, for example
`devcontainer up --workspace-folder .`. The container includes Python 3.12, uv, the Docker
CLI and Compose V2, and installs `monitoring-mqtt` automatically when it is created.

The container uses the **host Docker daemon** (Docker-outside-of-Docker). Stacks started
from the container and from the host are therefore the same stack. `monitoring-mqtt doctor`
shows the daemon ID on both sides so you can confirm it. Nothing has to be installed or run
on the host other than Docker.

> Access to the host Docker daemon grants broad control over the host's Docker
> environment. Use the Dev Container only for trusted code.

## Running the stack

```bash
monitoring-mqtt doctor
monitoring-mqtt stack up --build
monitoring-mqtt stack status
monitoring-mqtt stack logs device-aquaculture   # oxygen measurements being published
monitoring-mqtt stack logs monitor-aquaculture  # only aquaculture events, validated
monitoring-mqtt stack logs monitor-poultry      # only poultry events, validated
monitoring-mqtt stack logs monitor-cattle       # only cattle events, validated
monitoring-mqtt tui                             # live view: publishers, broker, subscribers
monitoring-mqtt stack workers                   # device workers registered in the broker
monitoring-mqtt dashboard                       # the history in the browser (http://127.0.0.1:8501)
curl -s http://127.0.0.1:8000/api/stats         # the stored history (reference: /docs)
monitoring-mqtt stack logs alerts               # alerts raised and cleared
monitoring-mqtt test smoke                      # end-to-end check of the running stack
monitoring-mqtt experiment run quick            # a measured experiment (about 3 minutes)
monitoring-mqtt stack down
```

The worker logs and the monitor logs show the same `event_id` values, so you can follow
each event from publisher to subscriber. Without installing the command, you can also run
`uv run monitoring-mqtt ...` from the checkout.

## Stack services

| Service | Role |
|---|---|
| `broker` | MQTT broker (Eclipse Mosquitto 2.1), reachable as `broker:1883` inside the stack |
| `device-aquaculture` | Worker containers for dissolved-oxygen probes |
| `device-poultry` | Worker containers for ammonia detectors |
| `device-cattle` | Worker containers for rumen pH boluses |
| `monitor-aquaculture` | Subscriber to `monitoring/v1/aquaculture/#` |
| `monitor-poultry` | Subscriber to `monitoring/v1/poultry/#` |
| `monitor-cattle` | Subscriber to `monitoring/v1/cattle/#` |
| `monitor-all` | Global subscriber to `monitoring/v1/#` (0 replicas by default) |
| `alerts` | Alert engine: applies [`config/alerts.yaml`](config/alerts.yaml) and publishes alerts on `monitoring/alerts/v1/...` ([alerts.md](docs/design/alerts.md)) |
| `ingestion` | Subscribes to measurements and alerts and posts them to the API in batches |
| `api` | FastAPI, the only component that talks to the database; `http://127.0.0.1:8000`, reference at [`/docs`](docs/design/api.md) |
| `dashboard` | Streamlit dashboard, `http://127.0.0.1:8501`: history, alerts and runs, read from the API ([dashboard.md](docs/guides/dashboard.md)) |
| `postgres` | PostgreSQL 18, the stored history (volume `pgdata`, not published on the host) |
| `migrate` | Applies the schema migrations on every `stack up`, then exits |
| `broker-relay` | Forwards to a broker tunnel on this machine's loopback; started only when needed (see [distributed.md](docs/guides/distributed.md#tunnels)) |

Each virtual device publishes every 5 s over its own MQTT connection. Publishers and
subscribers never know about each other. A device publishes whether or not any monitor
is running.

### Experiment configuration

Configuration is split by purpose:

| Where | What | Versioned |
|---|---|---|
| [`config/simulation.yaml`](config/simulation.yaml) | Everything that defines an experiment | Yes |
| [`config/devices.yaml`](config/devices.yaml) | The device catalog: what each sensor type measures, how its value evolves, its normal band and anomaly targets | Yes |
| [`config/alerts.yaml`](config/alerts.yaml) | Alert rules: thresholds, severity, hysteresis | Yes |
| [`config/examples/`](config/examples) | Ready-made experiments: anomalies, unstable devices, single-device faults | Yes |
| `.env` (copied from [`.env.example`](.env.example)) | Settings of your machine only: which parts of the stack run here, the broker address, the host port and the smoke test timeout | No |

`config/simulation.yaml` describes one experiment. The defaults shown below run one
container per device and one monitor per domain:

```yaml
run:
  run_id: null              # null: a new id per `stack up`, shared by every worker
  seed: 42
  log_level: INFO
mqtt:
  qos: 1
  keepalive_seconds: 30
  clean_session: true       # false: the broker queues QoS 1 messages for offline subscribers
  reconnect: {min_delay_seconds: 1, max_delay_seconds: 30}
devices:
  publish_interval_seconds: 5
workers:                    # publishers, per device type
  dissolved_oxygen_probe: {replicas: 1, devices_per_worker: 1}
  ammonia_detector:       {replicas: 1, devices_per_worker: 1}
  rumen_ph_bolus:         {replicas: 1, devices_per_worker: 1}
monitors:                   # subscribers, per kind
  aquaculture: {replicas: 1}
  poultry:     {replicas: 1}
  cattle:      {replicas: 1}
  all:         {replicas: 0}
```

- **Workers.** `replicas` is the number of containers (**N**), and `devices_per_worker` is
  the number of virtual devices inside each one (**M**). For example,
  `{replicas: 5, devices_per_worker: 10}` runs 50 oxygen probes in 5 containers.
- **Monitors.** `replicas` is the number of subscriber containers of that kind, for example
  `poultry: {replicas: 20}`. Every replica is an independent subscriber, and the broker
  delivers its own copy of each matching message to each one (fan-out). Replicas do not
  share the load.

- **Storage, alerts and dashboard.** `storage`, `alerts` and `dashboard` turn the history
  (PostgreSQL, API, ingestion), the alert engine and the dashboard on or off (all on by
  default) and tune them, for example `storage: {batch_size: 500, flush_interval: 1s}` or
  `dashboard: {refresh: 5s}`.
- **Behaviour.** An optional `behavior` section adds anomalies, instability (pauses,
  delays, disconnections) and scheduled faults on single devices, for example
  `anomalies: {every: 2m, lasts: 30s}`. See [simulation.md](docs/design/simulation.md).

To run a different experiment, edit the file and run `monitoring-mqtt stack up` again. No
image rebuild is needed. Every `stack up` starts a **new run**: it prints the experiment
parameters and the `run_id`, labels every worker container with that id, recreates the
worker containers, and checks that every container runs with a unique MQTT identity.
Experiment parameters set in `.env` or in the shell are ignored, because `stack up` always
applies the file.

## Command line interface

`monitoring-mqtt` is the single operational entry point of the project.

| Command | What it does |
|---|---|
| `monitoring-mqtt doctor` | Check the environment, Docker, this machine's roles, the broker, the API, the dashboard and the clock |
| `monitoring-mqtt stack up\|down\|status\|logs\|restart\|workers` | Run this machine's part of the stack; every `stack up` applies `config/simulation.yaml` as a new run |
| `monitoring-mqtt tui` | Live terminal UI: who publishes, the broker and who consumes |
| `monitoring-mqtt dashboard` | Open the stored history (series, alerts, runs) in the browser |
| `monitoring-mqtt device stop\|pause\|delay\|disconnect\|anomaly\|resume DEVICE_ID` | Live command to one device; its neighbours keep publishing |
| `monitoring-mqtt experiment list\|run\|report` | Run versioned experiment scenarios and turn them into tables and charts |
| `monitoring-mqtt test unit\|integration\|smoke` | Unit tests, the integration suite on an isolated stack, an end-to-end check |

Every command by task, with examples: [docs/commands.md](docs/commands.md). Every option:
the generated [command line reference](docs/reference/cli.md), or `monitoring-mqtt <command> --help`.

The global option `--project-dir PATH` selects the checkout to operate on. Without it, the
CLI looks for the project in this order:

1. The `MONITORING_MQTT_PROJECT_DIR` environment variable.
2. The checkout the package was installed from.
3. The current directory and its parents.

Because of this order, commands also work from outside the repository.

The `stack` commands call `docker compose` with the fixed project name `monitoring-mqtt` and
return the Compose exit code unchanged. The CLI is not part of the data path. Once the stack
is up, it keeps running without the CLI.

## Documentation

The [documentation index](docs/README.md) describes every document. Grouped by purpose:

- **Guides** (`docs/guides/`): [development](docs/guides/development.md),
  [terminal UI](docs/guides/tui.md), [dashboard](docs/guides/dashboard.md),
  [several machines](docs/guides/distributed.md),
  [Raspberry Pi deployment](docs/guides/deploy-raspberry-pi.md).
- **Design** (`docs/design/`): [system design](docs/design/design.md),
  [architecture](docs/design/architecture.md), [MQTT contract](docs/design/mqtt-contract.md),
  [simulation](docs/design/simulation.md), [storage](docs/design/storage.md),
  [alerts](docs/design/alerts.md), [HTTP API](docs/design/api.md).
- **Evaluation** (`docs/evaluation/`): [experiments](docs/evaluation/experiments.md),
  [failures and recovery](docs/evaluation/failures.md).
- **Reference** (`docs/reference/`, generated): [CLI](docs/reference/cli.md),
  [API reference page](docs/reference/api/index.html) and
  [OpenAPI document](docs/reference/api/openapi.json).
- **[Commands](docs/commands.md)**: every command, by task.

## Limitations

- The broker allows anonymous access. It is meant for an isolated lab: its port is published
  only on `127.0.0.1` by default. Expose it to other machines (`MQTT_BIND_ADDRESS`) only on
  an isolated network or through a private tunnel.
- A single Mosquitto instance is a single point of failure. The stack makes no
  high-availability claims.
- QoS 1 is at-least-once delivery, so duplicates are possible. Telemetry is not retained, so
  subscribers only see messages published while they are connected.
- Values are synthetic: a mean-reverting random process per device, not a physical model.
- Running `docker compose up` directly, without the CLI, ignores `config/simulation.yaml`
  and starts the default experiment.
- With `clean_session: false`, a subscriber's queued messages survive `docker stop` and
  `docker start`, but not container recreation, because the client id contains the container
  id. The broker keeps at most 1000 queued messages per client and expires abandoned sessions
  after one hour.
- MQTT 3.1.1 has no shared subscriptions. Monitor replicas multiply deliveries (fan-out),
  but they do not split the work between them. The ingestion consumer and the alert engine
  use fixed client ids with persistent sessions, so each runs as a single instance.
- The history is complete unless a limit is exceeded: events in the ingestion buffer are
  lost if that process dies, and the broker queues at most 1000 messages for a disconnected
  consumer. Both show up as sequence gaps in the stored data.
- PostgreSQL uses a lab password (`POSTGRES_PASSWORD`, default `monitoring`) and is not
  published on the host; the API and the dashboard have no authentication and are
  published on `127.0.0.1`.

## Tech stack

Python 3.12 · uv · Typer + Rich · Textual · Eclipse Paho MQTT · Pydantic · Eclipse
Mosquitto · FastAPI + Uvicorn · SQLAlchemy 2 + Alembic · PostgreSQL 18 (psycopg 3) · HTTPX ·
Scalar (API reference) · Streamlit + Altair + pandas · matplotlib (experiment reports) ·
Docker Compose V2.
