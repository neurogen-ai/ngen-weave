# ngen-weave product PRD

Status: authoritative. This document defines the whole product from v0.1 through 1.2 and beyond. Release requirement docs under `releases/` describe what each version must deliver; where they conflict with this one, this one wins. Technical design lives under `design/`, per-feature implementation plans under `implementation/`.

## Problem

Agent workflows today come in two bad flavors: raw code (LangGraph graphs, custom scripts) or loose markdown conventions with no contracts between steps. Code gives control but every workflow is a program someone has to read. Loose conventions give files but nothing stops a step from emitting garbage that breaks the next one. Neither gives you provenance, cost visibility, or a place for a human to stand in the loop.

There is also a gap above them. Tools like Conductor, Temporal, and Airflow own queuing, scheduling, sharding, and failure domains, and they are good at it. None of them offer typed node contracts, human review as a structured artifact, cost policies on every node, or workflows that nest like components. That combination is what ngen-weave builds.

## Thesis

A minimalist graph-based workflow and artifact management environment, extended into products through plugins.

The core is a small set of primitives: nodes with strict input/output validation, boolean control routing, human review artifacts, recursive composition (every workflow is itself a node), and provenance records written by default. Supervision over those records (observers) arrives in v0.2, built into the scheduler rather than as a separate graph-level facility. Everything else, including the UI, is replaceable or pluggable around those primitives.

The intended use is vertical products built on top: a coding IDE with code-graph and review workflows first, a research IDE with literature search after. ngen-weave stays workflow-type agnostic; the verticals prove the agnosticism rather than define it.

## Users

1. Workflow authors who write Python and want structure, validation, and observability without giving up code.
2. People who configure, launch, review, and supervise runs through the UI and never read the source.
3. Plugin authors who add node types, services, and workflow packs.
4. Future consumers of provenance exports: collaborators, reviewers, reproducibility checks in open science.

## Architecture

```
┌──────────────┐    HTTP API (semver from 1.0)     ┌──────────────────┐
│   ngen-weave-web   │ ◄───────────────────────────────► │   ngen-weave server     │
│ separate     │         also speaks MCP           │ FastAPI-owned,    │
│ repo, generic│                                   │ + MCP server      │
└──────────────┘                                   └────────┬─────────┘
                                                    │ RunService protocol
 external agent hosts (pi, opencode, Codex) ── MCP  ┌────────▼─────────┐
                                                    │    ngen-weave-core      │
                                                    │ Workflow classes, │
                                                    │ LangGraph engine  │
                                                    └────────┬─────────┘
                                     .ngen-weave/projects files + Postgres (v0.6+)
```

Two repos. The backend monorepo publishes `ngen-weave` as one pip distribution (workspace packages `ngen-weave-core`, `ngen-weave-cli`, later server and MCP packages inside it). The frontend `ngen-weave-web` is a separate published app, generic by construction: it knows the HTTP API contract, not ngen-weave's internals. The web API holds no business logic; it translates HTTP to core calls and nothing else. The API contract is unstable until 1.0 and semver'd from 1.0 onward.

## Core model

The `Workflow` class is the only abstraction that matters. Everything else specializes it.

```python
class CodeReview(Workflow):
    input_type = ReviewInput  # pydantic model
    output_type = ReviewOutput

    def build(self, g):
        g.add_node(self.draft)
        g.add_node(self.gate)  # composites declare structure
```

Rules:

- Inputs and outputs are pydantic models validated strictly at every boundary. The same classes generate the JSON Schema that config files, the UI, and MCP tool registration consume.
- Composite workflows wire their structure in a `build(g)` method over a narrow `GraphBuilder` protocol that accepts workflow classes (identity is the fully-qualified class path — `module.__qualname__` — resolved by the builder itself) and delegates to an ordinary LangGraph StateGraph underneath — plain `add_node`, `add_edge`, `add_conditional_edges`. Node kind is never stored as data: a workflow wiring no children is a leaf and overrides `run()`; one that does is a composite whose `run()` delegates to engine-managed subgraph runs, checkpointed and interruptible at any depth. Validation happens by an import-time dry-run compile: `build()` runs once against a throwaway real StateGraph behind the builder protocol, so structural problems raise from real compilation before any run, and a static lint over `build()`'s source rejects environment-dependent or stateful wiring; nodes never execute during validation. There is no second implementation of the builder and no recorded op sequence; the editor reads compiled topology from that same dry-run pass rather than source attributes. Only leaves override `run()` with arbitrary pure logic (pure function of definition, input, context).
- Human nodes carry an internal state model between input and output (`state_type`, a pydantic model), editable and prefilled from the incoming context. Submission validates `state_type`; conditional edges out of a human are declared in `build()`, their routers reading an enum/literal verdict field of the submitted state (enums generalize control's pass/fail to `"approve"` / `"reject"` / anything). Routing is therefore decided by what the human submitted, without a downstream control node. State and output are deliberately distinct models: what the human edits is not necessarily what travels downstream. By default the validated state is passed through as the output; a subclass may override with a programmatic transformation from (context, state) to output.
- Workflows carry two descriptions for two audiences: `description` is machine-facing and becomes the MCP tool description; `human_description` is person-facing, shown by the CLI workflow list and in the UI.
- Worker prompts are a `prompt` class attribute: a template string rendered against validated input fields, overridable by a method when generation needs logic.
- Human nodes have one edge input, surfaced as read-only `context` in their review artifact; the human's contribution is the artifact's `response` section, which exists only once a person submits and never travels over an edge. Response slots are generated from the state model's leaf primitive fields (`str`/`int`/`bool`/enum/literal; nested models are not supported until something needs them), fields with defaults may be left empty, required-without-default blocks completion, and completion validation is `state_type.model_validate(response)`; output validation runs afterwards on whatever the transformation produces. A per-node `prefill` map seeds slots from the context via path strings into the edge input (callables deferred until a real workflow needs derived values); prefill fills but never completes an artifact, since human submission is what resumes the run.
- Every activation emits provenance records (`run_id`, `node_path`, kind, payload) without opt-in, including a six-field metadata object per scope (iterations, tokens in context, tokens total, cost, elapsed time, last output validity). Supervision reads these records; it introduces none of its own. There are no author-declared Observer objects and no `observe()` override in v0.1; both arrive in v0.2 once the scheduler exists to host them.
- Human nodes interrupt the run and write review artifacts. Resuming means filling the artifact, locally as YAML or remotely as JSON, both carrying identical payloads.
- Node types ship as Worker, Control, Human, and Agent. AgentNode arrives in v0.2 together with the harness that exercises it, not earlier as an untested seam; real boxed autonomy lands in v0.5, enforced engine-side through PermissionSets (allow/deny lists, budget caps, forced return-to-review points), not by prompting.
- Registration: workflows are ordinary `Workflow` subclasses discovered by explicit listing, never by scanning. Distributions declare workflow modules under a `ngen-weave.workflows` entry-point group; projects list modules in `ngen-weave.json`; data-only definition files live in `.ngen-weave/definitions/`. Importing a listed module auto-registers every `Workflow` subclass found there, keyed by its fully-qualified class path (`module.__qualname__`, e.g. `examples.code_review.workflows.CodeReview`); duplicate paths fail loudly. There is no author-chosen name attribute: plugins may ship same-named classes without collision because the module path disambiguates. UIs tidy the class path into display labels; presentation never round-trips back into identity. Plugins (v0.5) register through ordinary package entry points and may contribute any combination of node types, services, workflow packs, namespaced HTTP API routes (`/api/plugins/<plugin-id>/...`), and declarative UI widget specs; one plugin, any combination of parts. There is no central manual registry, no decorator, and no build-time manifest.

## Configuration and state

Author-facing configuration is YAML referencing registered workflows by class path with kwargs; JSON is an accepted equivalent dialect. The thin config layer arrives in v0.1 because standalone deployment needs it; the editor storage format arrives in v0.4 as the same format widened to cover every serializable field. It serializes data only: structure, schemas, prompt templates, parameters, thresholds. Code-bearing members (`run()` overrides, method-form prompts, observer predicates) appear only as class-path references to Python-defined workflows; ngen-weave never serializes user code or guarantees its reproducibility. Git owns code history, provenance plus envelope versions own run history. No cross-version validity promise before 1.0: while usage is small the format may still be reshaped with migration notes; from 1.0 onward widening comes from coverage, never breaking changes.

Run state starts as one JSON file per run under `.ngen-weave/`: metadata plus the event/provenance stream, written atomically at each transition, always re-runnable from its contents. That store lasts exactly through v0.1. When serving arrives at v0.2, the stream moves into a project SQLite database behind the same store interface (one append per record instead of rewriting a growing file), and the single-file JSON becomes an export produced on demand by one writer. Artifacts live content-addressed under `.ngen-weave/projects`, Obsidian-style project trees that accept arbitrary file types. SQLite remains the local store; Postgres takes the role over at v0.6, with the JSON export retained permanently for reproducibility and handoff.

## Roadmap

| ver | theme | ships |
|---|---|---|
| v0.1 | Core | Workflow/node classes, pydantic boundaries, recursion native, provenance records, thin YAML config + registry, CLI, canonical code-review example end to end |
| v0.2 | Exposure | RunService protocol; own FastAPI serving implementation; SQLite run store with run JSON demoted to an on-demand export; MCP server; run/thread API; budget enforcement; supervision/observers baked into the scheduler (pause only); AgentNode harness against a real model |
| v0.3 | Read UI | ngen-weave-web scaffold, graph canvas, detail views, projects browsing, docs site begins |
| v0.4 | Editor MVP | data-only storage format, UI create/edit/launch/review, artifact diffs, PROV-JSON export, budget controls in UI |
| v0.5 | Extensibility | plugins via entry points with project-level capability grants — node types, services, workflow packs, namespaced API routes, spec-driven UI widgets plus prebuilt component bundles — built-ins as reference registrations, notification reference plugins (email/text), boxed agentic autonomy with MCP loopback |
| v0.6 | Platform | Postgres canonical store with Alembic migrations (runs, provenance, definitions), async store seams |
| v0.7 | Consolidation | external document links, deferred cleanup |
| v0.8 | Identity | WorkOS-backed auth, project/run roles, multi-reviewer human nodes, tickets, import-project |
| 1.0 | Deployed product | standalone server deployment (own FastAPI service), multi-project, editor, remote review, budgets, provenance export, notifications, container volume mounting and artifact-store configuration, semver'd API |
| 1.1 | Distribution | Argo export target, remote node dispatch |
| 1.2 | Collaboration | full team features |
| 1.3+ | Verticals | coding IDE pack first, research pack after |

Version 1.0 means the thing deploys as a server and a stranger can use the whole loop without reading source. It does not mean distributed orchestration; Argo waits.

## Toolchain

Python ≥3.12, uv-managed workspace, pytest, ruff, Apache-2.0, Forgejo CI (optional GitHub mirror for reach), conventional commits driving a changelog, MkDocs Material docs from v0.3.

## Decision log

Kept here because these questions were expensive to settle and will be expensive to reopen.

- Primitives are Python objects; no hand-authored YAML graph files. Config references registered Python workflows.
- Identity is the fully-qualified class path, never an author-chosen name string. A required unique `name` attribute was rejected: two plugins shipping a class called `Gate` must coexist, and module paths already guarantee that. Provenance and run files are uglier and refactors move addresses — accepted costs, recorded here so nobody relitigates them per error message.
- Model binding lives entirely in the config layer; code declares structure, config assigns models. A top-level `models:` section in the run config maps fully-qualified class paths to variant names from `models.json`. Keys match concrete class paths exactly — inheritance plays no role. Keying a composite assigns its variant to every model-calling leaf in that subtree, however deep; keying a leaf overrides any enclosing scope for that leaf alone. When scopes nest, the innermost wins. Wanting the same function on a different model means keying the leaf directly or shipping an explicit class copy with its own path — never a precedence contest between related classes. The engine resolves at compile time and freezes into the compiled node, so resumed runs resolve identically and provenance records the variant actually used.
- No shorthand keys in bindings or references — canonical fully-qualified class paths are the only form on disk, ever. Shorthand resolution by proximity would make config validity depend on what plugins happen to be installed. Convenience belongs in tooling that emits canonical files (`ngen-weave validate` prints the resolution table; the UI offers resolved-class pickers), never in parsers that accept loose forms.
- Workflow classes carry no model attributes (`variant`, `model_map` rejected). Class attributes referencing variant names would make shipped definitions depend on project environment data they have never seen. A library-shipped workflow runs on `defaultVariant` until configured — correct behavior, not a gap. If author-intent hints ("this subtree is cost-sensitive") ever prove necessary, they arrive as metadata the config may honor or override, not as a binding mechanism.
- Generics dropped. Plain `input_type`/`output_type` class attributes give the same guarantees without runtime type-parameter resolution.
- LangGraph wrapped directly, never exported to. No compile step between definitions and execution until Argo (1.1), which compiles from serialized definitions.
- Flask rejected (WSGI). FastAPI throughout; async has nothing to do with Python version, and 3.12 is the floor until pydantic-core and langgraph officially support newer releases.
- langgraph-server was evaluated as the v0.2 serving path and rejected. It would have handed over durable serving and interrupt/resume early, but ngen-weave semantics would live partly behind someone else's API, and 1.0 requires our own FastAPI service regardless. The earlier dual-implementation plan (adapter plus owned service through v0.6, adapter retired at v0.7) was dropped: it bought a config-change switch nobody needed while costing four versions of duplicated maintenance against an unstable protocol. One implementation, ours, from v0.2. RunService stays the seam so a future alternative backend remains possible without re-plumbing callers.
- Serialization covers data only. User code is referenced by class path, never serialized, never promised reproducible. Reproducibility of runs comes from primitive class paths, record-envelope versions, and provenance records.
- Observers moved wholesale out of v0.1 into v0.2, where they are baked into the scheduler instead of living as a parallel graph-level facility. The exact mechanism is settled when the scheduler is planned, not before. Predicates are structured builders (`gt`, `lt`, `and_`, `or_`, ...), not bare lambdas: each node renders its own description, so the UI-facing descriptor is mechanically generated from the evaluated object and cannot drift from it. Actions start at pause only; stop may follow when needed; reroute is deferred to post-1.0 because it alone requires runtime graph rewiring. The earlier shape (predicate function plus required description string as its serializable copy, plus an `observe()` method override) was rejected as two mechanisms for one job and a description guaranteed to go stale.
- Human routing reads the submitted internal state through routers declared in `build()`; there is no separate branch-label vocabulary — branch-map keys are the possible values. No mandatory downstream control node per review.
- Human artifacts stay simple: flat `state_type` models and path-string prefill only. Nested-model slot expansion and callable prefill were stripped before v0.1 as speculation; they return when a concrete workflow demands them.
- Artifact blob stores are configurable from 1.0 (local disk default, object-storage-ready); container deployments mount volumes explicitly. Multi-project servers never depend on implicit local directories for blobs.
- One JSON file per run shipped in v0.1, replacing JSONL streams: atomic-write, always complete, correct at CLI scale. Whole-file rewrite per transition is quadratic in run length, so when serving arrived the backing moved rather than the interface: from v0.2 the event/provenance stream appends to a project SQLite database behind the same RunStore methods, and the single-file JSON became an on-demand export written by one serializer (`dump_run_json`) whose bytes match the v0.1 files exactly. Early runs therefore stay readable forever, and Postgres at v0.6 inherits the same seam instead of inventing an export format.
- Provenance is emitted unconditionally; log verbosity varies, silence does not.
- No API backwards compatibility before 1.0. After 1.0, both repos' public surfaces follow semver.
- External documents (Google Docs etc.) are linked, never stored. Their provenance belongs to their providers. Local-first file-based projects remain fully supported permanently.
- Composites declare structure in `build()` over `GraphBuilder`, not as class attributes. This reverses an earlier attributes-only stance: the editor needs compiled topology regardless (structure is code-referenced either way), so a dry-run compile serves both validation and introspection, and authors get LangGraph's full wiring surface without a parallel edge mini-language. `build()` wires, it does not compute. Determinism enforcement was first specced as a double-build through a recording sink with op-sequence comparison; rejected because comparing routers false-fails on closures created inside `build()`, triples the build cost, and still cannot see environment-dependent wiring (the environment is stable within a process, so both builds agree). Validation builds once against a real throwaway StateGraph and enforces the no-compute contract with a static lint over `build()`'s source instead.
- Workflow-author routing is static. Routing is whatever conditional edges `build()` declares, validated at import time; LangGraph's dynamic routing (`Command` goto from inside a node function) is an engine implementation detail, never an author-facing surface. Needs static edges cannot express get new builder vocabulary, not dynamic routing. Observer rerouting, if and when it returns post-1.0, is likewise engine-side or scheduler-side, never author-facing dynamic goto.
- Fan-in is explicit and compile-checked; there are no merge-overwrite semantics anywhere in the engine. A node fed by multiple parents must fit one of two forms declared by its own input schema: named slots (edges carry `into="<field>"`, targeting fields of the child's `input_type`; every required field covered exactly once, each source's `output_type` validated against the targeted field) or collected fan-in (the child's `input_type` contains exactly one `list[<model>]` field whose pydantic `min_length`/`max_length` bounds the accepted parent count). Duplicate slots, uncovered required fields, arity outside bounds, a second list field, or multiple parents on a Human node are dry-run-compile errors. An earlier rule that merged all incoming dumps with later edges overwriting was rejected: no value may depend on edge declaration order.
- Scheduling is launch-now until we own the service layer; cron parity is not allowed to block owning the service layer.
- AgentNode waits for its harness. An earlier plan declared the node in v0.1 with a mocked executor as a "seam"; rejected: a mock of executor behavior nothing has exercised proves nothing, and v0.2 replaced it wholesale anyway. The node ships where it is driven by a real model loop (v0.2), boxed for real in v0.5.
- Config and definition format validity is promised from 1.0 onward only. An earlier "everything valid stays valid forever" commitment dated from v0.1; rejected as a promise made before first contact with users. Pre-1.0 the format may be reshaped with migration notes while usage is small; at 1.0 the widening-not-breaking rule becomes binding.
- One CLI name (`ngen-weave`). A short alias was planned as a second console script from v0.1; dropped: aliases are cheapest to add when someone actually complains, and until then they double every naming decision in docs and tests. The `ngw` prefix survives only in machine namespaces that predate this decision (`ngw.yaml` config files, `X-Ngw-*` headers, `ngw:` PROV identifiers).
- Documentation lives under defs, not at the top of files. Every public class/function carries a docstring saying what it achieves; a module header is at most two lines naming the module's job, never an index of its contents. It is documentation where you read it, not a navigation aid to query the top of a file instead — no rationale, history, usage examples, or signatures beyond names, and it must never grow into a parallel spec (that way staleness lies). Accuracy is maintained in the same commit as the change that invalidates it.
- Collaboration features (simultaneous editing, team workspaces) start post-1.0. Roles arrive with auth at v0.8; the database lands alone at v0.6 so the storage migration is never entangled with identity work. Auth itself is delegated to WorkOS (AuthKit-hosted login, SDK token verification): ngen-weave keeps only local role rows and a `Principal` boundary, and never builds user management, password handling, or session machinery.
- v0.1 implementation drift (recorded at closeout): `ngen-weave resume <run-id>` gained a `--project <name>` option so a resumed run can persist declared artifacts; the original CLI spec had no project flag on resume.
- v0.1 implementation drift: engine nested activations use per-level checkpoint thread ids — LangGraph ignores caller-supplied `checkpoint_ns` at top-level invoke, so without distinct thread ids per nesting level a durable interrupt at depth 2 on SQLite could not resume; per-level thread ids were required to make release criterion 3 pass.
- v0.1 implementation drift: run files land at `.ngen-weave/runs/<run-id>.json` (one flat JSON file per run), not the `<run-id>/` directory layout sketched in the v0.1 release doc; atomic whole-file write semantics are unchanged.

## What would make this fail

Named so nobody pretends otherwise. If the recursive Workflow abstraction turns out to make interrupts and cost attribution across nesting levels messy in practice, everything above it gets harder, so v0.1 tests two-level nesting with attribution before anything else builds on it. If two discovery sources ever disagree about a name, config breaks confusingly, so duplicate registrations fail loudly at startup and every consumer resolves names through one merged discovery map. And if the web API accretes business logic, the frontend stops being swappable, so reviews grep for logic outside core.
