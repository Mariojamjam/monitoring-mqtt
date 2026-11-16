# Experiments

An experiment answers one question about the system with numbers, for example "what does
QoS 1 cost compared with QoS 0?" or "how many devices does one machine carry?". Each one is
a **scenario**: a versioned file that says what varies, how long to measure and how many
times to repeat. The CLI runs it, measures every run from the outside and writes the
results; a second command turns them into tables and charts.

```bash
monitoring-mqtt experiment list                       # scenarios in experiments/
monitoring-mqtt experiment run quick --dry-run        # validate, list the runs, start nothing
monitoring-mqtt experiment run qos                    # run it (results/qos/<campaign>/)
monitoring-mqtt experiment report results/qos/<campaign>
```

`experiment run` options: `--repetitions N`, `--duration 60s`, `--warmup 10s` (override the
scenario, for a quick try) and `--resume <folder>` (continue an interrupted campaign: runs
already measured are kept).

## What happens in one run

```text
stack up (run id qos-<campaign>-p1-r1)
   │  the probe waits until every device reports
   ├─ warm-up ─────────┬──────── measured window ────────┬── settle ──► API: stored in the window
   │                   │ counters at both edges          │
   │                   │ broker stats every 1 s          │
   │                   │ CPU/memory every 2 s            │
   │                   │ failure actions on this clock   │
```

1. The CLI writes the run's experiment file (the base experiment, the scenario's `set`,
   then the sweep point) and starts it with `stack up`: every run has its own **run id**,
   so its data never mixes with another's.
2. The **probe** (`experiment-probe`, a one-off container like the TUI) connects to the
   broker as an observer. It waits until every device of the run reports, then the
   warm-up, then opens the **window**. It records the status of every worker and monitor
   ([mqtt-contract.md](mqtt-contract.md)) and samples the broker's own statistics (`$SYS`,
   refreshed every 2 s).
3. During the window the CLI samples CPU and memory of every container (`docker stats`)
   and runs the scenario's failure actions at their times.
4. When the window closes, the probe waits until the ingestion consumer has stored
   everything (buffer empty, count stable), computes the counters at both edges of the
   window, and asks the API what was stored for the measurements generated in it
   (`GET /api/runs/{run_id}/window`).

Only the window counts: start-up, warm-up and the settling time are excluded.

## Metrics

Every run gives one row of `runs.csv`. The main columns:

| Column | Definition |
|---|---|
| `published`, `published_per_s` | Measurements the devices handed to MQTT in the window (worker counters, end minus start) |
| `acked`, `acked_ratio` | Publications confirmed: the broker's PUBACK with QoS 1; written to the socket with QoS 0 (nothing more is known) |
| `failed` | Measurements a device could not send (disconnected, or refused by the client) |
| `monitor_received`, `monitor_duplicates` | Deliveries to the monitors, and those of an `event_id` the same monitor had already received |
| `monitor_delivery` | Unique messages the monitors received ÷ messages published on the domains their filters match |
| `stored`, `stored_per_s` | Measurements generated in the window and stored in the database |
| `sequence_gaps` | Missing sequence numbers among them: generated but never stored |
| `storage_delivery` | `stored ÷ (stored + sequence_gaps)` |
| `ingestion_duplicates` | Events the API had already stored (QoS 1 redeliveries, retried batches): stored once |
| `latency_mqtt_p50/p95/p99_ms` | Device timestamp → reception by the ingestion consumer: the MQTT path |
| `latency_storage_p50/p95/p99/max_ms` | Device timestamp → database. Includes the ingestion batching (`storage.flush_interval`, 1 s by default) |
| `broker_messages_received/sent_per_s` | The broker's own counters (`$SYS`): every message, telemetry and status alike |
| `cpu_<role>_avg/max`, `mem_<role>_avg/max_mib` | CPU in % of one core and memory per role (broker, devices, monitors, ingestion, api, postgres, alerts, dashboard), summed over its containers |
| `action1_recovery_s` | After a failure action: time until every device that delivered in the 10 s before it has delivered again, counted from the end of the failure (when the container came back) |
| `action1_silence_max_s` | The longest time a device went without delivering around the failure |
| `containers_failed` | Containers that exited with an error or were killed for lack of memory |

**What the numbers do not say.**

- A PUBACK means the broker took the message, not that anyone stored it: compare `acked`
  with `stored`.
- Latencies subtract two clocks. On one machine they share a clock. Across machines they
  include the clock difference, so the machines must be synchronized (NTP). The
  `manifest.json` records where the run happened.
- Publisher and monitor counters come from statuses refreshed every 5 s. The probe keeps
  every status and interpolates the counters at the exact start and end of the window,
  so totals are accurate to a few messages, and a ratio slightly above 1 can appear. The
  database figures are exact (sequence numbers).
- A larger number of containers is not proof of scale; the delivery, latency and
  resource columns are.

## The scenarios

| Scenario | Question | Varies | Runs |
|---|---|---|---:|
| `quick` | Does the tooling work here? (about 3 min) | publish interval 1 s, 0.5 s | 2 |
| `scale` | How many devices does one machine carry? | 3 → 50 → 100 → 250 → 500 devices, one MQTT connection each; **stops at the first saturated point** | 15 |
| `granularity` | One device per container, or many? | 50 devices as 50×1, 10×5, 5×10, 2×25, 1×50 | 15 |
| `frequency` | How fast can devices publish? | 20 devices, interval 5 s → 0.1 s (4 → 200 messages/s); stops when saturated | 15 |
| `qos` | What does "at least once" cost? | QoS 0, QoS 1 (50 devices at 2 messages/s each) | 6 |
| `fanout` | What does each extra subscriber cost the broker? | 1, 5, 20 monitors on the same filter | 9 |
| `broker-failure` | What happens when the broker restarts? | clean or persistent sessions; broker restarted 60 s into the window | 6 |
| `worker-failure` | What does a crashed container take with it? | one of two workers killed for 10 s | 3 |
| `subscriber-failure` | Do publishers notice a dead subscriber? Does it lose messages? | clean or persistent sessions; a monitor stopped for 20 s | 6 |
| `device-failure` | Stop versus disconnection of one device | `stop` or `disconnect` for 30 s (simulation faults, [simulation.md](simulation.md)) | 6 |

A point is **saturated** when the worst repetition stores less than 99% of what was
generated, its p95 delay to the database exceeds 5 s, or a container fails. With
`stop_when_saturated: true`, larger points are skipped and the report says why.

## Writing a scenario

```yaml
schema_version: 1
name: qos                       # lowercase, used in run ids and the results folder
description: QoS 0 versus QoS 1
base: config/simulation.yaml    # any experiment file
warmup: 30s                     # excluded from the metrics
duration: 2m                    # the measured window
repetitions: 3
set:                            # fixed overrides, dotted paths into the experiment file
  run.log_level: WARNING        # devices log every publication at INFO: too much at scale
  devices.publish_interval_seconds: 0.5
  workers.ammonia_detector: {replicas: 5, devices_per_worker: 10}
sweep:                          # one point per combination (the first key varies slowest)
  mqtt.qos: [0, 1]
actions:                        # failures, on the clock of the window
  - {at: 60s, do: restart, service: broker}
  - {at: 90s, do: stop, service: monitor-poultry, for: 20s}
  - {at: 90s, do: kill, service: device-poultry, index: 1, for: 10s}
observe_telemetry: true         # record arrivals per device (default: on with actions)
stop_when_saturated: false
saturation: {min_delivery: 0.99, max_p95_ms: 5000}
report:
  x: devices                    # x axis (default: the first sweep key)
  charts: [throughput, latency, latency_storage, delivery, acked, duplicates, gaps,
           cpu, memory, memory_per_device, broker, recovery]
```

Every run's experiment file is validated against the device catalog before anything
starts (`--dry-run` stops there).

**Actions:**
- `restart` restarts every container of the service.
- `stop` stops them and starts them again after `for`.
- `kill` kills one replica (`index`), as a crash, and starts it again after `for`. Docker
  does not restart a killed container by itself.
- Device-level failures use the simulation's own `behavior.faults`, which also publish the
  ground truth.

## Results and report

```text
results/qos/20261116T140000Z/        (git-ignored)
├── manifest.json        scenario, git commit, version, machine, start and end
├── scenario.yaml        the scenario as it was run
├── runs.csv, runs.jsonl one row per run
├── summary.csv          one row per sweep point (after `experiment report`)
├── report.md, charts/   the report
└── runs/<run_id>/       experiment.yaml, stack-up.log, probe.log, result.json, resources.csv
```

`experiment report` aggregates the repetitions of each point: mean, standard deviation,
95% confidence interval (Student's t), minimum and maximum. It draws one PNG per chart (the
mean with the interval as error bars) and writes `report.md` with the method, the
environment, a summary table and the charts. It runs on the host with the `experiments`
dependency group (pandas, matplotlib), which `uv sync` installs.

The stack keeps running the last experiment after a campaign; `monitoring-mqtt stack up`
returns to `config/simulation.yaml`.

## Across machines

Run experiments from the machine with the **devices** role; it starts its own part with
each run. The other machines (for example the broker, storage and dashboard on a
Raspberry Pi) keep running their part. For the runner to sample their containers and act
on them (restart the remote broker), give it their **Docker contexts**:

```bash
docker context create pi --docker host=ssh://pi@192.168.0.50   # once, on this machine
```

```dotenv
EXPERIMENT_DOCKER_CONTEXTS=pi
REMOTE_API_URL=http://192.168.0.50:8000      # the probe asks the API on the Pi
```

An action finds the service on whichever machine runs it. A scenario's overrides apply to
the services of the runner's machine only. A QoS sweep changes the devices' and the local
consumers' QoS, while the remote ingestion consumer keeps its own; the broker delivers with
the lower of the two.
