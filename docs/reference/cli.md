# Command line reference

Generated from the CLI by `uv run python -m monitoring_mqtt.cli.reference`: do not edit by hand. For commands grouped by task, see [commands.md](../commands.md).

Operate the Monitoring MQTT publish/subscribe lab.

**Usage**:

```console
$ monitoring-mqtt [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--project-dir <directory>`: Project checkout to operate on (overrides MONITORING_MQTT_PROJECT_DIR).
* `--help`: Show this message and exit.

**Commands**:

* `tui`: Open the terminal UI: who publishes, the...
* `dashboard`: Open the dashboard (history, charts,...
* `version`: Show the installed version.
* `doctor`: Check environment, Python, uv, Docker, the...
* `stack`: Manage the Docker Compose stack.
* `test`: Run automated tests.
* `device`: Send live commands to one simulated device...
* `experiment`: Run versioned experiments and report their...

## `monitoring-mqtt tui`

Open the terminal UI: who publishes, the broker, who consumes (read-only).

**Usage**:

```console
$ monitoring-mqtt tui [OPTIONS]
```

**Options**:

* `-c, --config <file>`: Experiment file to apply (default: config/simulation.yaml).
* `--help`: Show this message and exit.

## `monitoring-mqtt dashboard`

Open the dashboard (history, charts, alerts, runs) in the browser.

**Usage**:

```console
$ monitoring-mqtt dashboard [OPTIONS]
```

**Options**:

* `--open / --no-open`: Open it in the default browser.  [default: open]
* `--help`: Show this message and exit.

## `monitoring-mqtt version`

Show the installed version.

**Usage**:

```console
$ monitoring-mqtt version [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.

## `monitoring-mqtt doctor`

Check environment, Python, uv, Docker, the project dir, roles and the broker.

**Usage**:

```console
$ monitoring-mqtt doctor [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.

## `monitoring-mqtt stack`

Manage the Docker Compose stack.

**Usage**:

```console
$ monitoring-mqtt stack [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `up`: Start this machine&#x27;s part of the...
* `workers`: List the device workers registered in the...
* `down`: Stop and remove the stack containers and...
* `restart`: Restart running containers as they are...
* `status`: Show the status of the stack services.
* `logs`: Show service logs (Ctrl+C stops following;...

### `monitoring-mqtt stack up`

Start this machine&#x27;s part of the experiment (a new run each time).

The experiment comes from config/simulation.yaml (or --config); which services run
here and where the broker is come from the machine settings in .env.

**Usage**:

```console
$ monitoring-mqtt stack up [OPTIONS]
```

**Options**:

* `--build`: Build images before starting.
* `-c, --config <file>`: Experiment file to apply (default: config/simulation.yaml).
* `--run-id <str>`: Run id for this start (overrides run.run_id).
* `--help`: Show this message and exit.

### `monitoring-mqtt stack workers`

List the device workers registered in the broker, from every machine.

**Usage**:

```console
$ monitoring-mqtt stack workers [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.

### `monitoring-mqtt stack down`

Stop and remove the stack containers and network (the history is kept).

**Usage**:

```console
$ monitoring-mqtt stack down [OPTIONS]
```

**Options**:

* `--volumes`: Also delete the stored history (database).
* `-y, --yes`: Do not ask for confirmation.
* `--help`: Show this message and exit.

### `monitoring-mqtt stack restart`

Restart running containers as they are (same run id and settings).

Use it to provoke a restart, e.g. &#x27;stack restart broker&#x27;. To apply a changed
config/simulation.yaml or start a new run, use &#x27;stack up&#x27;.

**Usage**:

```console
$ monitoring-mqtt stack restart [OPTIONS] [services]...
```

**Arguments**:

* `services...`: Services to restart (default: all).

**Options**:

* `-t, --timeout <int range>`: Seconds to wait for a clean stop.  [x&gt;=0]
* `--help`: Show this message and exit.

### `monitoring-mqtt stack status`

Show the status of the stack services.

**Usage**:

```console
$ monitoring-mqtt stack status [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.

### `monitoring-mqtt stack logs`

Show service logs (Ctrl+C stops following; the stack keeps running).

**Usage**:

```console
$ monitoring-mqtt stack logs [OPTIONS] [service]
```

**Arguments**:

* `service`: Service name (default: all).

**Options**:

* `-f, --follow / --no-follow`: Keep streaming new lines.  [default: follow]
* `-n, --tail <int range>`: Lines to show from the end of each log.  [default: 100; x&gt;=0]
* `--help`: Show this message and exit.

## `monitoring-mqtt test`

Run automated tests.

**Usage**:

```console
$ monitoring-mqtt test [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `unit`: Run the unit tests (extra arguments go to...
* `integration`: Run the integration tests against an...
* `smoke`: Check the broker this machine uses end to...

### `monitoring-mqtt test unit`

Run the unit tests (extra arguments go to pytest, e.g. -k topics).

**Usage**:

```console
$ monitoring-mqtt test unit [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.

### `monitoring-mqtt test integration`

Run the integration tests against an isolated stack (needs Docker; about 2 min).

The tests build the images and start their own stack, Compose project
monitoring-mqtt-it, on a free local port; it is removed at the end. Your stack keeps
running untouched. Extra arguments go to pytest, e.g. -k recovery.

**Usage**:

```console
$ monitoring-mqtt test integration [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.

### `monitoring-mqtt test smoke`

Check the broker this machine uses end to end, from a container.

Live telemetry is required only when this machine runs devices.

**Usage**:

```console
$ monitoring-mqtt test smoke [OPTIONS]
```

**Options**:

* `-c, --config <file>`: Experiment file to apply (default: config/simulation.yaml).
* `--help`: Show this message and exit.

## `monitoring-mqtt device`

Send live commands to one simulated device (its neighbours keep publishing).

**Usage**:

```console
$ monitoring-mqtt device [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `stop`: Switch the device off: it disconnects and...
* `pause`: Keep the device connected but silent.
* `delay`: Hold the device&#x27;s measurements back and...
* `disconnect`: Disconnect the device; its measurements...
* `anomaly`: Drive the device&#x27;s values out of the...
* `resume`: End every active episode of the device...

### `monitoring-mqtt device stop`

Switch the device off: it disconnects and generates nothing.

**Usage**:

```console
$ monitoring-mqtt device stop [OPTIONS] {device_id}
```

**Arguments**:

* `device_id`: Device id, e.g. poultry-3f9a1c-002 (&#x27;stack workers&#x27;, TUI).  [required]

**Options**:

* `--for <str>`: How long, e.g. 30s or 2m (default: until &#x27;device resume&#x27;).
* `--help`: Show this message and exit.

### `monitoring-mqtt device pause`

Keep the device connected but silent.

**Usage**:

```console
$ monitoring-mqtt device pause [OPTIONS] {device_id}
```

**Arguments**:

* `device_id`: Device id, e.g. poultry-3f9a1c-002 (&#x27;stack workers&#x27;, TUI).  [required]

**Options**:

* `--for <str>`: How long, e.g. 30s or 2m (default: until &#x27;device resume&#x27;).
* `--help`: Show this message and exit.

### `monitoring-mqtt device delay`

Hold the device&#x27;s measurements back and send them together at the end.

**Usage**:

```console
$ monitoring-mqtt device delay [OPTIONS] {device_id}
```

**Arguments**:

* `device_id`: Device id, e.g. poultry-3f9a1c-002 (&#x27;stack workers&#x27;, TUI).  [required]

**Options**:

* `--for <str>`: How long, e.g. 30s or 2m (default: until &#x27;device resume&#x27;).
* `--help`: Show this message and exit.

### `monitoring-mqtt device disconnect`

Disconnect the device; its measurements are generated and lost.

**Usage**:

```console
$ monitoring-mqtt device disconnect [OPTIONS] {device_id}
```

**Arguments**:

* `device_id`: Device id, e.g. poultry-3f9a1c-002 (&#x27;stack workers&#x27;, TUI).  [required]

**Options**:

* `--for <str>`: How long, e.g. 30s or 2m (default: until &#x27;device resume&#x27;).
* `--help`: Show this message and exit.

### `monitoring-mqtt device anomaly`

Drive the device&#x27;s values out of the operating range.

**Usage**:

```console
$ monitoring-mqtt device anomaly [OPTIONS] {device_id}
```

**Arguments**:

* `device_id`: Device id, e.g. poultry-3f9a1c-002 (&#x27;stack workers&#x27;, TUI).  [required]

**Options**:

* `--for <str>`: How long, e.g. 30s or 2m (default: until &#x27;device resume&#x27;).
* `--direction <str>`: high or low (if the device type has it).  [default: high]
* `--help`: Show this message and exit.

### `monitoring-mqtt device resume`

End every active episode of the device (manual, scheduled or random).

**Usage**:

```console
$ monitoring-mqtt device resume [OPTIONS] {device_id}
```

**Arguments**:

* `device_id`: Device id, e.g. poultry-3f9a1c-002 (&#x27;stack workers&#x27;, TUI).  [required]

**Options**:

* `--help`: Show this message and exit.

## `monitoring-mqtt experiment`

Run versioned experiments and report their results.

**Usage**:

```console
$ monitoring-mqtt experiment [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `list`: List the scenarios in experiments/.
* `run`: Run a scenario: one stack per sweep point...
* `report`: Aggregate a campaign: summary.csv, charts...

### `monitoring-mqtt experiment list`

List the scenarios in experiments/.

**Usage**:

```console
$ monitoring-mqtt experiment list [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.

### `monitoring-mqtt experiment run`

Run a scenario: one stack per sweep point and repetition, measured by the probe.

**Usage**:

```console
$ monitoring-mqtt experiment run [OPTIONS] {SCENARIO}
```

**Arguments**:

* `SCENARIO`: Name in experiments/ (e.g. qos) or a scenario file.  [required]

**Options**:

* `-r, --repetitions <int range>`: Override the scenario&#x27;s repetitions.  [x&gt;=1]
* `--duration <str>`: Override the measured window, e.g. 30s.
* `--warmup <str>`: Override the warm-up, e.g. 10s.
* `--resume <directory>`: Continue an interrupted campaign (its results folder).
* `--dry-run`: Validate and list the runs without starting anything.
* `--help`: Show this message and exit.

### `monitoring-mqtt experiment report`

Aggregate a campaign: summary.csv, charts and report.md in its folder.

**Usage**:

```console
$ monitoring-mqtt experiment report [OPTIONS] {campaign}
```

**Arguments**:

* `campaign`: A results folder (results/&lt;scenario&gt;/&lt;id&gt;).  [required]

**Options**:

* `--help`: Show this message and exit.
