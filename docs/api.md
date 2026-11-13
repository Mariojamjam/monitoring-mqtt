# HTTP API

The API (FastAPI) is the single entry point to the stored history: the ingestion consumer
writes through it and everything else reads through it ([storage.md](storage.md)).

| | |
|---|---|
| Base URL | `http://127.0.0.1:8000` (`API_BIND_ADDRESS`, `API_HOST_PORT` in `.env`); the API routes are under **`/api`**, while `/health`, `/ready`, `/docs` and `/openapi.json` stay at the root |
| Interactive reference | **`/docs`**: a [Scalar](https://scalar.com) API reference, with request examples and a client to try every route |
| OpenAPI document | `/openapi.json`; a copy is versioned in [`openapi.json`](openapi.json) |
| Format | JSON; times in ISO 8601 with a time zone (UTC) |

The versioned `docs/openapi.json` is generated from the code and a unit test fails when it
is out of date:

```bash
uv run python -m monitoring_mqtt.api.docs > docs/openapi.json
```

## Routes

| Route | Purpose |
|---|---|
| `GET /health` | The process is running (never touches the database; used by the healthcheck) |
| `GET /ready` | The API can reach the database: 200, or 503 |
| `POST /api/ingest/measurements` | Store a batch of measurements (ingestion consumer) |
| `POST /api/ingest/alerts` | Store a batch of alert events (ingestion consumer) |
| `GET /api/devices` | Devices with their latest value and open alert count; filters `domain`, `device_type`, `run_id` |
| `GET /api/devices/{device_id}` | One device, how many measurements are stored, its open alerts |
| `GET /api/measurements` | Measurements, newest first; filters `device_id`, `domain`, `metric`, `run_id`, `from`, `to` |
| `GET /api/measurements/series` | Average, minimum, maximum and count per time bucket for one device (`bucket=10s`, `1m`, `1h`…) |
| `GET /api/alerts` | Alerts, newest first; filters `state` (`active`, `cleared`), `severity`, `domain`, `device_id`, `rule_id`, `run_id`, `from`, `to` |
| `GET /api/alerts/{alert_id}` | One alert, with its duration once cleared |
| `GET /api/runs`, `GET /api/runs/{run_id}` | Runs (one per `stack up`) with measurement, device and alert counts, per domain |
| `GET /api/stats` | Totals, open alerts, and the average delay to the database |

## Examples

```bash
API=http://127.0.0.1:8000
curl -s $API/api/stats
curl -s "$API/api/devices?domain=poultry"
curl -s "$API/api/measurements?device_id=poultry-3f9a1c-001&from=2026-11-13T12:00:00Z&limit=50"
curl -s "$API/api/measurements/series?device_id=poultry-3f9a1c-001&bucket=1m"
curl -s "$API/api/alerts?state=active"
curl -s "$API/api/runs/run-20261113T120000Z"
```

## Pagination

Lists return `{"items": [...], "next_cursor": "..."}`. To get the next page, send the same
query with `cursor=<next_cursor>`; `next_cursor` is `null` on the last page. `limit` sets the
page size (1–1000, default 100).

The cursor names the last row of the page (keyset pagination). Pages stay fast on large
tables, and rows arriving meanwhile never shift a page: a page never repeats or skips a
row. An invalid cursor answers 400.

## Ingestion batches

```json
POST /api/ingest/measurements
{
  "source": "ingestion",
  "items": [
    {"received_at": "2026-11-13T12:00:01.002Z", "event": { "...": "a telemetry payload" }}
  ]
}
```

`event` is the MQTT payload as published (schema `Measurement` or `AlertEvent` in the
reference); `received_at` is when the ingestion consumer got it. Each item is validated on
its own:

```json
{"received": 3, "stored": 1, "duplicates": 1,
 "rejected": [{"index": 2, "reason": "event_id: Field required"}]}
```

- `stored`: new rows; `duplicates`: events already stored (idempotent by `event_id`, or by
  alert transition), so a retried batch is harmless;
- `rejected`: invalid items, with the reason. They never block the valid ones.

## Errors

| Status | When |
|---|---|
| 400 | Invalid `cursor` |
| 404 | Unknown device, alert or run |
| 413 | More than 1000 items in one batch |
| 422 | Invalid parameter or body (e.g. `limit=0`, `bucket=30` without a unit, `state=open`) |
| 503 | The database is unavailable; retry after `Retry-After` seconds |
