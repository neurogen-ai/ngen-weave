# CLI

The `ngen-weave` console script carries nine commands: `validate`, `run`,
`resume`, `status`, `workflows`, `runs`, `cancel`, `note`, and
`export-run`. All of them translate to core calls; none carry business
logic.

## Discovery

Every command resolves workflows through one merged map built from:

1. the `ngen-weave.workflows` entry-point group of installed distributions,
2. an optional project manifest `ngen-weave.json` in the working directory:

```json
{"modules": ["my_project.workflows"]}
```

The map is built once per process. Duplicate class paths across sources fail
loudly, naming both sources.

## Commands

### validate

```
ngen-weave validate <config.yaml | module.path>
```

A YAML/JSON config is loaded against the discovery map (unknown keys,
unknown workflow paths, unresolvable model bindings are all errors) and its
workflow's JSON Schema is generated. A module path is imported through
discovery, so import-time structural validation runs. Exit 0 clean, 1 with
the message on stderr otherwise.

### run

```
ngen-weave run <class-path> [-i input.json] [-c config.yaml] [--project <name>]
```

`-c` wins when both the positional name and a config are given. Without a
config, the workflow resolves through the discovery map and engine settings
default (SQLite checkpointer at `.ngen-weave/checkpoints.db`, 3 retries).
Input must be a JSON object validating against the workflow's `input_type`.
The command prints the run id and final status; exit 1 unless completed.

Runs land under `.ngen-weave/runs/<run-id>.json`, written atomically at each
transition. Model calls go through LiteLLM using the `models.json` named by
the config's `models_file` (default `./models.json`).

### resume

```
ngen-weave resume <run-id> [-p response.json]
```

Continues a run from its checkpoint. `-p` supplies a human-review response as
JSON; without it the run continues from where it stopped. Exit 0 only on a
terminal status (`completed` or `failed`). Human submission is wired in a
later step; on this branch resuming a waiting run reports that instead.

Resume reads no config file. It uses default engine settings, so a run
started with a custom `db_path` should be resumed from the same working
directory with the same layout.

### status

```
ngen-weave status <run-id>
```

Prints the workflow class path, current status, the node path blocking on
human review when one is (`waiting-on`), and total cost summed from the run
file's `model_call` provenance records.

### export-run

```
gen-weave export-run <run-id> [--out PATH]
```

Emits the run as canonical JSON: the v0.1 key set plus the two defaulted
keys `started_at` and `notes`. One serializer (`dump_run_json`) produces
these bytes for the CLI, the HTTP export route, and every future consumer,
so identical runs serialize identically. Without `--out` the document goes
to stdout.

### workflows

```
gen-weave workflows
```

Prints one line per registered workflow: its fully-qualified class path plus
its `human_description`. This is the person-facing inventory; MCP exposure
uses the workflow's separate tool `description` instead.

### runs

```
gen-weave runs [--workflow PATH] [--status STATUS] [--url URL]
```

Lists runs with id, workflow class path, status, accumulated cost in USD,
start time, and a waiting flag. Filters select by workflow and status. The
command talks to a `RunService`: without `--url` it wires the local stack
in-process from the usual config resolution, and `--url` points it at a
remote ngen-weave server through `HttpRunService`.

### cancel

```
gen-weave cancel <run-id> [--url URL]
```

Requests cancellation at the next activation boundary. A running node
finishes; the next boundary stops the run, which ends as `cancelled`.
Cancelling an already-terminal run is a no-op. The command prints the
resulting status after the cancel request.

### note

```
gen-weave note <run-id> <text> [--url URL]
```

Attaches a free-text note to a run's annotations through the service only;
the note lands alongside the run record and shows up in exported JSON.

## MCP entry points

The `ngen-weave-mcp` and `ngen-weave-mcp-http` console scripts from the
`ngen-weave-mcp` package expose the same discovered workflows as MCP tools
over stdio and streamable HTTP at `/mcp`. Both accept `--root` (project root
holding the manifest), `--config PATH` (a YAML/JSON run config loaded before
serving, which applies settings such as `run.budget` limits), `--timeout`,
`--models`, and `--db`.

## Provider injection for tests

`ngen_weave_cli.context._build_engine(config, provider=None)` is the single
construction point for Engine and RunStore. Tests pass a fake provider there;
commands never build engines themselves.
