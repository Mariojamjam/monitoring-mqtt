# Deploying on a Raspberry Pi (gateway) and other machines

A step-by-step guide to the distributed setup: the **gateway**, for example a Raspberry
Pi, runs the broker, the history (PostgreSQL, API, ingestion consumer), the alert engine
and the dashboard; **field** machines run the devices and the monitors. Roles and machine
settings are described in [distributed.md](distributed.md). Ready-made `.env` files for
both machines are in [`config/machines/`](../config/machines).

```text
 field machine (devices, monitors)              gateway (Raspberry Pi)
 ┌───────────────────────────┐  MQTT :1883  ┌──────────────────────────────────────┐
 │ device-* ── monitor-*     │ ───────────► │ broker ─► ingestion ─► api ─► postgres │
 │ TUI, experiment runner    │              │        └► alerts      dashboard :8501 │
 └───────────────────────────┘              └──────────────────────────────────────┘
                     browser ──── HTTP :8501 (dashboard), :8000 (API reference) ───┘
```

Only MQTT and HTTP cross the network: MQTT from the field machines to the broker; HTTP
from browsers to the dashboard and to the API reference, and from experiment probes on
the field machines to the API.

## 1. The gateway

**Hardware and system.**

- A Raspberry Pi 4 or 5 with 4 GB of memory or more.
- **Raspberry Pi OS 64-bit** (`uname -m` prints `aarch64`). The images and every locked
  dependency are checked for 64-bit ARM in CI; 32-bit systems are not supported.
- Preferably an SSD on USB for the system or at least for Docker's data
  (`/var/lib/docker`). PostgreSQL writes continuously, which wears out SD cards.
- A wired network connection, or at least a record of which network was used: it affects
  latency and disconnections.

**Software.**

```bash
curl -fsSL https://get.docker.com | sh          # Docker Engine and the Compose plugin
sudo usermod -aG docker $USER                   # then log out and in again
sudo systemctl enable docker                    # start Docker, and the stack, at boot
sudo apt install -y chrony git                  # chrony: NTP (the Pi has no hardware clock)
git clone <repository> monitoring-mqtt && cd monitoring-mqtt
python3 scripts/bootstrap.py                    # uv, Python 3.12 and the CLI
```

**Machine settings.**

```bash
cp config/machines/gateway.env.example .env     # roles, exposed ports, limits
```

Edit `.env`: set `POSTGRES_PASSWORD` before the first start, and keep or adjust the memory
limits. The gateway publishes the broker, the dashboard and the API on the network
(`MQTT_BIND_ADDRESS`, `DASHBOARD_BIND_ADDRESS` and `API_BIND_ADDRESS` set to `0.0.0.0`).

**Start.**

```bash
monitoring-mqtt doctor                          # Docker, roles, clock
monitoring-mqtt stack up --build                # the first build takes a while on a Pi
monitoring-mqtt stack status
```

The services restart by themselves after a failure or a reboot
(`restart: unless-stopped`). Their logs are rotated (10 MB × 3 files per container), so
they never fill the card.

**Firewall.** Allow incoming TCP 1883 (MQTT), 8501 (dashboard) and 8000 (API) from the
lab network only.

## 2. A field machine

```bash
git clone <repository> monitoring-mqtt && cd monitoring-mqtt   # the same commit as the gateway
python3 scripts/bootstrap.py
cp config/machines/field.env.example .env
```

Edit `.env`: set `MQTT_BROKER_HOST` and `REMOTE_API_URL` to the gateway's address. Then:

```bash
monitoring-mqtt doctor                          # reaches the broker? clock synchronized?
monitoring-mqtt stack up --build
monitoring-mqtt test smoke                      # a round trip through the gateway's broker
monitoring-mqtt stack workers                   # every worker, from every machine
monitoring-mqtt tui                             # the live view, through the gateway
```

The dashboard is at `http://<gateway>:8501`, from any browser on the network.

## 3. Experiments across machines

Experiments run from the field machine (it runs the devices) and measure the whole path.
Let it sample the gateway's containers and act on them, through a Docker context over SSH:

```bash
ssh-copy-id pi@<gateway>                                      # key-based SSH login
docker context create gateway --docker host=ssh://pi@<gateway>
docker --context gateway ps                                   # works? then:
echo "EXPERIMENT_DOCKER_CONTEXTS=gateway" >> .env
monitoring-mqtt experiment run quick
```

The `broker-failure` scenario then restarts the gateway's broker, and the resource
columns include the gateway's containers ([experiments.md](experiments.md#across-machines)).

## 4. Checks and troubleshooting

| Symptom | Check |
|---|---|
| `doctor` cannot reach the broker | The gateway's firewall (TCP 1883), `MQTT_BIND_ADDRESS=0.0.0.0` on the gateway, the address in `MQTT_BROKER_HOST` (prefer an IP address: `.local` names often do not resolve inside containers) |
| `doctor` warns about the clock | `chronyc tracking` on both machines; latencies across machines include the clock difference |
| The dashboard shows "API unavailable" | `monitoring-mqtt stack status` on the gateway; `docker compose -p monitoring-mqtt logs api` |
| Devices reconnect again and again | Wi-Fi quality; the keepalive (`mqtt.keepalive_seconds`) detects a lost link after 1.5× its value |
| The Pi runs out of memory | Lower the limits in `.env`, or move the dashboard to another machine (`STACK_ROLES` without `dashboard` on the gateway, `dashboard` plus `REMOTE_API_URL` elsewhere) |

## Security

The broker accepts anonymous connections. On the gateway it listens on the network, so use
it only on an isolated lab network or behind a private tunnel ([distributed.md](distributed.md#tunnels)).
The API and the dashboard have no authentication either. MQTT and HTTP travel unencrypted.
