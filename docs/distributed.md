# Running on several machines

By default one machine runs the whole stack: broker, devices and monitors. The same
project can also be split across machines. For example, the broker can run on a Raspberry
Pi while other computers run the devices and the monitors. The components, the contract
and the commands stay the same; only where each service runs changes.

```text
   PC A (devices)                 Raspberry Pi (broker)              PC B (monitors)
 ┌───────────────────┐          ┌─────────────────────┐          ┌────────────────────┐
 │ device-* × N      │ PUBLISH  │ broker              │ deliver  │ monitor-* × N      │
 │ MQTT_BROKER_HOST ─┼────────► │ 0.0.0.0:1883        │ ───────► │ MQTT_BROKER_HOST   │
 └───────────────────┘          │ worker registry     │          └────────────────────┘
                                └─────────────────────┘
```

## Machine settings

Each machine describes its part in its own `.env` (git-ignored, copied from
[`.env.example`](../.env.example)). Shell variables take precedence over the file.

| Variable | Default | Meaning |
|---|---|---|
| `STACK_ROLES` | all roles | Parts of the stack that run on this machine: `broker`, `devices`, `monitors`, `storage` (PostgreSQL, API, ingestion consumer), `alerts` (alert engine) |
| `MQTT_BROKER_HOST` | empty | Broker to use when this machine has no `broker` role: IP address, hostname, `127.0.0.1` for a tunnel on this machine, or `host.docker.internal` |
| `MQTT_BROKER_PORT` | `1883` | Port of that broker (only with `MQTT_BROKER_HOST`) |
| `MQTT_BIND_ADDRESS` | `127.0.0.1` | Address the local broker is published on; `0.0.0.0` accepts other machines |
| `MQTT_HOST_PORT` | `1883` | Port the local broker is published on |
| `API_BIND_ADDRESS` / `API_HOST_PORT` | `127.0.0.1` / `8000` | Where the API is published (`storage` role) |
| `NODE_NAME` | hostname where the CLI runs | Label of this machine in worker claims and monitor status. Set it in a Dev Container, whose hostname is not the machine's |

The CLI checks the combination before calling Docker. A machine either runs the broker
(`MQTT_BROKER_HOST` empty) or uses another one (`MQTT_BROKER_HOST` set, no `broker` role).

Every machine runs the same command, `monitoring-mqtt stack up`. Services of roles that
run elsewhere get 0 replicas on this machine. Without a local broker, the services start
with `--no-deps` and connect to `MQTT_BROKER_HOST`.

## Example: broker on a Raspberry Pi

**Raspberry Pi** (for example `192.168.0.50`):

```dotenv
STACK_ROLES=broker
MQTT_BIND_ADDRESS=0.0.0.0
```

**Device computers:**

```dotenv
STACK_ROLES=devices
MQTT_BROKER_HOST=192.168.0.50
```

**Monitor computer** (or add `monitors` to the Pi's roles):

```dotenv
STACK_ROLES=monitors
MQTT_BROKER_HOST=192.168.0.50
```

**History and alerts** on a computer with more memory than the Pi:

```dotenv
STACK_ROLES=storage,alerts
MQTT_BROKER_HOST=192.168.0.50
```

The ingestion consumer and the alert engine connect to the Pi's broker like any other
client; PostgreSQL and the API stay on this computer. Run `storage` and `alerts` on one
machine each: they use fixed client ids, so a second copy on another machine would take
over the first one's session.

Start the broker first, then the other machines:

```bash
monitoring-mqtt doctor                          # roles and broker connection
monitoring-mqtt stack up --build --run-id lab-01
monitoring-mqtt stack workers                   # every worker, from every machine
monitoring-mqtt test smoke
```

Devices tolerate a broker that is not up yet: they keep reconnecting with backoff, and
measurements generated meanwhile are dropped and leave a visible `sequence` gap.

## One experiment across machines

- **Run id.** Every machine must use the same run id. Pass `stack up --run-id <id>` on
  each machine, or set `run.run_id` in the experiment file. When the run id is generated
  automatically on a machine that takes part in a multi-machine setup, `stack up` prints a
  warning.
- **Different parts of the topology.** Each machine can apply its own experiment file with
  `stack up --config <file>`. For example, one computer runs only the poultry devices and
  another only the cattle devices. Keep the `mqtt` and `devices` sections identical in
  these files, so all devices follow the same experiment parameters.
- **Worker ids.** Device client ids contain the worker id, which derives from the
  container id. The CLI can only compare containers on its own machine, so every worker
  also claims its id in the broker before creating devices. A worker whose id is already
  claimed by another machine switches to a random id. `stack workers` marks those ids as
  `own id`. The protocol is described in [mqtt-contract.md](mqtt-contract.md#worker-registry).

## Checks

| Command | On a machine without a local broker |
|---|---|
| `monitoring-mqtt doctor` | Opens a TCP connection to `MQTT_BROKER_HOST:MQTT_BROKER_PORT`. Fails if it is unreachable |
| `monitoring-mqtt test smoke` | Runs the probe round trip against the remote broker. Live telemetry is required only when this machine has the `devices` role; otherwise it is reported as a warning |
| `monitoring-mqtt stack workers` | Reads the worker registry from the remote broker |

## Tunnels

`MQTT_BROKER_HOST` accepts any address the containers can reach, so a private network
such as a VPN or mesh network works by using the broker machine's address on it. Prefer
its IP address: names served by a local DNS resolver (mDNS `.local` names, or a VPN's own
DNS) are often not resolvable inside containers.

### Broker side

The broker port is published on the host's loopback by default
(`127.0.0.1:MQTT_HOST_PORT`). A tunnel agent running on the broker machine points to
`localhost:1883` (or the port set in `MQTT_HOST_PORT`). `MQTT_BIND_ADDRESS` stays
`127.0.0.1`, so only processes on that machine, such as the tunnel, can connect.

### Device and monitor side

Most tunnel clients publish the remote broker on a port of this machine's loopback, for
example `127.0.0.1:1883`. Point `MQTT_BROKER_HOST` to that address:

```dotenv
STACK_ROLES=devices
MQTT_BROKER_HOST=127.0.0.1    # or localhost: the tunnel endpoint on this machine
MQTT_BROKER_PORT=1883         # the port the tunnel client listens on
```

A container's loopback is the container itself, so the services connect to
`host.docker.internal:<port>` instead, which the stack maps to the machine running Docker:

- **Docker Desktop (macOS, Windows):** `host.docker.internal` reaches the host's loopback
  directly. Nothing else runs.
- **Docker Engine on Linux:** `host.docker.internal` reaches the Docker bridge gateway
  (usually `172.17.0.1`), not the loopback. `stack up` therefore also starts the
  `broker-relay` service, which runs in the host network namespace, listens on
  `<bridge gateway>:<port>` and forwards every connection to `127.0.0.1:<port>`.

```text
 device container ──► host.docker.internal:1883 = 172.17.0.1:1883
                        └─ broker-relay (host network) ──► 127.0.0.1:1883 ──► tunnel ──► broker
```

The relay forwards bytes without reading them and never listens on the machine's network
interfaces. If the tunnel is down, the relay closes each connection and logs a warning,
and the clients keep reconnecting until it is back. A host firewall that filters traffic
from containers to the host must allow the relay port on the Docker bridge.

If the tunnel client can listen on the bridge gateway or on `0.0.0.0` itself, set
`MQTT_BROKER_HOST=host.docker.internal` instead; no relay is needed then.

`doctor` checks the tunnel endpoint itself, and `test smoke` and `stack workers` go
through the same path as the services.

The stack speaks plain MQTT over TCP. Tunnels that require TLS or WebSockets on the client
side are not supported.

## Raspberry Pi

- Use a 64-bit OS with Docker Engine and the Compose plugin. The images used by the stack
  (`eclipse-mosquitto`, `python:3.12-slim`, `uv`) are published for `arm64`, and every
  machine builds the project images locally with `stack up --build`.
- Install the CLI with `python scripts/bootstrap.py`, as on any other host.
- Allow incoming TCP connections on the broker port (1883) in the Pi's firewall.
- Prefer a wired connection, or record the network used, because it affects latency and
  disconnections in experiments.

## Clocks

Monitors compute latency as the time of arrival minus the device timestamp. With devices
and monitors on different machines, latency is only meaningful when their clocks are
synchronized. Enable NTP on every machine, preferably against the same server. A Raspberry
Pi has no hardware clock and depends on NTP after every boot.

## Security and limits

- The broker allows anonymous connections. Publish it on `0.0.0.0` only on an isolated lab
  network or behind a private tunnel, never on the internet.
- The broker remains a single point of failure: when it stops, every machine stops
  exchanging data until it is back. Its worker registry is in memory, and workers publish
  their claims again when they reconnect.
