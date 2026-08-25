# CLI

The `ngen-weave` console script has four commands. All of them translate to
core calls; none carry business logic.

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

## Provider injection for tests

`ngen_weave_cli.context._build_engine(config, provider=None)` is the single
construction point for Engine and RunStore. Tests pass a fake provider there;
commands never build engines themselves.
