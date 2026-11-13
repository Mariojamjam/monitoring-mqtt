# Alerts

The alert engine (service `alerts`) is an independent subscriber with business logic. It
applies threshold rules to every measurement and publishes **derived events** on the
broker. It knows nothing about the database: the ingestion consumer stores its alerts like
any other event ([storage.md](storage.md)), and the terminal UI shows them live.

```text
devices ──measurements──► broker ──► alerts ──alert events──► broker ──► ingestion → API → database
                                                                     └──► terminal UI, any subscriber
```

## Rules

`config/alerts.yaml` (validated by `stack up` against the device catalog):

```yaml
rules:
  - id: ammonia-high
    device_type: ammonia_detector
    above: 25.0          # or below: …  (exactly one)
    severity: warning    # warning | critical
    after: 2             # values past the threshold in a row before raising (default 1)
    clear_at: 23.0       # clears only when back below this (hysteresis; default: threshold)
    clear_after: 2       # values past clear_at in a row before clearing (default 1)
```

| Rule | Device type | Raises when | Severity | Clears when |
|---|---|---|---|---|
| `oxygen-low` | dissolved oxygen | < 5.0 mg/L, 2 in a row | warning | ≥ 5.3, 2 in a row |
| `oxygen-critical` | dissolved oxygen | < 4.0 mg/L | critical | ≥ 4.5 |
| `oxygen-high` | dissolved oxygen | > 8.0 mg/L, 2 in a row | warning | ≤ 7.7, 2 in a row |
| `ammonia-high` | ammonia | > 25 ppm, 2 in a row | warning | ≤ 23, 2 in a row |
| `ammonia-critical` | ammonia | > 35 ppm | critical | ≤ 32 |
| `rumen-ph-low` | rumen pH | < 5.8, 2 in a row | warning | ≥ 5.9, 2 in a row |
| `rumen-ph-high` | rumen pH | > 6.8, 2 in a row | warning | ≤ 6.7, 2 in a row |

Thresholds are synthetic lab parameters, consistent with the normal bands in
`config/devices.yaml`. `after` ignores a single noisy value; `clear_at` keeps an alert from
flapping when the value hovers around the threshold. A value can break several rules at
once: warning and critical are separate alerts. The experiment chooses the rules file
(`alerts.rules` in `config/simulation.yaml`) or turns the engine off (`alerts.enabled`).

## States

Each (device, rule) pair has its own state:

```text
NORMAL ──(after violations in a row)──► RAISED      publishes `raised` with a new alert_id
RAISED ──(clear_after values back)────► NORMAL      publishes `cleared` with the same alert_id
```

- **Duplicates:** a QoS 1 duplicate (same `event_id`) is evaluated once; an older
  measurement (lower `sequence`) is ignored.
- **Device restart:** `sequence` back to 1, or a new run, starts the counting over.
- **Engine restart:** open alerts are kept. See below.

## Topics

```text
monitoring/alerts/v1/events/{domain}/{severity}/{device_id}   raised / cleared, QoS 1, not retained
monitoring/alerts/v1/active/{device_id}/{rule_id}             the open alert, retained; empty = none
```

Both are outside `monitoring/v1/#`, so monitors never receive them. The event payload is
described in [mqtt-contract.md](mqtt-contract.md#alerts). Events are the history; the
retained active topic tells a newcomer at once which alerts are open (the terminal UI
subscribes to both).

## Restarts

The engine uses the fixed client id `alerts` with a persistent session, so the broker
queues the measurements published while it is down. Before connecting, it reads the
retained active alerts and restores them. After a restart, an open alert is neither raised
again nor forgotten: it is cleared with its original `alert_id` when the values come back.
The integration tests check this. Only one engine may run at a time; `stack up` checks it.

## Live

```bash
monitoring-mqtt device anomaly poultry-3f9a1c-001 --for 30s   # make one device anomalous
monitoring-mqtt tui                                           # Mode: anomaly ↑ + alert warning; `a` lists open alerts
curl -s "http://127.0.0.1:8000/alerts?state=active"
docker compose -p monitoring-mqtt logs -f alerts              # every transition is logged
```

## Evaluation against the ground truth

Workers announce every simulated anomaly on the simulation plane (ground truth,
[simulation.md](simulation.md#ground-truth)); the API stores every alert. Comparing them
measures the rules: how many anomalies are detected, how fast, and how many alerts are
false alarms.

Measured with `config/examples/alerts-demo.yaml`: 6 devices (2 per type), an anomaly about
every minute per device lasting 30 s, publications every 5 s, the shipped rules, 9.5
minutes. A recorder subscribed to the ground truth during the run; the alerts came from
`GET /alerts?run_id=…`. An episode counts as detected when an alert of the right direction
(`above` for a high anomaly) was raised on that device between the start of the episode
and 15 s after its end.

| Measure | Result |
|---|---|
| Anomaly episodes | 36 (oxygen 12, ammonia 14, rumen pH 10) |
| Detected by a new alert | 33 |
| Already covered by an open alert | 3: each started 0–5 s after the previous anomaly of the same device ended, while that alert was still open (values had not come back yet), so no second alert was raised. No anomaly went unalerted |
| Episodes that also reached a critical alert | 20 |
| Alerts raised | 48 (warning and critical are separate alerts) |
| False alarms (no anomaly behind them) | 0 |
| Detection delay (episode start → alert raised) | median 10 s, mean 10.6 s, from 5 s to 25 s |

Reading the delay: values move gradually (mean-reverting generator) and most rules need 2
values past the threshold in a row (`after: 2`). At one value every 5 s, about 10 s is the
expected price of not alerting on noise. Lower `after` or the publish interval to detect
faster, at the risk of false alarms with noisier generators. The integration suite runs the
same comparison at one value every 0.5 s: detection within 5 s.
