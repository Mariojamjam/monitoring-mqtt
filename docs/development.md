# Development guide

## Environments

Two environments are supported. Both operate the same Compose project, `monitoring-mqtt`.

### Host

`scripts/bootstrap.py` installs everything a contributor needs on Linux, macOS or Windows.
The only prerequisite is Python 3.9 or newer. The script performs these steps:

| Step | Command run by the script |
|---|---|
| Install uv if missing (after confirmation, or with `--yes`) | official installer from `astral.sh` |
| Managed Python 3.12 | `uv python install 3.12` |
| Development environment (`.venv`) | `uv sync --frozen` |
| User-level command, pinned to `uv.lock` | `uv export ...` and `uv tool install --editable . --constraints ...` |
| Command directory on `PATH` | `uv tool update-shell` (only if needed) |
| Verification | `monitoring-mqtt version`, `monitoring-mqtt doctor` |

`uv tool install` does not read `uv.lock` by itself. For that reason the script exports the
locked versions and passes them as constraints. Re-run the script after dependency changes.
Code changes are picked up immediately because the installation is editable.

### Dev Container

`.devcontainer/` defines the image (Python 3.12, uv, a non-root `vscode` user) and uses the
`docker-outside-of-docker` feature to provide the Docker CLI and Compose V2 against the host
daemon. When the container is created, `uv sync --frozen` installs the project and the locked
dependencies, which also provides the `monitoring-mqtt` command. The container never starts
the stack on its own.

Design notes:

- **No host-side scripts.** No step runs on the host. The container works on any OS that
  runs Docker.
- **Separate virtual environment.** `UV_PROJECT_ENVIRONMENT` points to
  `/home/vscode/.venvs/monitoring-mqtt`. The checkout is shared with the host, and writing
  `.venv` from Linux would break a host environment on another OS or path.
- **Cached downloads.** The `monitoring-mqtt-uv-cache` volume keeps packages between
  container rebuilds.
- **Docker networking.** The stack's containers run on the host daemon, so `127.0.0.1:1883`
  inside the Dev Container is not the broker. Use the CLI, or run tools as containers on
  the Compose network.

### Line endings

`.gitattributes` forces LF line endings. Files copied into Linux images, such as
`mosquitto.conf`, therefore stay valid when the repository is cloned on Windows.

## Python environment

The project uses [uv](https://docs.astral.sh/uv/) with `pyproject.toml` and a committed
`uv.lock`. The interpreter is pinned to Python 3.12 in `.python-version`. uv downloads that
interpreter when needed, so the system Python is never modified.

```bash
uv sync          # create .venv and install runtime + dev dependencies from uv.lock
```

Dependencies are split as follows:

- **Runtime:** `typer`, `rich`, `paho-mqtt`, `pydantic`, `pyyaml`
- **Development group (`dev`):** `pytest`, `ruff`

To add a dependency, use `uv add <package>`, or `uv add --dev <package>` for tooling. These
commands update both `pyproject.toml` and `uv.lock`.

## Using the CLI

After either setup (host bootstrap or Dev Container), the command is available by name in
any directory:

```bash
monitoring-mqtt --help
monitoring-mqtt doctor
```

`doctor` exits with code `1` when a required check fails. A CLI that is missing from `PATH`
is reported as a warning, not as a failure.

If you have only run `uv sync` and have not installed the command, prefix it with `uv run`
from inside the checkout, for example `uv run monitoring-mqtt doctor`.

## Docker stack

```bash
monitoring-mqtt stack up --build   # build images, start, wait for healthchecks
monitoring-mqtt stack status
monitoring-mqtt stack logs broker
monitoring-mqtt stack restart broker  # restart as is (same run id); see failures.md
monitoring-mqtt stack down              # keeps the stored history; --volumes deletes it
```

With the `storage` role, `stack up` also runs PostgreSQL, the schema migrations, the API
(`http://127.0.0.1:8000`, reference at `/docs`) and the ingestion consumer; see
[storage.md](storage.md). To change the schema, edit `storage/schema.py` and add an Alembic
migration (`storage/migrations/versions/`); an integration test fails if they differ.
After changing a route or a response model, regenerate the API documents (a unit test
checks them):

```bash
uv run python -m monitoring_mqtt.api.docs > docs/openapi.json
uv run python -m monitoring_mqtt.api.docs --html > docs/api-reference.html
```

`stack up` reads `config/simulation.yaml` (or the file given with `--config`), validates it
against `config/devices.yaml` and runs `docker compose up --detach --wait --remove-orphans`
with the matching replica and devices-per-container variables. It returns only after every service with a healthcheck
reports healthy, and then prints and verifies the device topology. Images embed their configuration, and the stack needs no
bind mounts. This keeps it working when the host and a development container see the
checkout at different paths.

The default host port is `127.0.0.1:1883`. If it is already in use, set `MQTT_HOST_PORT`:

```bash
MQTT_HOST_PORT=11883 monitoring-mqtt stack up
```

### Running parts of the stack on different machines

One machine runs everything by default. The machine settings in `.env` (`STACK_ROLES`,
`MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, `MQTT_BIND_ADDRESS`) split the stack across
machines, for example the broker on a Raspberry Pi and devices on other computers. See
[distributed.md](distributed.md).

### Running a service locally

The services are plain Python modules, so they run inside the project environment with
`uv run`, not through the CLI. With the broker running, you can start one **on the host**
against `127.0.0.1:1883`. This does not work from the Dev Container, where `127.0.0.1` is
the container itself.

```bash
MQTT_HOST=127.0.0.1 MQTT_TOPIC_FILTER='monitoring/v1/#' uv run python -m monitoring_mqtt.consumers.monitor
MQTT_HOST=127.0.0.1 PUBLISH_INTERVAL_SECONDS=1 uv run python -m monitoring_mqtt.simulator
```

### Configuration

| Kind | Where | Examples |
|---|---|---|
| Experiment | `config/simulation.yaml` (versioned) | run id, seed, log level, QoS, keepalive, clean session, reconnect backoff, publish interval, workers, monitors, behaviour, storage, alerts |
| Alert rules | `config/alerts.yaml` (versioned, baked into the image; `stack up` passes the experiment's rules as `ALERT_RULES`) | thresholds, severity, `after`, hysteresis |
| Device catalog | `config/devices.yaml` (versioned, baked into the image) | types, domains, metrics, units, generators, normal bands, anomaly targets |
| Machine | `.env` (git-ignored, from `.env.example`) | `STACK_ROLES`, `MQTT_BROKER_HOST`, `MQTT_BROKER_PORT`, `MQTT_BIND_ADDRESS`, `MQTT_HOST_PORT`, `NODE_NAME`, `SMOKE_TIMEOUT_SECONDS`, `API_BIND_ADDRESS`, `API_HOST_PORT`, `POSTGRES_PASSWORD` |
| Contract and internals | code (documented) | MQTT 3.1.1, port 1883, `retain=false`, topics, schema, validation limits |

`monitoring-mqtt stack up` turns `config/simulation.yaml` into the environment variables
below. You only set them yourself when you run a service outside Compose, for example
with `uv run`:

| Variable | Used by | Default | From `simulation.yaml` |
|---|---|---|---|
| `MQTT_HOST`, `MQTT_PORT` | all | `localhost`, `1883` | no (Compose sets `broker:1883`, or `MQTT_BROKER_HOST`/`MQTT_BROKER_PORT` from `.env`) |
| `MQTT_QOS` | all | `1` | `mqtt.qos` |
| `MQTT_KEEPALIVE` | all | `30` | `mqtt.keepalive_seconds` |
| `MQTT_CLEAN_SESSION` | all | `true` | `mqtt.clean_session` (the smoke test always uses `true`) |
| `MQTT_RECONNECT_MIN_DELAY_SECONDS`, `MQTT_RECONNECT_MAX_DELAY_SECONDS` | all | `1`, `30` | `mqtt.reconnect` |
| `LOG_LEVEL` | all | `INFO` | `run.log_level` |
| `NODE_NAME` | all | unset | no (machine label from `.env`, default: the CLI's hostname) |
| `RUN_ID` | simulator | `run-<UTC timestamp>` | `run.run_id`, or generated once per `stack up` |
| `SIMULATION_SEED` | simulator | `42` | `run.seed` |
| `PUBLISH_INTERVAL_SECONDS` | simulator | `5` | `devices.publish_interval_seconds` |
| `DEVICE_TYPES` | simulator | all catalog types | set per worker service by Compose |
| `DEVICES_PER_WORKER` | simulator | `1` | `workers.<type>.devices_per_worker` |
| `DEVICES_CONFIG` | simulator | `config/devices.yaml` | no (the image uses `/app/config/devices.yaml`) |
| `WORKER_ID` | simulator | first 6 characters of the hostname | no; never set it on a Compose worker service, because all its replicas would share it |
| `MQTT_TOPIC_FILTER`, `MONITOR_NAME` | monitor | required, `monitor` | set per monitor service by Compose |
| `SIMULATION_BEHAVIOR` | simulator | empty | `behavior`, as JSON |
| `API_URL` | ingestion | `http://api:8000` | no (set by Compose) |
| `INGEST_BATCH_SIZE`, `INGEST_FLUSH_INTERVAL_SECONDS`, `INGEST_BUFFER_LIMIT` | ingestion | `500`, `1`, `50000` | `storage.batch_size`, `.flush_interval`, `.buffer_limit` |
| `ALERT_RULES` | alerts | empty: `ALERT_RULES_FILE` (`config/alerts.yaml`) | `alerts.rules`, loaded and validated, as JSON |
| `DATABASE_URL` | api, migrate | `postgresql+psycopg://monitoring:monitoring@localhost:5432/monitoring` | no (Compose builds it from `POSTGRES_PASSWORD`) |
| `API_PORT` | api | `8000` | no |
| `SMOKE_TIMEOUT_SECONDS` | smoke | `10` | no (machine setting, `.env`) |
| `SMOKE_TELEMETRY_WINDOW_SECONDS` | smoke | `15` | derived: 2 × publish interval + 5 s |
| `SMOKE_REQUIRE_TELEMETRY` | smoke | `true` | no (`false` when this machine runs no devices) |

The simulator exits with code `3` when it cannot claim a worker id in the broker registry
or loses it to another worker while running; Compose then restarts it with a new id.

Invalid values stop the service at startup with exit code `2` and a clear message.

## Tests

```bash
monitoring-mqtt test unit              # unit tests (runs pytest in the project environment)
monitoring-mqtt test unit -k topics    # extra arguments are passed to pytest
monitoring-mqtt test smoke             # end-to-end check against the running stack
monitoring-mqtt test integration       # integration suite on an isolated stack (Docker, ~5 min)
```

`test unit` runs `uv run --frozen pytest tests/unit` in the project directory. `pytest`
alone also runs only the unit tests (`testpaths` in `pyproject.toml`). pytest is a
development dependency, so it lives in the project environment and not in the installed
CLI. Running `uv run pytest` directly is equivalent. The exit code is the one from pytest.

The unit tests never call a real Docker daemon or broker. Subprocess calls are replaced
with fakes, and the tests assert the exact `docker compose` argument list and the exit-code
propagation. The terminal UI is tested in two layers: its state model with synthetic broker
messages, and the Textual application headless (`App.run_test`) with a fake feed, pressing
keys and checking tables and screens.

`test smoke` requires a running broker: the local stack, or `MQTT_BROKER_HOST` when this
machine has no broker role. It is described in
[architecture.md](architecture.md#smoke-test). Typical failures and their causes:

| Failing check | Likely cause |
|---|---|
| `the stack is not running` | The stack is down; run `monitoring-mqtt stack up` |
| `broker` | The broker is unhealthy or unreachable on the Compose network |
| `probe round trip` | The broker accepts connections but does not route messages |
| `live telemetry` | No device worker is publishing; check `config/simulation.yaml` and `stack logs`. Reported as a warning only on machines without the `devices` role |

### Integration tests

`monitoring-mqtt test integration` runs `tests/integration` against real containers. The
tests start their **own stack** through the real CLI (`stack up --build` with
`tests/integration/experiment.yaml`) under the Compose project `monitoring-mqtt-it`, on a
free port of `127.0.0.1`, and remove it at the end. The stack you use, `monitoring-mqtt`,
keeps running untouched. The project name comes from the test-only variable
`MONITORING_MQTT_COMPOSE_PROJECT`; never set it for normal use.

The test experiment publishes every 0.5 s with a 5 s keepalive, so failures are detected
in seconds and the whole suite takes about 5 minutes. The tests observe the system only
through the broker, like any client: telemetry, the worker registry, each monitor's own
status (what it received), the simulation ground truth and the alerts; and the history
through the API.

| File | Checks |
|---|---|
| `test_1_routing.py` | One client per device; each monitor receives only its domain; `monitor-all` receives every domain; replicas get the same messages (fan-out) |
| `test_2_consumers.py` | Invalid payloads (not JSON, schema error, payload of another device) are counted and the monitor keeps consuming |
| `test_3_decoupling.py` | Devices publish without gaps with no monitor running; a killed monitor is `offline` (Last Will); a killed worker leaves the registry and returns with the same id; a second worker with a taken id picks another |
| `test_4_behaviour.py` | A scheduled anomaly matches its ground truth and leaves the normal band; live `stop` (no gap, neighbour unaffected), `disconnect` (gap), `delay` (late, complete), `pause` (silence); rejected and unanswered commands |
| `test_5_cli.py` | `stack workers`, `test smoke` and `stack restart` end to end |
| `test_6_recovery.py` | Broker restart: everyone reconnects and receives again; network cut: the broker detects it by keepalive and the worker comes back |
| `test_7_storage.py` | The migrated schema matches the code; everything the observer saw is stored; a repeated event is stored once; the three times are in order; API, database or ingestion consumer down for 5 s: nothing lost |
| `test_8_alerts.py` | A scheduled anomaly raises and clears an alert within its ground truth episode; a live anomaly is alerted and stored; no alert without anomaly; an open alert survives an engine restart; the queries reflect the run |

Inside the Dev Container, whose `localhost` is not the host's, the tests join the test
network and reach the broker as `broker:1883`. Set `IT_KEEP_STACK=1` to keep the test stack
after the run (for its logs); remove it with
`docker compose -p monitoring-mqtt-it down`. Extra arguments go to pytest, for example
`monitoring-mqtt test integration -k recovery`.

## Code quality

```bash
uv run ruff check .        # lint
uv run ruff format .       # format
```

Before submitting changes, `uv run ruff check .`, `uv run ruff format --check .` and
`monitoring-mqtt test unit` must all pass.

GitHub Actions runs the same checks on every push to `main` and on pull requests
(`.github/workflows/ci.yml`), with the locked dependencies and the same uv version as the
Dev Container. A second job then runs the integration suite on the runner's Docker and,
if it fails, attaches the test stack's logs to the run.

## Layout

```text
.devcontainer/           # Dev Container: Dockerfile + devcontainer.json
scripts/bootstrap.py     # host installer (Linux, macOS, Windows)
compose.yaml             # Compose stack (project name: monitoring-mqtt)
config/devices.yaml      # device type catalog (baked into the app image)
config/simulation.yaml   # experiment: parameters and topology (read by the CLI)
Dockerfile               # shared application image (uv, locked deps, non-root)
.env.example             # machine settings (roles, broker address, host port, smoke timeout)
infra/mosquitto/         # broker image: Dockerfile + mosquitto.conf
src/monitoring_mqtt/
├── core/                # shared contract and infrastructure, no service logic
│   ├── topics.py        # topic building, parsing and filters
│   ├── models.py        # Measurement payload model (Pydantic)
│   ├── registry.py      # worker registry contract (claim topic and payload)
│   ├── simulation.py    # simulation plane: ground truth events, device commands
│   ├── alerts.py        # alert contract: events and active state topics
│   ├── ingest.py        # HTTP ingestion batches (consumer → API)
│   ├── durations.py     # "30s", "2m"… parsing
│   ├── status.py        # observability plane: monitor status topic and payload
│   ├── config.py        # environment configuration
│   ├── mqtt.py          # Paho client factory, reconnection
│   ├── identity.py      # hostname-derived ids
│   ├── lifecycle.py     # SIGTERM/SIGINT handling
│   └── logging.py       # UTC key=value logging
├── simulator/
│   ├── __main__.py      # simulator entrypoint (builds and runs a worker)
│   ├── catalog.py       # device type catalog (YAML + validation)
│   ├── factory.py       # builds a worker's devices (round-robin types, ids, sites)
│   ├── worker.py        # SimulationWorker: M devices, fixed-rate schedule, episodes
│   ├── behavior_config.py # the experiment's `behavior` section (durations, faults)
│   ├── behavior.py      # episodes per device: random, scheduled, live commands
│   ├── plane.py         # worker's simulation plane client (events, commands)
│   ├── device.py        # VirtualDevice: own MQTT client, sequence, generator
│   ├── identity.py      # worker_id and device_id
│   ├── registration.py  # claims the worker id in the broker registry
│   └── generators/      # mean-reverting and bounded-noise generators (steerable)
├── consumers/
│   ├── monitor.py       # generic monitor (entrypoint)
│   ├── ingestion.py     # MQTT → API bridge (entrypoint): persistent session, batches
│   ├── buffer.py        # bounded buffer that hands out batches
│   ├── delivery.py      # HTTP delivery with retries (httpx)
│   ├── rules.py         # alert rules (YAML) and their evaluation (pure)
│   └── alerts.py        # alert engine (entrypoint): restore, evaluate, publish
├── storage/             # only the API (and migrate) import it
│   ├── schema.py        # tables (SQLAlchemy Core)
│   ├── migrations/      # Alembic, configured in code (migrate.py)
│   ├── migrate.py       # migrate service entrypoint
│   ├── writes.py        # idempotent writes (ON CONFLICT)
│   ├── queries.py       # read side, cursor pagination
│   └── store.py         # facade used by the API; outages → StorageUnavailableError
├── api/                 # FastAPI
│   ├── app.py           # application factory, 503 handling
│   ├── routes/          # health, ingest, devices, measurements, alerts, runs
│   ├── schemas.py       # response models
│   └── docs.py          # Scalar reference (/docs), OpenAPI export
├── testing/
│   └── smoke.py         # end-to-end smoke check (runs as the `smoke` service)
├── tui/                 # read-only terminal UI (Textual)
│   ├── __main__.py      # entrypoint (runs as the `tui` service)
│   ├── feed.py          # observer MQTT client: queues raw messages
│   ├── state.py         # pure model built from broker messages (no MQTT, no Textual)
│   ├── render.py        # Rich renderables from the model
│   └── app.py           # Textual application: screens, keys, refresh
├── tools/
│   ├── workers.py       # lists registry claims (runs as the `workers` service)
│   ├── control.py       # sends a live command to a device (runs as `control`)
│   └── relay.py         # TCP relay to a loopback tunnel (runs as `broker-relay`)
└── cli/
    ├── main.py          # Typer application (entry point: monitoring-mqtt)
    ├── project.py       # project root resolution
    ├── doctor.py        # environment checks, including broker reachability
    ├── machine.py       # machine settings (.env): roles and broker address
    ├── device.py        # `device` commands (live control through the broker)
    ├── dockerhost.py    # Docker Desktop vs Engine: what host.docker.internal reaches
    ├── tui.py           # monitoring-mqtt tui (starts the `tui` service)
    ├── compose.py       # docker compose runner (argv list, exit code preserved)
    ├── simulation.py    # config/simulation.yaml → validated experiment → Compose variables
    ├── testing.py       # test unit / integration / smoke
    └── stack.py         # stack up/down/restart/status/logs/workers
tests/unit/              # unit tests (no Docker, no broker)
tests/integration/       # integration tests (isolated stack: monitoring-mqtt-it)
config/examples/         # ready-made experiments (anomalies, instability, faults)
```
