# ngen-weave

ngen-weave is durable human-in-the-loop AI workflows on LangGraph: define a
workflow in Python, configure it in YAML, run it locally, and resume any run
after a crash or a human pause.

## Install

Requires Python 3.12 or later and [uv](https://docs.astral.sh/uv/). From the
repository root:

```console
$ uv sync
```

This installs the `ngen-weave-core` and `ngen-weave-cli` workspace packages as
one distribution and puts the `ngen-weave` console script on the virtualenv's
path. Run commands through `uv run ngen-weave ...`, or activate `.venv/bin`
directly.

## Quickstart

The canonical example lives in `examples/code_review`: a worker drafts a code
review, a control gate checks it, a human approves or rejects, and the reviewed
diff lands as an artifact. All paths below are relative to the repository root.

### 1. Install the example workflow package

```console
$ uv pip install -e examples/code_review
```

The package registers its workflow module under the `ngen-weave.workflows`
entry-point group, which is how discovery finds it.

All remaining commands run from the repository root.

### 2. Validate the config

```console
$ uv run ngen-weave validate examples/code_review/ngw.yaml
ok: CodeReview (ngw.yaml)
```

Validation loads the YAML against the discovered workflows and checks model
bindings, schemas, and graph structure at import time.

### 3. Run the workflow

```console
$ uv run ngen-weave run code_review.workflows.CodeReview \
    -i examples/code_review/request.json \
    -c examples/code_review/ngw.yaml \
    --project demo
```

The config binds the workflow's model calls to the `example-model` variant in
`examples/code_review/models.json`. By default that variant points at a local
llama.cpp server (`http://localhost:8080/v1`); start one with a model of your
choice:

```console
$ llama-server -m <model.gguf> --port 8080
```

To use a cloud provider instead, set the variant's `model` (for example
`openai/gpt-4o-mini`) and export the matching credentials (`OPENAI_API_KEY`).
The gate approves any non-empty review, so a normal run prints `status
completed` right away. The human review step only triggers when the gate
rejects an empty draft; if the process dies mid-run for any other reason, the
same `resume` command continues from the last checkpoint. Note the printed
run id for the next two steps.

### 4. Resume with the human decision

The human review node waits on a `HumanDecision` state: a `verdict` literal of
`approve` or `reject`, plus optional review notes. Put one in
`response.json`:

```json
{
    "verdict": "approve",
    "notes": "Looks good to me."
}
```

Then continue the run from its checkpoint:

```console
$ uv run ngen-weave resume <run-id> -p response.json --project demo
status completed
```

A `reject` verdict loops back to the drafter instead.

### 5. Inspect the run

```console
$ uv run ngen-weave status <run-id>
```

Prints the workflow class path, current status, the node path blocking on
human review when one is, and total cost summed from the run's provenance.

## Serving and MCP

Two additional workspace packages, `ngen-weave-server` and
`ngen-weave-mcp`, make runs reachable over HTTP and MCP. `uv sync` installs
them alongside the core package with all three console scripts.

### The FastAPI service

The server package wraps one in-process `LocalRunService` behind HTTP routes:
`POST /runs`, `POST /runs/{run_id}/resume`, `GET /runs/{run_id}`,
`POST /runs/{run_id}/cancel`, `GET /runs`, `POST /runs/{run_id}/notes`, and
`GET /runs/{run_id}/export`. Start it from your project directory:

```console
$ uvicorn "ngen_weave_server.app:create_app" --factory --port 8000
```

Optional keyword arguments such as `config_path`, `runs_db_path`, and
`models_file` tune where configuration and state come from; defaults match
the v0.1 settings anchored at the working directory.

### Workflows as MCP tools

Workflows become MCP tools over either transport:

```console
$ uv run ngen-weave-mcp --root examples/code_review      # stdio
$ uv run ngen-weave-mcp-http --root examples/code_review # streamable HTTP at /mcp
```

Both entry points discover workflows from installed distributions plus an
optional project manifest `ngen-weave.json` next to `--root`, whose default
is the working directory. A manifest lists importable workflow modules:

```json
{"modules": ["my_project.workflows"]}
```

Each tool call launches the named workflow and blocks until the run reaches
a terminal or parked state; paused and waiting runs return immediately with
the run id, which stays resumable through the runs API or the CLI. Pass
`--config ngw.yaml` to load a run config before serving, which applies
settings such as `run.budget` cost limits. `--timeout`, `--models`, and
`--db` override the tool-call timeout, the models file, and the checkpoint
database respectively. `NGEN_WEAVE_FAKE_PROVIDER=1` replaces real model
calls with canned replies; it exists for tests, as documented in each main's
`--help` epilog.

### Managing runs from the CLI

The management verbs speak to a `RunService`: without `--url` they use the
local in-process stack, and `--url` points them at the FastAPI service.

```console
$ uv run ngen-weave workflows                 # registered workflows and descriptions
$ uv run ngen-weave runs [--workflow X] [--status Y]
$ uv run ngen-weave cancel <run-id>
$ uv run ngen-weave note <run-id> "shipping blocked on spec"
$ uv run ngen-weave export-run <run-id> [--out run.json]
```

`workflows` prints one line per registered workflow with its person-facing
description. `runs` lists id, workflow, status, cost, start time, and a
waiting flag. `cancel` requests cancellation at the next activation
boundary and prints the resulting status. `note` attaches a free-text note.
`export-run` writes a run's canonical JSON document to stdout, or to a path
with `--out`; those bytes are also what the HTTP export route serves.

## Where things land on disk

Everything lives under `.ngen-weave/` in the working directory:

- `.ngen-weave/runs/<run-id>.json`: one JSON document per run holding metadata
  plus the event and provenance stream, written atomically at each transition.
- `.ngen-weave/runs/<run-id>/artifacts/`: artifacts written by a run.
- `.ngen-weave/projects/<project>/`: content-addressed artifact blobs named by
  their sha256, each with a `<sha256>.json` sidecar carrying the artifact
  metadata and the provenance link back to the producing activation.
