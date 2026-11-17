# Documentation

Start with the [project README](../README.md) (what it is, getting started), then
[commands.md](commands.md) for every command by task.

| Folder | Answers |
|---|---|
| [`guides/`](#guides-using-and-operating-it) | How do I set it up, run it, watch it, deploy it? |
| [`design/`](#design-how-it-works) | How does it work, and why this way? |
| [`evaluation/`](#evaluation-measuring-it) | How is it measured, what happens when something fails? |
| [`reference/`](#reference-generated) | Every CLI option and API route. Generated from the code, never edited by hand |

## Guides: using and operating it

- [Development guide](guides/development.md): environments (host, Dev Container),
  configuration, the Docker stack, tests and code quality.
- [Terminal UI](guides/tui.md): screens, keys, data sources and demonstration scenarios.
- [Dashboard](guides/dashboard.md): pages, live refresh and caching, why it only talks to
  the API.
- [Running on several machines](guides/distributed.md): roles, machine settings, tunnels,
  clocks.
- [Deploying on a Raspberry Pi](guides/deploy-raspberry-pi.md): step by step, with `.env`
  profiles for the gateway and the field machines.

## Design: how it works

- [System design](design/design.md): how every part communicates (MQTT, HTTP, SQL), with
  diagrams.
- [Architecture](design/architecture.md): components, data flow, failure behaviour and
  security boundaries.
- [MQTT contract](design/mqtt-contract.md): topics, payload schema, identities and QoS
  semantics.
- [Simulation](design/simulation.md): how values evolve, anomalies, instability, scheduled
  faults, live device commands and ground truth.
- [Storage](design/storage.md): ingestion, delivery guarantees hop by hop, schema, measured
  outages.
- [Alerts](design/alerts.md): rules, states, topics, restarts and evaluation against the
  ground truth.
- [HTTP API](design/api.md): routes, pagination, examples, ingestion batches, errors.

## Evaluation: measuring it

- [Experiments](evaluation/experiments.md): scenarios, how a run is measured, metric
  definitions, reports, experiments across machines.
- [Failures and recovery](evaluation/failures.md): how to provoke each failure, how it is
  detected and what happens to the data, with measured results.

## Reference (generated)

- [Command line reference](reference/cli.md): every command, argument and option.
- [API reference](reference/api/index.html): a stand-alone page (open it in a browser, no
  API needed) built from the [OpenAPI document](reference/api/openapi.json).

Regenerate them with the commands in [commands.md](commands.md#8-develop); unit tests fail
when they are out of date, and when a relative link in the docs points nowhere.
