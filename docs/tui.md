# Terminal UI

`monitoring-mqtt tui` opens a read-only terminal UI that shows who publishes, what the
broker does and who consumes, live. It is built with [Textual](https://textual.textualize.io/).

```bash
monitoring-mqtt stack up
monitoring-mqtt tui            # q quits; the stack keeps running
```

## How it works

The UI is an **observer**: an ordinary MQTT subscriber with its own client id (`tui-<id>`)
and a clean session. It never publishes and never talks to Docker. Everything it shows
comes from the broker:

| Source | Topic | What it gives |
|---|---|---|
| Telemetry | `monitoring/v1/#` | Devices, values, sequence gaps, observed rates |
| Worker registry | `monitoring/registry/v1/workers/+` | Workers, their machine (`node`) and device types |
| Monitor status | `monitoring/status/v1/monitors/+` | Who consumes: subscribed filter, machine, counters, the latest messages it received, online/offline/stopped |
| Broker statistics | `$SYS/broker/#` | Mosquitto version, uptime, clients, publish rates |

MQTT cannot list subscribers, so the UI learns about consumers from the status that each
monitor publishes (see [mqtt-contract.md](mqtt-contract.md#observability-plane)): the topic
filter it subscribes to and the latest messages it **reported as received**. What a
consumer shows is therefore what it actually got, not what the UI expects it to get.

Where the UI shows who a device is routed to ("→ 2 monitors", "routed to …"), it applies
the broker's filter matching to the filters of the monitors that are online.

The UI runs as the one-off `tui` Compose service (`docker compose run --rm --no-deps tui`).
It therefore reaches the broker the same way as every other service: the local stack, a
remote broker or a tunnel through the `broker-relay`, from the host or a Dev Container.
When the broker restarts, the UI reconnects on its own and shows the interruption.

**The observer is also a subscriber.** It receives a copy of every telemetry message, so
the broker's publish-out rate includes the UI. The UI lists itself as `observer` and never
counts itself among the monitors.

## Screens

| Key | Screen | Shows |
|---|---|---|
| `1` | Overview | Machines → workers → devices with their latest value, trend and number of consumers; the broker summary; machines → monitor kinds with the filter they subscribe to → replicas, each with its rate, received count and the last message it received (`← device value #seq age`); per-domain flow over the last 60 s and where it is routed |
| `2` | Devices | One row per device: state, type, domain, machine, worker, site, value, trend, sequence, rate, missing messages, last seen, number of consumers. `Enter` opens the device with its last payload and the monitors that receive it |
| `3` | Monitors | One row per monitor: state, filter, machine, rate, received, invalid, devices seen, latency, uptime. Below, the selected monitor: the filter it subscribes to, its counters per domain and the last 10 messages it received (time, topic, value, sequence, the publisher's latest sequence, latency). `Enter` opens its raw status |
| `4` | Events | System changes: workers joining and leaving, monitors online/stopped/offline, broker connection, sequence gaps, restarts, invalid messages. `t` also includes every telemetry message |
| `5` | Broker | Mosquitto statistics and the history of observed and broker publish rates |

Other keys: `/` filters the Devices or Events table, `p` pauses the Events table, `h` hides
stopped and offline monitors and departed workers, `Esc` closes a detail or leaves a filter.

States:

| Mark | Device | Monitor |
|---|---|---|
| ● green | Published within 2.5 publish intervals | Online |
| ● bright | Just published (pulse) | — |
| ◐ yellow | Silent for longer (stale) | No status for 15 s |
| ○ | Its worker left the registry (gone) | Stopped cleanly |
| ✕ red | — | Offline: connection lost (Last Will) |

A worker with more than 6 devices appears as one summary line in the Overview; every
device is listed in the Devices screen.

## Scenarios

Each scenario uses the CLI (in a second terminal) while the UI shows the effect.

| Scenario | How | What the UI shows |
|---|---|---|
| Default stack | `stack up` | 3 devices, each consumed by exactly the monitor of its domain |
| Nobody consumes a domain | set its monitor to 0 replicas | Devices keep publishing; "→ nobody" and "no subscriber: the broker drops it" in red |
| Fan-out | `monitors: {poultry: {replicas: 20}, all: {replicas: 1}}` | Every poultry replica and the global monitor report the same messages (same device and sequence); broker publish out ≈ in × 21 |
| Monitor stops | `docker stop` a monitor | "monitor stopped" event; devices are no longer routed to it; devices are unaffected |
| Monitor crashes | `docker kill` a monitor | ✕ offline (published by the broker as its Last Will) |
| Broker restarts | `docker restart monitoring-mqtt-broker-1` | Observer disconnected/connected events, a missing message per device in the sequence, workers and monitors registering again |
| Invalid message | publish anything that is not a valid measurement | Invalid counter in the header and the reason in Events |
| Scale | e.g. 10 workers x 20 devices | Workers summarized in the Overview; all devices in the Devices screen |
| Worker crashes | `docker kill` a worker | "worker left" event; its devices turn ◐ and then ○ gone |
| Worker id collision | a worker with an existing `WORKER_ID` | The new worker appears with "own id" |
| Several machines | `STACK_ROLES`, `MQTT_BROKER_HOST`, `NODE_NAME` ([distributed.md](distributed.md)) | Publishers and subscribers grouped by machine |
| Persistent session | `mqtt: {clean_session: false}`, stop and start a monitor | "(restarted)" event; the monitor receives the queued messages at once: its received list shows older sequences arriving together (received count above rate x uptime) |
| Lagging consumer | any monitor | In its received list, "Publisher at" shows how far the publisher has moved since each message (`16 (+2)`) |

## Requirements

- An interactive terminal; the layout works best at 120 x 30 characters or larger.
  `monitoring-mqtt tui` exits with code 2 when standard input or output is not a terminal.
- `TERM` and `COLORTERM` are passed from your shell to the container (defaults:
  `xterm-256color` and `truecolor`).
