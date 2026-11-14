# Dashboard

The dashboard shows the **stored history** in a browser: values over time against their
normal band and the alert thresholds, the alerts with their duration, and runs side by
side. It is a [Streamlit](https://streamlit.io) application served by the `dashboard`
service.

```bash
monitoring-mqtt stack up          # starts it with the rest of the stack
monitoring-mqtt dashboard         # opens http://127.0.0.1:8501 in the browser
```

## Where its data comes from

```text
broker ─► ingestion ─► api ◄── HTTP GET (polling, cached) ── dashboard ◄── browser
          alerts  ─►    │
                     postgres
```

The dashboard talks **only to the API**. It never subscribes to MQTT and never opens the
database, so:

- if it stops, nothing else notices: devices, monitors, the ingestion consumer and the alert
  engine keep running, and the history keeps growing (an integration test checks it);
- it can run on any machine that reaches the API, for example a laptop
  ([distributed.md](distributed.md));
- what it draws besides the data, the normal band of each device type and the thresholds
  of the alert rules, also comes from the API (`GET /api/catalog`), so it reads no
  configuration file and always shows the rules the alert engine applies.

The terminal UI ([tui.md](tui.md)) is the other interface, and the opposite style: the
broker **pushes** every message to it as it happens. The dashboard **pulls** what was
stored. Together they show both ways of communicating in the system.

## Pages

The sidebar is shared by every page: the **run** (the newest by default, shown as `Current (run id)`, or all runs), the
**time window** (15 min to 24 h, or the whole run), the **domain**, and **live** refresh.
Times are in UTC everywhere, like the API and the logs.

| Page | What it shows |
|---|---|
| **Overview** | Measurements stored and devices of the run, open alerts (critical ones counted apart), the average delay from the device to the database, the last write. One chart per domain: the average of its devices, with their minimum and maximum, over the normal band. Every device with its latest value, whether it is inside its normal band, how long ago it was seen and its open alerts. The open alerts |
| **Devices** | One device. Its series (average per bucket, minimum-maximum band) over the normal band, with the alert thresholds as dashed lines and the periods with an alert shaded in the severity colour. Its measurements with the three times (generated, received, stored) and the delays between them; CSV download |
| **Alerts** | Filtered by state, severity and rule. A timeline with one bar per alert, from raised to cleared (an open alert reaches now); count and mean and maximum duration per rule; every alert with the value that raised and the one that cleared it; CSV download |
| **Runs** | Every run. Two runs side by side: duration, devices, measurements, measurements per second, missing measurements (sequence gaps), delays from the device to the ingestion consumer and to the database (average, p95, maximum), alerts per severity, measurements per domain, and the difference between them |

Charts adapt their bucket to the window, about 180 points: 5 s buckets for 15 minutes,
30 s for an hour, 15 minutes for a day. Hovering a chart shows the numbers of the nearest
bucket.

## How it refreshes

Streamlit runs the page script again from top to bottom on every interaction. Two
mechanisms keep that cheap:

- **Live blocks are fragments** (`st.fragment(run_every=…)`): every `dashboard.refresh`
  (5 s by default) only the data part of the page runs again; filters and the selected
  device stay as they are.
- **API answers are cached** for 2 seconds (`st.cache_data`), keyed by the query; the
  catalog for a minute. The start of the time window is rounded to the refresh interval,
  so reruns and several open browsers ask the same question and share one answer.

Every query is bounded: lists follow the API's cursor up to a few thousand items, and
series are aggregated by the database (`GET /api/measurements/series`).

## When the API is down

Each page shows a warning ("API unavailable … retrying") instead of an error, and keeps
showing the last data it received; it recovers by itself on the next refresh. With the
database down, the API answers 503 and the dashboard behaves the same way.

## Settings

| Where | Setting | Default |
|---|---|---|
| `config/simulation.yaml` | `dashboard.enabled` | `true` |
| `config/simulation.yaml` | `dashboard.refresh`: live refresh (`0s` turns it off) | `5s` |
| `.env` | `DASHBOARD_BIND_ADDRESS`, `DASHBOARD_HOST_PORT`: where it is published | `127.0.0.1`, `8501` |
| `.env` | `DASHBOARD_API_URL`: the API it reads, when the API runs on another machine | this stack's API |
| `.env` | `STACK_ROLES`: includes `dashboard` on the machine that runs it | every role |

Like the API, the dashboard has no authentication: it is published on the loopback
address by default.

## Image

The dashboard has its own image (`monitoring-mqtt/dashboard`, the `dashboard` target of
the `Dockerfile`): the application plus Streamlit, pandas and Altair, about 580 MB of
dependencies. Every other service uses the `app` image, which stays small because device
containers also run on Raspberry Pis. The container runs headless, never sends Streamlit's
usage statistics and never looks up the machine's public address.

## Code

`src/monitoring_mqtt/dashboard/` depends only on `core` (a unit test checks it):

| Module | Role |
|---|---|
| `client.py` | Typed HTTP client: follows cursors with a limit; connection errors and 503 become `ApiUnavailableError` |
| `data.py` | Pure transformations: API answers → pandas tables (alert intervals, rule summary, run comparison, bucket size) |
| `charts.py` | Altair charts: series with bands, thresholds and alert periods; alert timeline |
| `context.py` | What pages share: the cached API, the sidebar, live fragments, outages |
| `views/` | One module per page (not `pages/`, which Streamlit would turn into its own navigation) |
| `app.py`, `__main__.py` | Navigation; `python -m monitoring_mqtt.dashboard` serves it |

The unit tests run every page with Streamlit's `AppTest` (no browser) against a fake API;
the integration suite runs them against the real API of the test stack.

To run it outside Docker, against a stack running on this machine:

```bash
API_URL=http://127.0.0.1:8000 uv run python -m monitoring_mqtt.dashboard --server.port 8502
```
