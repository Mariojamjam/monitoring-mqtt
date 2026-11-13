# Storage: from the broker to the database

Live consumers only see what is published while they are connected. The storage path keeps
the **history**: every measurement and every alert, queryable later through the API
([api.md](api.md)).

```text
broker ──MQTT (QoS 1, persistent session)──► ingestion ──HTTP POST, batches──► api ──SQL──► postgres
```

| Service | Module | Role |
|---|---|---|
| `ingestion` | `consumers.ingestion` | Subscribes to every measurement (`monitoring/v1/#`) and alert event (`monitoring/alerts/v1/events/#`), validates them and posts them to the API in batches. Never opens a database connection |
| `api` | `api` (FastAPI) | The **only** component that talks to the database: stores what the ingestion consumer sends and answers queries |
| `postgres` | PostgreSQL 18 | The data, in the volume `pgdata`. Not published on the host |
| `migrate` | `storage.migrate` | Runs once on every `stack up`: applies the schema migrations (Alembic), then exits. The API starts only after it succeeded |

All four run with the `storage` role ([distributed.md](distributed.md)) when the experiment
has `storage.enabled: true` (the default).

## Why a bridge, and not the API subscribing to MQTT

The API stays a plain HTTP service. It can be restarted, scaled or tested without a broker,
and the MQTT side stays in the consumers. The ingestion consumer is the only place where the
two worlds meet. It also lets the API answer queries while the broker is down, and lets the
ingestion consumer keep events while the API is down.

## Delivery guarantees, hop by hop

| Hop | Mechanism | What happens when the next hop is down |
|---|---|---|
| broker → ingestion | QoS 1 with a **persistent session** and the fixed client id `ingestion` | The broker queues the measurements for the consumer (up to 1000, sessions expire after 1 h) and delivers them when it reconnects |
| ingestion → API | In-memory buffer (`storage.buffer_limit`, default 50,000 events); batches of `storage.batch_size` (500) at least every `storage.flush_interval` (1 s) | The batch stays in the buffer and is sent again with exponential backoff (0.5 s to 10 s). When the buffer is full, the oldest events are dropped and counted |
| API → database | One transaction per batch | The API answers **503** with `Retry-After`; the consumer keeps the batch and retries |
| database | `INSERT … ON CONFLICT (event_id) DO NOTHING` | A repeated event (QoS 1 duplicate, retried batch) is stored once |

The result is at-least-once delivery to the API with idempotent storage: every event is
stored exactly once, unless a limit is exceeded. Two limits remain, and both show up as
`sequence` gaps in the stored history:

- events in the ingestion buffer are lost if the consumer **process dies** (the broker
  already got its acknowledgement);
- the broker queues at most 1000 messages for a disconnected consumer.

Because the client id is fixed, only one ingestion consumer may run at a time; `stack up`
checks it.

## Measured

Default experiment (3 devices, one measurement every 5 s each), one hop stopped for 30 s
with `docker stop`, then started again:

| Stopped | Ready again after start | Outage stored after start | Stored | Gaps | Longest delay to the database |
|---|---|---|---|---|---|
| `api` | 3.3 s | 3.6 s | 21 measurements | 0 | 34.8 s (held in the ingestion buffer) |
| `postgres` | 0.6 s | 1.5 s | 18 | 0 | 28.1 s (API answered 503; buffer) |
| `ingestion` | — | 2.4 s | 18 | 0 | 31.1 s (queued by the broker) |

A longer, unplanned outage confirmed it: with the API unreachable for about 5 minutes, the
consumer buffered 189 measurements and stored all of them when it came back: 224 per
device over the run, **no gap**, the oldest stored 311 s after it was generated.

In normal operation an event reaches the database about 1 s after it was generated (the
flush interval). `GET /api/stats` reports the current averages.

## Schema

| Table | Key | Content |
|---|---|---|
| `runs` | `run_id` | First and last measurement time of every run |
| `devices` | `device_id` | Type, domain, site, worker; latest value, sequence and run |
| `measurements` | `id`, unique `event_id` | One row per measurement, with **three times**: `generated_at` (on the device), `received_at` (by the ingestion consumer), `stored_at` (by the database) |
| `alerts` | `alert_id` | One row per alert: rule, severity, threshold, state (`raised`/`cleared`), the value and measurement that raised and cleared it, and when |

The differences between the three times show where time goes: `received_at − generated_at`
is the MQTT path, `stored_at − received_at` the buffering and the API. Alerts have no
foreign key to measurements: an alert may be stored before the measurement that caused
it, since both travel independently.

## Operations

```bash
monitoring-mqtt stack up                    # also migrates the schema
monitoring-mqtt stack down                  # keeps the history (volume pgdata)
monitoring-mqtt stack down --volumes        # deletes it (asks first; --yes to skip)
monitoring-mqtt doctor                      # checks GET /ready on storage machines
docker compose -p monitoring-mqtt exec postgres psql -U monitoring   # SQL shell
```

Settings: `storage` in `config/simulation.yaml` (enabled, batch size, flush interval,
buffer limit); `POSTGRES_PASSWORD`, `API_BIND_ADDRESS` and `API_HOST_PORT` in `.env`.

### Changing the schema

Edit `src/monitoring_mqtt/storage/schema.py`, then add a migration in
`src/monitoring_mqtt/storage/migrations/versions/` (Alembic, configured in code: see
`storage/migrate.py`). `python -m monitoring_mqtt.storage.migrate sql` prints the SQL of
every migration without a database. An integration test compares the migrated database
with `schema.py` and fails if they differ.
