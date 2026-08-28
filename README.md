# ngen-weave

**Structure your AI automation with reproducible workflows and audit traces by default.**

You describe a workflow as plain Python classes and declare model bindings in YAML. Every run is recorded and resumable, every step leaves an audit trace, and waiting on a person is just another node type.

Extensibility is a first-class feature. Start with a worker, then add controls, human reviews, and composites as your process grows. Model selection stays in configuration.

Runs are exposed through a CLI, a FastAPI service, and MCP tools, so the same workflow works locally, over HTTP, or inside your IDE's agent.

## Quick start

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

The repo is a uv workspace: the root project plus `packages/ngen-weave-core`, `packages/ngen-weave-cli`, `packages/ngen-weave-server`, and `packages/ngen-weave-mcp`. Plain `uv sync` only installs the root project's dependencies (the CLI), which is why `uv run uvicorn ...` fails with `ModuleNotFoundError` out of the box. Sync every workspace member instead:

```console
$ uv sync --all-packages
```

(To sync just one package, use `uv sync --package ngen-weave-server`.)

Install the canonical example workflow (`examples/code_review`, which wires up a draft → gate → finalize / human-review path):

```console
$ uv pip install -e examples/code_review
```

Validate its config against discovered workflows:

```console
$ uv run ngen-weave validate examples/code_review/ngw.yaml
ok: CodeReview (examples/code_review/ngw.yaml)
```

Start a local model (or point the config at any LiteLLM-supported provider instead):

```console
$ llama-server -m <model.gguf> --port 8080
```

Run the workflow:

```console
$ uv run ngen-weave run code_review.workflows.CodeReview \
    -i examples/code_review/request.json \
    -c examples/code_review/ngw.yaml \
    --project demo
status completed
```

If a run pauses on a human node, submit a decision file (`{"verdict": "approve", "notes": "..."}`) and resume by run id. A `reject` verdict loops back to the drafter:

```console
$ uv run ngen-weave resume <run-id> -p response.json --project demo
status completed
$ uv run ngen-weave status <run-id>
```

### Serving and MCP

```console
$ uv run uvicorn "ngen_weave_server.app:create_app" --factory --port 8000
$ uv run ngen-weave-mcp --root examples/code_review        # stdio transport
$ uv run ngen-weave-mcp-http --root examples/code_review   # streamable HTTP at /mcp
```

Run uvicorn through `uv run` so it uses the project venv; a bare `uvicorn` resolves to whatever your shell PATH has. The `--factory` flag is required because `create_app()` returns the app rather than being the app itself.

MCP tools discover workflows from installed distributions plus a project manifest (`ngen-weave.json`) next to `--root`. For testing, the MCP servers accept `NGEN_WEAVE_FAKE_PROVIDER=1`, which replaces real model calls with canned replies.

Handy management verbs (add `--url` to target a running server; without it they operate on local state):

```console
$ uv run ngen-weave workflows              # registered workflows + descriptions
$ uv run ngen-weave runs [--workflow X] [--status Y]
$ uv run ngen-weave cancel <run-id>
$ uv run ngen-weave note <run-id> "shipping blocked on spec"
$ uv run ngen-weave export-run <run-id> [--out run.json]
```

## Build a workflow from small pieces

A workflow is a Python class with typed inputs and outputs. Each piece has one job:

- `Worker` calls a model and turns its response into typed output.
- `Control` chooses the next path using Python logic or a model.
- `Human` adds a review step and routes from the reviewer's verdict.
- A composite `Workflow` connects the pieces.

Design the workflow one piece at a time. A worker can feed a control, a control can branch into several paths, and a human verdict can send the flow back to an earlier worker. The same small vocabulary handles a straight-through job, a branching graph, or a review loop.

Model bindings and run settings live in YAML (`ngw.yaml`), so the Python class describes the workflow while configuration selects its model and runtime:

```yaml
workflow: code_review.workflows.CodeReview
params: {}
models:
  code_review.workflows.CodeReview: example-model
run:
  checkpointer: memory
```

Models resolve through variants in a `models.json` registry. Point a variant at `openai/gpt-4o-mini` and set `OPENAI_API_KEY` to use it instead of a local llama.cpp endpoint.

## Design notes

- `build()` runs once against a recording adapter, then recorded operations construct the LangGraph graph.
- Runs persist in SQLite at `.ngen-weave/runs.db`, resume from stored input, and use `AsyncSqliteSaver` for checkpoints.
- Declare `artifacts = ("field",)` to save worker output by SHA-256 under `.ngen-weave/projects/<project>/<sha256>`.
- Import-time checks validate topology and `build()` before a run starts, including checks for nondeterminism and hidden mutation.

### Repository layout

uv workspace with packages under `packages/`:

- `ngen-weave-core`: engine, workflow model, validation, artifacts, provenance
- `ngen-weave-cli`: the `ngen-weave` console script
- `ngen-weave-server`: FastAPI service (`create_app()` factory)
- `ngen-weave-mcp`: MCP tools over stdio and streamable HTTP

Core dependencies: `langgraph>=0.2`, `langgraph-checkpoint-sqlite>=2`, `litellm>=1.40`, `pydantic>=2.7`, `jsonschema>=4.21`, `pyyaml>=6.0.3`.

### Development

```console
$ scripts/check.sh    # deps → complexity → lint → format → test → build
```

Tests run with pytest (`asyncio_mode = auto`); tests marked `live` touch real provider SDKs and are excluded by default (`pytest -m live`). A conformance suite covers the RunService contract across every backend. Lint/format is ruff (line length 100).

## License

Apache-2.0. See [LICENSE](LICENSE).
