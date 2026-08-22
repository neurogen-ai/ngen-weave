# agent-loom v0.1 PRD

Status: amended. v0.1 scope unchanged; sections reversed by later plans carry inline notes pointing at plans/v0.2/PRD.md and plans/v1.0/PRD.md.
Scope: v0.1 only. Forward-looking notes live in the outlook section at the end.

## Problem

Agent workflows today are either code (LangGraph, custom scripts) or loose markdown conventions with no contracts between steps. Code gives you control but every workflow is a program you have to read. Loose conventions give you files but nothing stops a step from emitting garbage that breaks the next one.

agent-loom is a declarative YAML framework for agent workflows with strict boundary schemas. Every node declares its input and output as JSON Schema. The engine validates at each boundary and refuses to run anything that doesn't check out. Abstractions stay small enough to learn in an afternoon; complexity comes from composing nodes, not from the framework growing features.

The style reference is Microsoft's conductor-style declarative orchestration: YAML-defined graphs, typed node contracts, orchestration separated from node logic, engine-level observability.

## Goals for v0.1

1. Four node types: worker, control, human, plus observers embedded in any of them.
2. Strict JSON Schema validation at every node boundary.
3. Cyclic graphs from day one, with a global step budget to guarantee termination.
4. A thin sequential TypeScript engine behind a narrow interface, so a different executor (Python/LangGraph, FastAPI service) can replace it later without touching workflows.
5. File-based state: inspectable checkpoints, resumable runs, human review through editable YAML artifacts that any UI can serve.

## Non-goals for v0.1

- Parallel execution. Runs are strictly sequential.
- Subworkflows / nesting. Modularity comes from file-referenced schemas, prompts, and observer presets. Nesting is designed for (see invariant below) but not implemented.
- Web UI. Human review is a file contract; a UI is a client of that contract, not part of the framework.
- SQL-style observer queries. A sandboxed expression language covers v0.1 needs.
- Inline schemas. External JSON Schema files only.
- Token streaming, multi-workflow scheduling/queues, secrets management beyond environment variables.

## Mental model

A workflow is a directory:

```
workflows/example/
├── graph.yaml          # the graph: nodes + edges + run config
├── schemas/            # JSON Schema (draft 2020-12) files
│   ├── input.json
│   └── output.json
└── *.md                # node definition files
```

Node files carry a YAML frontmatter header and optionally a markdown body. `graph.yaml` wires node definitions into a graph by file path. Data flows along edges: parent output becomes child input. Nothing executes in parallel; the engine walks one ready node at a time.

## Node types

### Worker nodes

Take validated input, call a model, return output validated against their output schema.

```markdown
---
name: worker-example        # overrides filename if present
type: worker
input: schemas/input.json   # paths resolve relative to this file
output: schemas/output.json

model: kimi-k3              # resolved via models.yaml registry
variant: high               # named preset, e.g. reasoning effort
temperature: 0.2

observers:
  - ref: ~/Projects/project-name/workflows/observers/max-tokens-observer.md
    max_tokens: 2048        # overrides preset value

  max_cost_observer:        # inline observer definition
    when: "cost_usd > max_cost"
    max_cost: 1.00
    action:
      type: reroute
      target: human_review  # name of an explicit node
---

You are a drafting assistant. Given the brief below, produce
a structured draft matching the output schema.

<brief>
{{ input.brief }}
</brief>
```

Prompt placement rules:

- The main prompt may live inline under a `prompt:` key or in the markdown body after the frontmatter. If both exist, the YAML `prompt:` key wins. If neither exists, validation fails.
- There is no separate `instruction:` field in v0.1. It duplicated what the prompt body already does.
- Input values are available to templates as `input.<field>`.

Worker output validation has no built-in retry. An invalid output is data, not an error: workers always emit something, and gating quality is the job of a control node downstream (optionally paired with an observer encoding "one retry then divert"). This keeps strictness policy in the workflow author's hands and out of the engine.

### Control nodes

Branch logic. Like a worker, except the output schema must contain a required boolean field named `pass`. On `true` the engine follows the edge's `pass` target, on `false` the `fail` target. Either target can be any node, which is how loops happen.

Control nodes are either programmatic (pure logic evaluated over the input, no model call) or model-generated (the model fills the output schema, including `pass`). Which one is declared on the node.

Edges from control nodes use compact syntax, because a control node has exactly two exits by definition:

```yaml
edges:
  - from: quality_gate
    pass: finalize
    fail: revision_worker
```

The long form (`from/on/to`) is deliberately rejected. It invites malformed graphs for no gain.

### Human nodes

Pause the run and wait for a person. A human node has normal input and output schemas. When it fires, the engine writes a review artifact and the run enters `waiting_human`:

```
.loom/runs/<run-id>/reviews/<node-name>.yaml
```

The artifact contains the input data for context, the output schema's fields rendered as editable entries, and a decision field. The decision field is a regular schema field; an enum type is recommended so routes stay finite. Required fields must be filled before the artifact counts as complete.

Resuming is file-based: any interface (CLI, editor, future web UI) fills in the fields and writes the completed artifact to `<node-name>.reviewed.yaml`. The engine picks it up and continues. Where the run goes next is a route table evaluated over the human-filled values, using the same expression evaluator as observers:

```yaml
type: human
routes:
  - when: "decision == 'approve'"
    to: finalize
  - when: "decision == 'reject'"
    to: revision_worker
  - to: escalation      # optional default route
```

The framework defines the artifact contract only. How a UI presents or fills it is the UI's business.

> Note from v0.2: remote runs need the same artifact as JSON, submitted through langgraph-server's interrupt/resume endpoints. Both forms carry identical payloads.

### Observers (meta-nodes)

Observers are never standalone. They are defined inside worker, control, and human nodes, either referenced from a preset file (`ref:`) or written inline. Every activation of the host node exposes a flat metadata object to its observers:

| field | meaning |
|---|---|
| `iterations` | times this node has activated during this run |
| `tokens_in_context` | tokens in the current context window |
| `tokens_total` | tokens spent by this node across the whole run |
| `cost_usd` | credits/cost spent, total for this node this run |
| `elapsed_ms` | wall time of current activation |
| `last_output_valid` | whether the previous output passed schema validation |

Expression language: plain identifiers evaluated against that metadata object, plus parameters pre-bound from the observer's own keys (`max_cost` above). Evaluated in a sandboxed JS expression evaluator. No template interpolation inside expressions; identifiers refer to real bindings. No SQL in v0.1; revisit if a query UI ever justifies it.

Actions:

- `pause` — suspend the run (same mechanics as human waiting)
- `stop` — end the run cleanly
- `reroute` — discard the current attempt, mark the node skipped, jump to the named target node

Preset composition works like the example above: `ref:` loads a preset, keys in the referencing block override preset values.

## Graph configuration

`graph.yaml` names the nodes and wires the edges:

```yaml
name: example-workflow
input: schemas/input.json     # workflow-level contract
output: schemas/output.json

retries:                      # infra failures only
  attempts: 3
  backoff_ms: 1000

max_steps: 100                # mandatory termination budget

nodes:
  draft:
    def: worker-example.md
  quality_gate:
    def: control-quality.md
  human_review:
    def: human-review.md

edges:
  - from: draft
    to: quality_gate
  - from: quality_gate
    pass: finalize
    fail: human_review
```

Rules:

- Node names come from the `name:` field in the definition file, falling back to the filename. Duplicate names across the graph are a validation error.
- Workflow-level `input`/`output` schemas are validated against the entry nodes' inputs and exit candidates' outputs. This is the invariant that makes a workflow shaped exactly like a worker node, which is the hook for first-class subworkflows later. Not implemented in v0.1, only validated and documented.

## Fan-out and fan-in

A node may have multiple children and multiple parents.

- Fan-out: every child receives the parent's output. Since execution is sequential, children run in declaration order.
- Fan-in: inputs are assembled explicitly on the child node. A node-declared map says which parent feeds which input field:

```yaml
nodes:
  synthesizer:
    def: synthesize.md
    inputs:
      draft: drafter_a
      critique: reviewer
```

- Single parent with no map: the whole parent output passes through as the child's input.
- Multiple parents: a total map of all required input fields is mandatory. A missing mapping for a required field is a static validation error, caught by `loom validate` before anything runs.

Mapping targets validate against the child's input schema, so miswired fields fail at load time.

## Execution semantics

Sequential single-threaded walk. One node ready, run it, write checkpoint, pick next.

Run state lives at `.loom/runs/<run-id>/state.json`, written after every node transition. Contents: run id, workflow reference plus content hash, per-node status (`pending`, `blocked`, `waiting_human`, `done`, `failed`, `skipped`), all node outputs, observer event log, step counter.

Error taxonomy, two classes with different treatment:

1. **Data failures.** Schema-invalid output, failed control logic, unmet human requirements. These are values that flow through the graph; control nodes and observers route on them. The engine does not retry them.
2. **Infra failures.** API timeouts, HTTP errors, malformed workflow configs. Transport errors retry per the graph-level retry policy: default 3 attempts with exponential backoff starting at 1000ms. Exhausted retries, or any config error, kill the run with an error report.

Checkpointing is the resume mechanism. `loom resume <run-id>` reloads state.json and continues from wherever the run stopped, whether that was a crash, an observer pause, or a pending human review.

## Model layer

> Superseded by v0.2, which drops pi and Vercel AI SDK in favor of LiteLLM via LangChain's `init_chat_model`, with a project-root `models.json` (shared across workflows) replacing this per-workflow registry. The variant-as-data principle below survives unchanged.

Providers are abstracted behind pi's provider layer if it imports cleanly; Vercel AI SDK is the fallback. Both normalize token counts and costs, which the observer metadata depends on.

Model references resolve through a `models.yaml` registry:

```yaml
kimi-k3:
  provider: openrouter
  id: moonshotai/kimi-k3
  variants:
    high: { reasoningEffort: high }
    low: { reasoningEffort: low }
```

Variants are data, not code branches. `variant: high` on a node looks up the named entry.

## Expression evaluator (shared)

One evaluator serves both observer triggers and human routes:

- Sandboxed JS expression evaluation, no I/O, no imports.
- Identifiers bind to a flat object: metadata fields for observers, artifact fields for human routes.
- Parameters from surrounding YAML bind alongside (`max_cost`).
- All expressions compile at `loom validate` time. A non-compiling expression is a static error.

Frozen metadata surface for v0.1: the six fields listed in the observer table. Nothing else exists; additions go through a version bump.

## CLI

Single TypeScript package, no monorepo yet. Layout: `src/schema` (Zod mirrors of every structure in this document), `src/engine`, `src/cli`.

```
loom validate <workflow-dir>   # full static check
loom run <workflow-dir> [-i input.json]
loom resume <run-id>
loom status <run-id>
```

`loom validate` catches everything cheap: schemas resolve and parse, names unique, edges reference existing nodes, fan-in maps complete, expressions compile, control output schemas contain the required `pass` boolean, workflow-level schemas agree with entry/exit nodes.

> Note from v0.2: graph.yaml also requires a top-level `description:` field (agents pick loom tools by description once workflows are exposed over MCP), and the TS engine here is demoted to validation plus a non-authoritative smoke runner once the LangGraph backend lands.

## Canonical examples

These live here until there is an implementation to test against.

**Worker** — see the worker-example.md listing in the worker section above.

**Control node** (programmatic):

```markdown
---
name: quality_gate
type: control
mode: programmatic            # pure logic, no model call
input: schemas/review-input.json
output: schemas/review-output.json   # must require boolean `pass`
---

checks:
  - when: "input.word_count < min_words"
    pass: false
min_words: 200
```

**Control node** (model-generated): same shape, `mode: model`, plus model/variant/prompt keys identical to a worker. The model fills the schema including `pass`.

**Human node**: see the routes snippet in the human section, combined with standard `input`/`output` schema references and a decision enum field.

**Observer preset** (`workflows/observers/max-tokens-observer.md`):

```markdown
---
when: "tokens_in_context > max_tokens"
max_tokens: 4096
action:
  type: pause
---
```

## Outlook

> Superseded. See plans/v0.2/PRD.md (LangGraph as canonical executor, MCP exposure of workflows as tools, artifacts block) and plans/v1.0/PRD.md (distribution constraints, Argo path). The original list is kept for the record.

In priority order:

1. Subworkflows as first-class nodes, riding the workflow-contract invariant from day one. A node whose def resolves to a graph.yaml executes as a nested run sharing the checkpoint tree.
2. Alternative executor behind the same `ExecutionEngine` interface: FastAPI Python service, optionally LangGraph-backed, for checkpoint portability and ecosystem reuse.
3. Parallel fan-out once the engine interface proves itself sequentially.
4. SQL observer queries if a run-inspection UI materializes.
5. Web UI serving the human review artifact contract.
