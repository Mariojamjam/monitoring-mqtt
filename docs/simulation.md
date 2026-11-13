# Simulation: values, anomalies and faults

Two files define what devices do:

| File | Defines | Example |
|---|---|---|
| `config/devices.yaml` | What each device type **is**: how its value evolves, its normal band, where an anomaly takes it | dissolved oxygen around 6.5 mg/L, normal between 5 and 8 |
| `config/simulation.yaml` (`behavior:`) | What **happens** during an experiment | an anomaly about every 2 minutes; device 002 off at 1 minute |

Every value is synthetic. The ranges are lab parameters, not veterinary references.

## How values evolve

Each device has its own generator. With `kind: mean_reverting`, the value drifts and is
pulled back towards the device's mean at every publication:

```text
next = clip(value + alpha * (mean - value) + gauss(0, noise), minimum, maximum)
```

- `alpha` is the share of the distance to the mean recovered at each step. A small value
  gives smooth, slow changes; `1` jumps straight to the mean.
- `site_spread` shifts each device's mean by up to that amount, so two ponds are never
  identical. The shift is drawn from the experiment seed and the device id.
- `kind: bounded_noise` gives independent values around a center instead (the earlier
  model; still available).

All randomness comes from the experiment `seed` and the device id, so the same experiment
produces the same values. Each feature uses its own random stream: turning anomalies on
does not change the normal noise.

| Type | Mean | Normal band (`operating_range`) | Anomaly targets |
|---|---|---|---|
| `dissolved_oxygen_probe` | 6.5 mg/L | 5.0–8.0 | low 3.0, high 10.0 |
| `ammonia_detector` | 12 ppm | 0–25 | high 40 |
| `rumen_ph_bolus` | 6.3 | 5.8–6.8 | low 5.2, high 7.5 |

With the shipped catalog, normal values never left the band in 20,000 samples per type,
and an anomaly left the band within 4 publications (20 s at the default interval). Use
`lasts` of at least 30 s.

## Episodes

An episode is a period when a device behaves differently:

| Kind | What the device does | What consumers see |
|---|---|---|
| `anomaly` | Its generator is steered to an anomaly target (`high` or `low`) and back | Values leave the normal band gradually and return |
| `pause` | Stays connected, generates nothing | Silence; the sequence continues without a gap |
| `delay` | Generates on time, keeps the measurements, sends them all when the delay ends | A late burst with high latency, in order |
| `disconnect` | Disconnects; generates and drops measurements | A gap in the sequence |
| `stop` | Switched off: disconnects and generates nothing | Silence; the sequence continues without a gap |

A device has two independent families: its **values** (anomaly) and its **availability**
(pause, delay, disconnect, stop). An anomaly can happen while the device is delayed, for
example. Episodes apply to one device only; its neighbours in the same worker container
keep publishing normally.

When a device comes back after `stop` or `disconnect`, it generates nothing until its
connection is open again (at most 10 s), like a device that boots and then connects. So a
`stop` leaves no gap even when reconnecting takes longer than one publish interval.

## Configuring episodes

```yaml
behavior:
  anomalies:
    every: 2m            # average time between anomalies of one device
    lasts: 30s           # default 30s
    direction: both      # high | low | both (default both)
    only: [ammonia_detector]   # optional: only these device types

  instability:
    every: 3m
    lasts: 20s           # default 20s
    kinds: [pause, delay, disconnect]   # default: all three

  faults:                # exact, scheduled episodes
    - device: poultry-*-002   # device id; * and ? match anything
      at: 1m                  # after the worker starts (0s = right away)
      lasts: 30s              # omit: until the end of the run
      action: stop            # stop | pause | delay | disconnect | anomaly
    - {device: cattle-*-001, at: 2m, lasts: 1m, action: anomaly, direction: low}
```

- Omit a block to turn it off. Without `behavior`, devices behave normally.
- Durations need a unit: `500ms`, `30s`, `2m`, `1h`. `30` alone is rejected.
- `every` is an average. The gaps between episodes are random (exponential), drawn from the
  seed, so they repeat in every run of the same experiment.
- `stack up` validates the section before starting anything and prints it.
- When several episodes of the same family overlap, a live command wins over a fault,
  and a fault wins over a random episode.

Ready-made experiments in `config/examples/`:

```bash
monitoring-mqtt stack up -c config/examples/device-fault.yaml   # one device off, one disconnected
monitoring-mqtt stack up -c config/examples/anomalies.yaml      # anomalies about once a minute
monitoring-mqtt stack up -c config/examples/unstable.yaml       # pauses, delays, disconnections
```

## Live commands

```bash
monitoring-mqtt device stop poultry-3f9a1c-002 --for 30s
monitoring-mqtt device anomaly cattle-3f9a1c-001 --direction low --for 1m
monitoring-mqtt device pause poultry-3f9a1c-003           # until resume
monitoring-mqtt device resume poultry-3f9a1c-003          # ends every active episode
```

Commands: `stop`, `pause`, `delay`, `disconnect`, `anomaly`, `resume`. Device ids are
listed by the terminal UI and come from `stack workers`.

The command travels through the broker like everything else. The CLI runs the one-off
`control` service, which publishes on the device's control topic. The worker hosting the
device acknowledges it, and the CLI reports the answer:

| Exit code | Meaning |
|---|---|
| 0 | Accepted; it applies at the device's next publish time |
| 1 | Rejected, e.g. `anomaly --direction low` on an ammonia detector |
| 2 | Invalid arguments, e.g. a malformed device id or `--for 30` |
| 3 | No worker answered: the device does not exist or its worker is down |
| 4 | Broker unreachable |

## Ground truth

Workers announce every episode start and end on `monitoring/sim/v1/devices/{device_id}`
([contract](mqtt-contract.md#simulation-plane)). Monitors never subscribe to it. It
exists to check consumers: an alert engine, for example, is evaluated by comparing its
alerts with these events. The terminal UI shows the episodes in effect in the Devices
screen and lists them in Events.
