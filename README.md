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
entry-point group, which is how discovery finds it. The example directory also
ships a project manifest, `examples/code_review/ngen-weave.json`, listing the
same module. Discovery fails loudly on duplicate class paths across sources,
so once the package is installed, remove or rename that manifest before
running from inside `examples/code_review`:

```console
$ mv examples/code_review/ngen-weave.json examples/code_review/ngen-weave.json.disabled
```

The remaining steps run from `examples/code_review`, where `ngw.yaml`,
`models.json`, and `request.json` live.

### 2. Validate the config

```console
$ uv run ngen-weave validate ngw.yaml
ok: CodeReview (ngw.yaml)
```

Validation loads the YAML against the discovered workflows and checks model
bindings, schemas, and graph structure at import time.

### 3. Run the workflow

```console
$ uv run ngen-weave run code_review.workflows.CodeReview -i request.json -c ngw.yaml --project demo
```

`request.json` carries the diff under review:

```json
{
    "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,3 +1,4 @@\n def greet(name):\n-    return \"hello\"\n+    return f\"hello, {name}\"\n"
}
```

The config binds the workflow's model calls to the `example-model` variant in
`models.json`, an `openai/gpt-4o-mini` endpoint, so provider credentials (for
example `OPENAI_API_KEY`) must be set in the environment. The gate approves
any non-empty review, so a normal run prints `status completed` right away.
The human review step only triggers when the gate rejects an empty draft; if
the process dies mid-run for any other reason, the same `resume` command
continues from the last checkpoint. Note the printed run id for the next two
steps.

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

## Where things land on disk

Everything lives under `.ngen-weave/` in the working directory:

- `.ngen-weave/runs/<run-id>.json`: one JSON document per run holding metadata
  plus the event and provenance stream, written atomically at each transition.
- `.ngen-weave/runs/<run-id>/artifacts/`: artifacts written by a run.
- `.ngen-weave/projects/<project>/`: content-addressed artifact blobs named by
  their sha256, each with a `<sha256>.json` sidecar carrying the artifact
  metadata and the provenance link back to the producing activation.
