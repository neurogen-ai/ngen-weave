# ngen-weave product PRD

Status: authoritative. This document defines the whole product from v0.1 through 1.2 and beyond. Release requirement docs under `releases/` describe what each version must deliver; where they conflict with this one, this one wins. Technical design lives under `design/`, per-feature implementation plans under `implementation/`.

## Problem

Agent workflows today come in two bad flavors: raw code (LangGraph graphs, custom scripts) or loose markdown conventions with no contracts between steps. Code gives control but every workflow is a program someone has to read. Loose conventions give files but nothing stops a step from emitting garbage that breaks the next one. Neither gives you provenance, cost visibility, or a place for a human to stand in the loop.

There is also a gap above them. Tools like Conductor, Temporal, and Airflow own queuing, scheduling, sharding, and failure domains, and they are good at it. None of them offer typed node contracts, human review as a structured artifact, cost policies on every node, or workflows that nest like components. That combination is what ngen-weave builds.

## Thesis

A minimalist graph-based workflow and artifact management environment, extended into products through plugins.

The core is a small set of primitives: nodes with strict input/output validation, boolean control routing, human review artifacts, observers that watch cost and token spend on every activation, recursive composition (every workflow is itself a node), and provenance records written by default. Everything else, including the UI, is replaceable or pluggable around those primitives.

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
    name = "code_review"            # unique registry name
    input_type = ReviewInput        # pydantic model
    output_type = ReviewOutput

    children = [draft, gate, human_review]   # composites declare structure
```

Rules:

- Inputs and outputs are pydantic models validated strictly at every boundary. The same classes generate the JSON Schema that config files, the UI, and MCP tool registration consume.
- Composite workflows declare structure as class attributes: `children`, `edges`, and per-target `inputs` fan-in maps. Attributes, not builder methods, because the validator, manifest generator, and editor all need to introspect structure without executing code. Their execution is engine-managed subgraph runs, checkpointed and interruptible at any depth. A composite's `run()` delegates to the engine; only leaves override `run()` with arbitrary pure logic (pure function of definition, input, context).
- Human nodes carry an internal state model between input and output (`state_type`, a pydantic model), editable and prefilled from the incoming context. Submission validates `state_type`; edges out of a human carry branch labels matched against state values (enums generalize control's pass/fail to `"approve"` / `"reject"` / anything). Routing is therefore decided by what the human submitted, without a downstream control node. State and output are deliberately distinct models: what the human edits is not necessarily what travels downstream. By default the validated state is passed through as the output; a subclass may override with a programmatic transformation from (context, state) to output.
- Worker prompts are a `prompt` class attribute: a template string rendered against validated input fields, overridable by a method when generation needs logic.
- Human nodes have one edge input, surfaced as read-only `context` in their review artifact; the human's contribution is the artifact's `response` section, which exists only once a person submits and never travels over an edge. Response slots are generated from the state model's leaf primitive fields (nested models expand recursively, defaults may be left empty, required-without-default blocks completion), and completion validation is `state_type.model_validate(response)`; output validation runs afterwards on whatever the transformation produces. A per-node `prefill` map seeds slots from the context (paths primarily, callables where logic is needed); prefill fills but never completes an artifact, since human submission is what resumes the run.
- Every activation emits provenance records (`run_id`, `node_path`, kind, payload) without opt-in. Observers are constructed with a predicate function over a frozen six-field metadata object (iterations, tokens in context, tokens total, cost, elapsed time, last output validity) plus a required description string that serves as the serializable copy of the expression; actions are pause, stop, reroute, and reroute targets are declared on the observer, never inferred from predicate source. In addition, every workflow may override an internal `observe(metadata)` method evaluated after its declared observers, giving programmatic access to the same actions for cancellation and custom behavior. ngen-weave does not police predicate or method code beyond useful errors when the contract is broken.
- Human nodes interrupt the run and write review artifacts. Resuming means filling the artifact, locally as YAML or remotely as JSON, both carrying identical payloads.
- Node types ship as Worker, Control, Human, and Agent. AgentNode exists from v0.1 as a declared seam with a mocked executor; real boxed autonomy lands in v0.5, enforced engine-side through PermissionSets (allow/deny lists, budget caps, forced return-to-review points), not by prompting.
- Registration: workflows are ordinary `Workflow` subclasses discovered by explicit listing, never by scanning. Distributions declare workflow modules under a `ngen-weave.workflows` entry-point group; projects list modules in `ngen-weave.json`; data-only definition files live in `.ngen-weave/definitions/`. Importing a listed module auto-registers every `Workflow` subclass found there (name = its `name` attribute); duplicates fail loudly. Plugin node kinds and services register through ordinary package entry points. There is no central manual registry, no decorator, and no build-time manifest.

## Configuration and state

Author-facing configuration is YAML naming registered workflows with kwargs; JSON is an accepted equivalent dialect. The thin config layer arrives in v0.1 because standalone deployment needs it; the editor storage format arrives in v0.4 as the same format widened to cover every serializable field. It serializes data only: structure, schemas, prompt templates, parameters, thresholds. Code-bearing members (`run()` overrides, method-form prompts, callable prefills, observer predicates) appear only as registry-name references to Python-defined workflows; ngen-weave never serializes user code or guarantees its reproducibility. Git owns code history, provenance plus envelope versions own run history. The v0.1 subset stays valid forever; widening comes from coverage, not breaking changes.

Run state starts as one JSON file per run under `.ngen-weave/` (metadata plus the event/provenance stream), written atomically at each transition, always valid, always re-runnable from its contents. Artifacts live content-addressed under `.ngen-weave/projects`, Obsidian-style project trees that accept arbitrary file types. SQLite checkpoints locally; Postgres becomes the canonical store at v0.6, with JSON export retained for reproducibility and handoff.

## Roadmap

| ver | theme | ships |
|---|---|---|
| v0.1 | Core | Workflow/node classes, pydantic boundaries, recursion native, observers, provenance records, thin YAML config + registry, CLI, canonical code-review example end to end |
| v0.2 | Exposure | RunService protocol; langgraph-server integration plus basic own-FastAPI implementation; run JSON; MCP server; run/thread API; budget enforcement; AgentNode harness against a real model |
| v0.3 | Read UI | ngen-weave-web scaffold, graph canvas, detail views, projects browsing, docs site begins |
| v0.4 | Editor MVP | data-only storage format, UI create/edit/launch/review, artifact diffs, PROV-JSON export, budget controls in UI |
| v0.5 | Extensibility | plugins via entry points with project-level capability grants, built-ins as reference registrations, notification reference plugins (email/text), boxed agentic autonomy with MCP loopback |
| v0.6 | Platform | Postgres canonical store with Alembic migrations (runs, provenance, definitions), async store seams; run JSON becomes an export format |
| v0.7 | Consolidation | retire the langgraph-server adapter, external document links, deferred cleanup |
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

- Primitives are Python objects; no hand-authored YAML graph files. Config names registered Python workflows.
- Generics dropped. Plain `input_type`/`output_type` class attributes give the same guarantees without runtime type-parameter resolution.
- LangGraph wrapped directly, never exported to. No compile step between definitions and execution until Argo (1.1), which compiles from serialized definitions.
- Flask rejected (WSGI). FastAPI throughout; async has nothing to do with Python version, and 3.12 is the floor until pydantic-core and langgraph officially support newer releases.
- langgraph-server evaluated first behind RunService, but our own FastAPI implementation is required by 1.0 and grows incrementally from v0.2 so the switch is a config change. Both implementations ship through v0.6 to keep development testable; a dedicated v0.7 step retires the adapter rather than folding its deletion into 1.0.
- Serialization covers data only. User code is referenced by registry name, never serialized, never promised reproducible. Reproducibility of runs comes from primitive names, record-envelope versions, and provenance records.
- Observer predicates are ordinary functions handed to the `Observer` constructor; the required description string is their serializable copy. We provide sensible APIs and clear errors, not policing of user code.
- Human routing reads the submitted internal state; branch labels on human out-edges match state values. No mandatory downstream control node per review.
- Artifact blob stores are configurable from 1.0 (local disk default, object-storage-ready); container deployments mount volumes explicitly. Multi-project servers never depend on implicit local directories for blobs.
- One JSON file per run replaces JSONL streams until the database takes over. Files stay atomic-write and complete at every transition.
- Provenance is emitted unconditionally; log verbosity varies, silence does not.
- No API backwards compatibility before 1.0. After 1.0, both repos' public surfaces follow semver.
- External documents (Google Docs etc.) are linked, never stored. Their provenance belongs to their providers. Local-first file-based projects remain fully supported permanently.
- Scheduling is launch-now until we own the service layer; cron parity is not allowed to block the move off langgraph-server.
- Collaboration features (simultaneous editing, team workspaces) start post-1.0. Roles arrive with auth at v0.8; the database lands alone at v0.6 so the storage migration is never entangled with identity work. Auth itself is delegated to WorkOS (AuthKit-hosted login, SDK token verification): ngen-weave keeps only local role rows and a `Principal` boundary, and never builds user management, password handling, or session machinery.

## What would make this fail

Named so nobody pretends otherwise. If the recursive Workflow abstraction turns out to make interrupts and cost attribution across nesting levels messy in practice, everything above it gets harder, so v0.1 tests two-level nesting with attribution before anything else builds on it. If two discovery sources ever disagree about a name, config breaks confusingly, so duplicate registrations fail loudly at startup and every consumer resolves names through one merged discovery map. And if the web API accretes business logic, the frontend stops being swappable, so reviews grep for logic outside core.
