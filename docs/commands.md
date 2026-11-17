# Commands

Every command of the project, grouped by what you want to do. Each section links to the
document that explains it. Every option of every CLI command is listed in the generated
[command line reference](reference/cli.md), the same text as `monitoring-mqtt <command> --help`.

Commands run from any directory once the CLI is installed (see 1). With only `uv sync`, run
them from the checkout prefixed with `uv run`, for example `uv run monitoring-mqtt doctor`.

## 1. Set up a machine

```bash
python3 scripts/bootstrap.py          # uv, Python 3.12, the environment and the CLI (--yes: no prompts)
uv sync                               # or only the environment, inside the checkout
monitoring-mqtt version
monitoring-mqtt doctor                # environment, Docker, roles, broker, API, dashboard, clock
cp .env.example .env                  # machine settings (ports, roles, passwords); optional on one machine
```

In VS Code, *Reopen in Container* gives the same environment without installing anything
on the host. See [development.md](guides/development.md#environments).

## 2. Run the stack

```bash
monitoring-mqtt stack up              # apply config/simulation.yaml; a new run each time
monitoring-mqtt stack up --build      # after changing code or a Dockerfile
monitoring-mqtt stack up -c config/examples/anomalies.yaml --run-id lab-01
monitoring-mqtt stack status          # state of every service
monitoring-mqtt stack workers         # device workers registered in the broker, from every machine
monitoring-mqtt stack logs broker     # follow one service (Ctrl+C stops following, not the service)
monitoring-mqtt stack logs -n 50 --no-follow
monitoring-mqtt stack restart broker  # restart as is: same run id and settings
monitoring-mqtt stack down            # stop; the stored history is kept
monitoring-mqtt stack down --volumes  # stop and delete the history (asks first)
```

Experiment parameters live in [`config/simulation.yaml`](../config/simulation.yaml);
`stack up` always applies the file. See the [README](../README.md#running-the-stack).

## 3. Watch it

```bash
monitoring-mqtt tui                   # live: who publishes, the broker, who consumes
monitoring-mqtt dashboard             # the stored history in the browser (http://127.0.0.1:8501)
monitoring-mqtt dashboard --no-open   # check it and print the address only
monitoring-mqtt test smoke            # end-to-end check of the broker this machine uses
```

Guides: [tui.md](guides/tui.md), [dashboard.md](guides/dashboard.md).

## 4. Provoke behaviour and failures

One device, live (its neighbours keep publishing):

```bash
monitoring-mqtt device anomaly poultry-<worker>-001 --for 30s   # values out of range (--direction low)
monitoring-mqtt device delay   poultry-<worker>-001 --for 30s   # hold back, then send late
monitoring-mqtt device pause   poultry-<worker>-001 --for 30s   # connected but silent
monitoring-mqtt device disconnect poultry-<worker>-001 --for 30s  # measurements generated and lost
monitoring-mqtt device stop    poultry-<worker>-001 --for 30s   # switched off: nothing generated
monitoring-mqtt device resume  poultry-<worker>-001             # end every episode now
```

Scheduled episodes, from an experiment file:

```bash
monitoring-mqtt stack up -c config/examples/anomalies.yaml      # an anomaly per device about once a minute
monitoring-mqtt stack up -c config/examples/device-fault.yaml   # one device off, one disconnected
monitoring-mqtt stack up -c config/examples/unstable.yaml       # devices pause, delay or disconnect every ~2 min
monitoring-mqtt stack up -c config/examples/alerts-demo.yaml    # anomalies tuned for the alert engine
```

Containers and the network:

```bash
monitoring-mqtt stack restart broker                                # every client reconnects
docker stop  monitoring-mqtt-monitor-poultry-1                      # clean stop
docker kill  monitoring-mqtt-device-poultry-1                       # crash (Last Will)
docker start monitoring-mqtt-device-poultry-1                       # bring it back
docker network disconnect monitoring-mqtt_default monitoring-mqtt-device-poultry-1   # cut
docker network connect    monitoring-mqtt_default monitoring-mqtt-device-poultry-1
docker stop  monitoring-mqtt-api-1                                  # storage path down
```

What each one does to the data: [simulation.md](design/simulation.md#live-commands),
[failures.md](evaluation/failures.md).

## 5. Query the history

```bash
API=http://127.0.0.1:8000
curl -s $API/api/stats
curl -s "$API/api/devices?domain=poultry"
curl -s "$API/api/measurements/series?domain=cattle&bucket=10s&run_id=lab-01"
curl -s "$API/api/alerts?state=active"
curl -s "$API/api/runs/lab-01"
```

Interactive reference at `http://127.0.0.1:8000/docs`, or offline in
[reference/api/index.html](reference/api/index.html). Routes, pagination and errors:
[api.md](design/api.md).

## 6. Run experiments

```bash
monitoring-mqtt experiment list                         # scenarios in experiments/
monitoring-mqtt experiment run quick --dry-run          # validate and list the runs only
monitoring-mqtt experiment run quick                    # about 3 minutes: does the tooling work?
monitoring-mqtt experiment run qos -r 3                 # three repetitions of each point
monitoring-mqtt experiment run scale --duration 60s --warmup 15s   # shorter, for a try
monitoring-mqtt experiment run scale --resume results/scale/<campaign>   # continue after a stop
monitoring-mqtt experiment report results/qos/<campaign>          # summary.csv, charts, report.md
monitoring-mqtt stack up                                # back to config/simulation.yaml afterwards
```

See [experiments.md](evaluation/experiments.md).

## 7. Several machines

```bash
cp config/machines/gateway.env.example .env   # on the Raspberry Pi: broker, storage, alerts, dashboard
cp config/machines/field.env.example .env     # elsewhere: devices and monitors; set the gateway's address
monitoring-mqtt doctor                        # reaches the broker and the API? clocks in sync?
monitoring-mqtt stack up --build              # on each machine, its own roles
monitoring-mqtt test smoke                    # a round trip through the remote broker
docker context create gateway --docker host=ssh://pi@<gateway>   # experiments act on the gateway
docker --context gateway restart monitoring-mqtt-broker-1
```

See [distributed.md](guides/distributed.md) and
[deploy-raspberry-pi.md](guides/deploy-raspberry-pi.md).

## 8. Develop

```bash
monitoring-mqtt test unit                     # unit tests (= uv run pytest)
monitoring-mqtt test unit -k topics           # extra arguments go to pytest
monitoring-mqtt test integration              # real containers on an isolated stack, about 5 min
IT_KEEP_STACK=1 monitoring-mqtt test integration -k recovery   # keep the test stack for its logs
docker compose -p monitoring-mqtt-it down     # then remove it
uv run ruff check .                           # lint
uv run ruff format .                          # format (--check: verify only)
uv add <package>                              # a runtime dependency (--group dashboard, --dev)
```

Generated documents, checked by unit tests; regenerate after changing the CLI or the API:

```bash
uv run python -m monitoring_mqtt.cli.reference > docs/reference/cli.md
uv run python -m monitoring_mqtt.api.docs > docs/reference/api/openapi.json
uv run python -m monitoring_mqtt.api.docs --html > docs/reference/api/index.html
```

See [development.md](guides/development.md).

## 9. Docker directly

The CLI wraps Docker Compose with the fixed project name `monitoring-mqtt`; these are
useful when the CLI is not at hand:

```bash
docker compose -p monitoring-mqtt ps
docker compose -p monitoring-mqtt logs -f ingestion
docker stats --no-stream $(docker ps -q --filter label=com.docker.compose.project=monitoring-mqtt)
```
