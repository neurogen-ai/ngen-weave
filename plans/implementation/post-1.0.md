# post-1.0 implementation plan (v1.1 distribution, v1.2 collaboration, v1.3 vertical packs)

## Context

After v1.0 (`implementation/v1.0.md`) ngen-weave deploys as its own FastAPI service against Postgres, every route lives under `/api/v1` behind a frozen OpenAPI snapshot and `design/api-contract.md`, projects are first-class isolated entities under authenticated users with owner/reviewer/observer roles resolved through `ProjectScope`, budgets, notifications, tickets, plugins (entry-point `ngen-weave.plugins`, node-kind registry `NODE_KINDS`), boxed AgentNode autonomy, MCP, and semver-governed public surfaces all exist. What does not exist: any execution path off the local engine process, anything above single-user-per-project collaboration beyond the role model, or any vertical-specific workflow content.

This plan ships three releases in order: **v1.1** compiles serialized workflow definitions to Argo Workflows and dispatches node execution to remote workers returning results through the checkpoint store; **v1.2** adds shared workspaces, ticket queues, concurrent review sessions, team notification routing, and simultaneous workflow editing in the UI; **v1.3+** ships vertical packs (coding IDE first, research after) as ordinary v0.5 plugin packages.

Files touched:

- `packages/ngen-weave-core/src/ngen_weave/compile/` (**new**, v1.1): compiler seam, renderer registry, Argo renderers
- `packages/ngen-weave-core/src/ngen_weave/dispatch.py` (**new**, v1.1): dispatch backend protocol and local backend
- `packages/ngen-weave-server/src/ngen_weave_server/`: compile route, dispatch wiring, hook route, workspace/review-session routes
- `packages/ngen-weave-core/src/ngen_weave/workspaces.py` (**new**, v1.2), `review_sessions.py` (**new**, v1.2)
- `packages/ngen-weave-core/alembic/versions/0005_workspaces.py`, `0006_ticket_queues.py` (**new**)
- `packages/ngen-weave-cli/src/ngen_weave_cli/`: `compile` command
- `packs/ngen-weave-pack-coding/` (**new**, v1.3), `packs/ngen-weave-pack-research/` (**new**, v1.3)
- `design/distribution.md`, `design/collab-editing.md` (**new**)
- `../ngen-weave-web/`: Argo export view, workspace switcher, live review sessions, collaborative editor client

Depends on: `releases/post-1.0.md` (requirements, wins on nothing because `product/PRD.md` wins overall), `product/PRD.md` (decision log, especially the LangGraph-wrapped-not-exported and Argo-compiles-from-serialized-definitions decisions), `releases/v1.0.md` explicit-outs, and module shapes fixed by `implementation/v0.4.md` (data-only storage format), `implementation/v0.5.md` Steps 2–4 (plugin loading, `NODE_KINDS`, service resolution), `implementation/v0.6.md` Steps 1–5 (tables, async stores), `implementation/v0.8.md` (auth matrix, collaboration slice, importer), `implementation/v1.0.md` Step 1 (versioned routing, contract doc).

Conventions inherited unchanged: Python ≥3.12, uv workspace, pytest with fakes instead of network except `live`-marked tests, ruff, conventional commits, one commit per step, full non-live suite green before committing, web repo pnpm/Vitest/Playwright.

Global out of scope for every step (per `releases/post-1.0.md`): Temporal as a built backend (stays documented alternative), storing external documents' contents, plugin marketplace infrastructure, interpreter-level sandboxing, cron scheduling beyond what Argo itself provides on compiled workflows.

---

## Goals

### v1.1: distribution

1. A registered workflow compiles to a valid Argo Workflows spec purely from its serialized definition (`DefinitionStore.load` output), with zero import of live Python objects at compile time.
2. The mapping is exactly the release doc's: workers become container templates, control booleans become conditional DAG edges, human nodes become `suspend` templates resumed through the checkpoint store, fan-in maps become DAG dependencies.
3. Node dispatch has one seam: the engine asks a `DispatchBackend` where/how to execute a leaf activation. The local in-process backend is the default and the only shipped behavior until configured otherwise; an Argo backend runs leaves as pods whose results land back through `RunRepo`.
4. Gaps Argo leaves (cross-node retry policy, priority) are bridged as fields on the compiled spec, not as a second scheduler.
5. `ngen-weave compile <workflow> --target argo -o out/` works offline from the CLI; `POST /api/v1/workflows/{name}/compile` serves the same output.

### v1.2: collaboration

6. Workspaces group projects and members; roles resolve within a workspace, projects inherit membership unless overridden.
7. Tickets gain assignee queues filtered by role; a reviewer sees their queue and picks up work.
8. Multiple humans occupy one review artifact concurrently: each sees who is present and each submission lands without clobbering others (multi-reviewer completion semantics from v0.5 Step 8 stay authoritative).
9. Notification rules target workspace groups, not just individuals.
10. Two people edit the same workflow definition in ngen-weave-web simultaneously; both clients converge and persistence writes the data-only storage format. The OT-vs-CRDT mechanism is chosen once, by experiment, in Step 11, and recorded in `design/collab-editing.md`.

### v1.3+: vertical packs

11. The coding pack installs as a plugin package providing code-graph artifacts, repository-aware review workflows, and CI hooks that launch runs on PR events, with zero core changes; if a core change turns out necessary, it is made and justified as a seam fix before the pack ships (release-doc rule).
12. The research pack follows the same shape: search provider plugins, literature-review workflows, citation-bearing artifacts, provenance exports shaped for open-science reproducibility.

## Success criteria

From `releases/post-1.0.md`, verbatim in intent:

**v1.1**
1. A two-level composite with worker → control → human → fan-in compiles to Argo, applies against a real cluster (live-marked), suspends at the human node, resumes when the artifact response is written to the checkpoint store, and completes with provenance records identical in shape to local-mode records for the same run.
2. Compiling reads only the serialized definition: the compile test suite imports no workflow modules and passes with the packages' workflow sources removed from the import graph (enforced by test, Step 3).
3. The same run executes end to end locally (no Argo) after this release, proving dispatch is behind the seam and defaulted off.
4. Retry count and priority set on a node appear in the compiled spec as Argo `retryStrategy` / `priority` without engine changes elsewhere.

**v1.2**
5. A user in workspace W sees only W's projects; an observer in project A cannot list project B's runs even when both sit under one workspace.
6. Two browser sessions open the same pending review artifact; both submit distinct slot values; the artifact holds both contributions and the run resumes exactly when completion validation passes.
7. Two browsers edit one workflow definition; after reconnecting both show the merged document and `DefinitionStore.load` returns a valid, hash-consistent definition.
8. A notification rule addressed to "workspace reviewers" fires once per qualifying event per member.

**v1.3**
9. `uv pip install packages/ngen-weave-pack-coding && enable in project config` makes the pack's workflows, node metadata, and CI hooks appear through the ordinary v0.5 plugin path; uninstalling removes them cleanly.
10. A Forgejo PR webhook triggers a repo-aware review run that posts findings back as PR comments using only pack-provided code and published core interfaces.
11. Research pack search providers answer queries through the same service-resolution interface the CompletionProvider uses; a literature-review run produces citation-bearing artifacts whose PROV-JSON export names the source records.

## Test map

| area | location |
|---|---|
| compiler seam, renderer registry | `src/ngen_weave/compile/test_compiler.py` |
| Argo renderers, spec validity | `src/ngen_weave/compile/test_argo.py` (validates against `argoproj/argo-workflows` JSON schema vendored as fixture) |
| compile-from-serialization guard | `src/ngen_weave/compile/test_no_live_objects.py` |
| dispatch seam, local default | `src/ngen_weave/test_dispatch.py`, `engine/test_runner_dispatch.py` |
| Argo dispatch round-trip | `tests/e2e/test_argo_dispatch.py` (live-marked, kind cluster) |
| CLI compile | `packages/ngen-weave-cli/tests/test_compile_cmd.py` |
| compile API route | `packages/ngen-weave-server/src/ngen_weave_server/test_compile_api.py` |
| workspaces, migration 0003 | `src/ngen_weave/test_workspaces.py`, `db/test_migrations.py` (extended) |
| ticket queues, migration 0006 | `src/ngen_weave/tickets.py` tests (extended) |
| review sessions | `src/ngen_weave/test_review_sessions.py`, server `test_review_sessions_api.py` |
| collab editing sync | web Vitest + server `test_collab_relay.py`; Playwright two-context spec |
| notification group routing | `src/ngen_weave/test_notify_groups.py` |
| pack loading via plugin path | `packs/*/tests/test_pack_loads.py` |
| CI hook route | `packages/ngen-weave-server/src/ngen_weave_server/test_hooks_api.py` |
| per-version success e2e | `packages/ngen-weave-server/tests/e2e/test_v11_criteria.py`, `test_v12_criteria.py`, `test_v13_criteria.py` |

Web (repo `../ngen-weave-web/`): Argo YAML preview component, workspace switcher tests, review-presence component tests, collab editor two-client Playwright specs.

---

## Abstractions and what they absorb

One sentence each; these are the load-bearing choices.

- **The compile seam as one protocol, `CompileTarget.compile(definition, *, name) -> CompiledArtifact`, fed exclusively by `DefinitionStore.load`** absorbs new orchestration backends (Temporal stays a future target): a backend is a package of renderers plus an entry-point registration, never engine edits.
- **A renderer registry keyed by node kind, mirroring v0.5's `NODE_KINDS` pattern (`ARGO_RENDERERS: dict[str, NodeRenderer]`)** absorbs new node types and changed mappings: a new kind adds one registration in its own file; plugins may ship renderers for their own kinds through the existing plugin entry point.
- **The `DispatchBackend` protocol with `dispatch(activation) -> DispatchTicket` and results landing only through `RunRepo`** absorbs new executors (batch systems, cloud jobs) and changed result plumbing in one place, because the runner already treats checkpoints as the only inter-process channel.
- **Retry/priority carried on the node's serialized metadata and translated per-backend inside renderers/dispatchers** absorbs scheduling-gap changes (deadlines, queues) as metadata fields plus one translation site per backend.
- **`WorkspaceScope` composed into the existing `ProjectScope` dependency (scope gains a field, callers unchanged, per the v1.0 abstraction contract)** absorbs deeper nesting (organizations) later without touching routes.
- **Review sessions as a server-held session object keyed by artifact id, with submissions funneled through one merge function before completion validation** absorbs new concurrency shapes (watchers, locks) inside the session object; the artifact schema and resume semantics do not move.
- **Collab editing behind one relay boundary: clients speak the mechanism's op/document protocol to `/api/v1/collab/{definition}`, persistence subscribes to committed snapshots and writes the data-only format** absorbs the OT/CRDT winner (and any later replacement) without re-touching editor features, storage, or validation.
- **Packs as pure plugin consumers constrained to published seams (entry points, `NODE_KINDS`, service resolution, HTTP API)** absorbs new verticals entirely outside the monorepo; a required core change is by definition a bug report on a seam.

---

## Commit sequence

### Step 1: Distribution design doc

`design/distribution.md` (**new**). Paperwork only; no code.

Content fixed here so the implementing agent writes it in intent:

- Mapping table (release-doc rules made concrete): Worker/Agent → `container` template running `ngen-weave exec-node` with the definition hash, node path, and input passed as arguments/env; Control → DAG edge `when:` expressions compiled from the control node's boolean outputs; Human → `suspend` template; resumption happens out-of-band when the artifact response reaches the checkpoint store, which the watcher pod observes; composite children → nested DAG with fan-in expressed as ordinary DAG dependencies onto the aggregated inputs.
- Compile-time input contract: everything comes from `WorkflowDefinition` (v0.4 format) plus static config; the compiler must not import workflow Python.
- Runtime contract: remote leaves write results as checkpoint/provenance records through the same stores the server uses; no direct worker-to-worker calls ever.
- Supervision boundary (release-doc rule, made concrete): observers and budget hooks fire only at activation boundaries — inside `exec-node`'s wrapper around the leaf, or at engine-owned transitions like human resume. The mapping table records where each hook fires per node kind; a definition needing supervision finer than that raises `CompileError` rather than silently degrading.
- Worker image contract: the image carries the ngen-weave distribution plus every plugin package owning registry names the definition references; `exec-node` resolves those references through the standard loader and fails loudly at container start on a gap.
- Bridge policy: cross-node retries and priority are per-node serialized metadata fields (`retry: int`, `priority: int | null`) translated by each backend; ngen-weave never runs a scheduler loop of its own.
- Temporal named as documented alternative, not built (release-doc rule).
- Cross-reference, do not restate: PRD architecture section, `implementation/v0.4.md` storage format (JSON Schema model documents), v1.0 `design/api-contract.md`.

Out of scope: any code, UI mockups, cost estimates.

Verify: `grep -c 'mapping' design/distribution.md` returns ≥1 and the doc renders (`mkdocs build` if wired, else skip); full suite still green (`uv run pytest -m 'not live'`) since nothing changed.

Commit: `docs(design): distribution mapping and runtime contract`

---

### Step 2: Compile seam and renderer registry

`packages/ngen-weave-core/src/ngen_weave/compile/__init__.py`, `base.py`, `registry.py` (**all new**). No Argo yet; this step is the seam plus a fake backend used by tests.

```python
# compile/base.py
from dataclasses import dataclass
from typing import Any, Protocol

@dataclass(frozen=True)
class CompiledArtifact:
    target: str                      # e.g. "argo"
    name: str                        # sanitized workflow name
    files: dict[str, str]            # relative path -> text content
    entrypoint: str                  # primary file path inside files
    metadata: dict[str, Any]         # target-defined extras (never read by core)

class CompileTarget(Protocol):
    name: str
    def compile(self, definition: dict, *, name: str) -> CompiledArtifact:
        """definition is the serialized workflow definition as stored by DefinitionStore."""

class CompileError(Exception):
    """Unsupported node kind, unmappable structure, or malformed definition."""
```

```python
# compile/registry.py
from dataclasses import dataclass
from typing import Callable

@dataclass(frozen=True)
class RenderedNode:
    templates: list[dict]     # raw target-native template fragments
    dependencies: list[str]   # upstream node paths this node waits on
    condition: str | None     # edge condition inherited from a control parent, if any

NodeRenderer = Callable[[dict], RenderedNode]
# dict argument: the node's serialized definition subtree. Raises CompileError on anything unmappable.

RENDERERS: dict[str, NodeRenderer] = {}   # key: node kind string, matching NODE_KINDS keys

def register_renderer(kind: str, renderer: NodeRenderer) -> None:
    if kind in RENDERERS:
        raise ValueError(f"renderer already registered for kind {kind!r}")
    RENDERERS[kind] = renderer
```

`compile/__init__.py` exposes `compile_definition(definition: dict, *, target: str, name: str) -> CompiledArtifact`: resolves the target from the entry-point group `ngen-weave.compile-targets` (same loading machinery as v0.5 plugins), walks the definition tree depth-first, calls the registered renderer per node kind, assembles `dependencies` into the target's edge structure via the target's own assembler, raises `CompileError("no renderer for kind X")` on gaps. Unknown kinds are always an error at the seam; a target may additionally consult plugin-provided renderers registered through `ngen-weave.plugins` (wired here, used by packs in v1.3).

Built-in renderers for the four built-in kinds register from stub implementations in this step (each returns minimal `RenderedNode`s sufficient for the fake target); real bodies land in Step 3. Registration call sites live beside each kind's renderer module, mirroring how `NODE_KINDS.register` sites sit beside built-ins.

Out of scope: any Argo vocabulary, CLI/server surfaces (Steps 5), dispatch (Step 4), validating rendered output against external schemas (Step 3).

Absorbs: new compile targets and new node kinds each land in their own registration; the walk logic never grows a branch.

Verify: `uv run pytest src/ngen_weave/compile/ -q` green; `uv run python -c "from ngen_weave.compile import compile_definition"` imports clean.

Commit: `feat(core): compile seam with per-kind renderer registry`

---

### Step 3: Argo target: renderers, spec assembly, serialization-only proof

`packages/ngen-weave-core/src/ngen_weave/compile/argo.py` (**new**), registered via entry point `ngen-weave.compile-targets = argo = ngen_weave.compile.argo:ArgoTarget`. Fixture schemas vendored at `packages/ngen-weave-core/tests/fixtures/argo-schema.json`.

Renderer bodies, decided here:

- **Worker/Agent** → one `container` template per node: image from the definition's `image` metadata field (default `ghcr.io/<org>/ngen-weave-worker:<version>`), command `["ngen-weave", "exec-node", "--definition-hash", ..., "--node-path", ...]`, input injected via env `NGW_NODE_INPUT` (JSON). `retry`/`priority` metadata translate to `retryStrategy.limit` and `priority`.
- **Control** → no own template body; contributes `when:` conditions on outgoing DAG edges, one per boolean output branch, referencing the control task's output parameter.
- **Human** → `suspend` template with `suspend: {}` (indefinite); the accompanying `activeDeadlineSeconds` only if the definition carries a timeout metadata field. Resumption is external (Step 4 watcher).
- **Composite** → `dag` template recursing through children; fan-in maps become named input parameters on the child dag task with `valueFrom.parameter` referencing upstream tasks.
- Edge assembly: `ArgoTarget.compile` builds one top-level `Workflow` manifest (`apiVersion: argoproj.io/v1alpha1`, `kind: Workflow`, entrypoint = root dag), serializes each `CompiledArtifact.files` entry as YAML.

Serialization-only proof, `test_no_live_objects.py`: builds a definition dict literal in the test, monkeypatches `sys.modules` entries for a sentinel workflow package to raise on import, then asserts `compile_definition(...)` succeeds. This pins the PRD decision mechanically.

Spec validity: every fixture workflow (reuse the canonical code-review example's stored definition from v0.4 fixtures) compiles to YAML that validates against the vendored Argo schema; golden-file tests lock the exact YAML for the canonical example, regenerated deliberately via `uv run python -m ngen_weave.compile.argo.golden` when mappings change.

Out of scope: applying to a cluster, image building/publishing, `exec-node` implementation (Step 4), CronWorkflow generation, workflow templating/parameterization beyond the fixed mapping.

Verify: `uv run pytest src/ngen_weave/compile/ -q` all green including the import-guard and golden tests.

Commit: `feat(compile): argo target with container/suspend/conditional-dag rendering`

---

### Step 4: Dispatch seam and the Argo round trip

`packages/ngen-weave-core/src/ngen_weave/dispatch.py` (**new**), `engine/runner.py` and `engine/store.py` (widened), `packages/ngen-weave-core/src/ngen_weave/argo_watch.py` (**new**).

```python
# dispatch.py
from dataclasses import dataclass
from typing import Any, Protocol

@dataclass(frozen=True)
class Activation:
    run_id: str
    node_path: str                 # dotted path, matches provenance node_path
    definition_hash: str
    input: dict                    # validated input dump
    budget_ctx: dict               # parent budget snapshot for attribution

@dataclass(frozen=True)
class DispatchTicket:
    backend: str                   # "local" | "argo" | ...
    handle: str                    # backend-local id (pid, pod name, ...)
    location: str | None           # human-readable locator for provenance

class DispatchBackend(Protocol):
    name: str
    async def dispatch(self, activation: Activation) -> DispatchTicket: ...
    async def cancel(self, ticket: DispatchTicket) -> None: ...

class LocalDispatch:
    """In-process execution, today's behavior, the permanent default."""
```

Runner change, one site: wherever the runner currently invokes a leaf's `run()` inline, it instead calls `await backend.dispatch(activation)` and awaits completion through the existing wait-on-checkpoint path (the runner already polls/awaits `RunRepo` state; remote results arrive as the same records, so the wait code is reused, not duplicated). Provenance gains one record kind `"dispatch"` carrying `DispatchTicket` fields; emission stays unconditional like every record.

`ArgoDispatch` (in `dispatch.py` or a sibling `dispatch_argo.py`, implementer's choice within the file layout above): submits the pre-compiled `Workflow` for the run's definition (compiled once per run, cached on the run record), maps activations to pod expectations, exposes `cancel` as Argo terminate. Requires `kubernetes` async client as an optional extra `ngen-weave[argodespatch]`; import failure yields a clear `DispatchError("argo backend requires the argodespatch extra")`.

Remote leaf completion: workers run `ngen-weave exec-node --definition-hash H --node-path P --run-id R`, which loads the definition by hash from `DefinitionStore`, reconstructs the node, executes, and appends the result record through the same `RunRepo`/store configuration resolved from environment (`NGW_DATABASE_URL` etc.). Human-node resumption under Argo: `argo_watch.py` provides a small long-running pod that watches suspended nodes and marks them complete when the artifact response record appears in `RunRepo` — the store remains the only channel, per the design doc.

Config: `ResolvedConfig` gains `dispatch: {backend: str = "local", ...}`; unknown keys rejected as usual. Server and CLI resolve the backend once at startup through the entry-point group `ngen-weave.dispatch-backends`.

Bridge fields: `retry` and `priority` ride on node metadata in the serialized definition (documented in the v0.4-format metadata section; additive, so old definitions stay valid) and are consumed only by renderers/backends.

Tests: local backend keeps today's behavior byte-for-byte (existing engine suite is the proof, untouched); fake-backend tests pin the runner's contract (ticket recorded, result awaited via records, dispatch provenance emitted); live-marked kind-cluster e2e runs criterion 1's scenario.

Out of scope: autoscaling, resource requests/limits UI, multi-cluster, priority preemption testing, watching anything other than suspend nodes.

Absorbs: a third executor later means one new entry-point registration; result-plumbing changes land in the runner's single dispatch call site.

Verify: `uv run pytest -m 'not live'` green; with a kind cluster: `NGW_TEST_ARGO=1 uv run pytest -m live packages/ngen-weave-core/tests/e2e/test_argo_dispatch.py` completes the suspended-then-resumed run.

Commits: `feat(engine): dispatch seam behind the runner`, `feat(dispatch): argo backend and suspend watcher`, `test(e2e): argo round trip on kind`

---

### Step 5: Compile surfaces: CLI and API

`packages/ngen-weave-cli/src/ngen_weave_cli/` (`compile` subcommand), `packages/ngen-weave-server/src/ngen_weave_server/api.py` (one route), OpenAPI snapshot regenerated.

CLI:

```
ngen-weave compile <workflow-name> [--target argo] [-o DIR] [--project NAME]
```

Resolves the definition through `DefinitionStore` (project-scoped, same as `run`), calls `compile_definition`, writes `CompiledArtifact.files` under `-o` (default `./dist/<name>/`), prints the entrypoint path. Exit codes: `2` unknown workflow/target, `3` `CompileError` with message.

API route, added to the existing versioned router (handlers never know their path, per the v1.0 mount):

```
POST /api/v1/workflows/{name}/compile
  body: {"target": str}                       # default "argo"
  200: {"name": str, "files": {path: text}, "entrypoint": str}
  404 unknown workflow | 400 CompileError message in error envelope | 403 observer role
```

Role gate: `require_role(principal, "edit")` reuse — compiling is a read of definitions but leaks structure, so reviewers and owners only. Snapshot test updated by regenerating `docs/api/openapi.json` (additive change, allowed anytime per `design/api-contract.md`).

Web: an "Export Argo" action on the workflow detail view hitting the route and offering the YAML bundle as a download (component test: fetch mocked, download triggered).

Out of scope: compile-time customization flags (image overrides etc. come from definition metadata only), serving compiled artifacts from the server persistently, CI integration (v1.3 pack).

Verify: `uv run pytest packages/ngen-weave-cli packages/ngen-weave-server/src/ngen_weave_server/test_compile_api.py packages/ngen-weave-server/src/ngen_weave_server/test_api_contract.py`; manually `uv run ngen-weave compile code_review -o /tmp/dist && head -20 /tmp/dist/code_review/*.yaml` shows a Workflow manifest.

Commits: `feat(cli): compile command`, `feat(server): compile route`, `feat(web): argo export action`

---

### Step 6: v1.1 assembly: docs, changelog, success criteria e2e

`docs/` (distribution guide: compile, deploy worker images, run the watcher, dispatch config), `packages/ngen-weave-server/tests/e2e/test_v11_criteria.py`, changelog entries.

E2E asserts criteria 1–4 in sequence against a kind cluster (live-marked) with a local-mode fallback variant asserting criterion 3 without a cluster. Docs walk: compile canonical example → `kubectl apply` → suspend observed → artifact answered via API → run completes → provenance fetched and compared to a local-mode twin run (record-shape equality, ignoring timestamps/handles).

Out of scope: performance numbers, production Helm charts, Temporal documentation beyond one paragraph naming it as alternative.

Verify: `uv run pytest -m live packages/ngen-weave-server/tests/e2e/test_v11_criteria.py` green with cluster; `uv run pytest -m 'not live'` green without.

Commits: `test(e2e): v1.1 success criteria`, `docs: distribution guide`, `chore: v1.1 changelog`

---

### Step 7: v1.2 groundwork: workspaces

`packages/ngen-weave-core/src/ngen_weave/workspaces.py` (**new**), `alembic/versions/0005_workspaces.py` (**new**), server workspace routes, `ProjectScope` widened, web workspace switcher.

Migration `0003`:

- `workspaces`: `name` str PK, `created_at` timestamptz.
- `workspace_members`: `workspace` FK workspaces.name, `user_name` FK users.name, `role` in {owner, reviewer, observer}; PK `(workspace, user_name)`.
- `projects` table gains nullable `workspace` FK column (null = ungrouped, today's behavior preserved).

Core (`workspaces.py`):

```python
@dataclass(frozen=True)
class WorkspaceScope:
    workspace: str | None          # None preserves plain-project behavior everywhere
    # role resolution: project role wins, else workspace member role, else none

async def resolve_workspace_role(store: WorkspaceRepo, principal: Principal, project_scope: ProjectScope) -> str | None: ...
```

`WorkspaceRepo` protocol (`list`, `members`, `add_member`, `set_project_workspace`, all `async`) implemented for Postgres in this step; SQLite gets table parity via the migration test's `create_all` path so unit tests run storeless as usual.

Server routes on the existing versioned mount: `POST/GET /api/v1/workspaces`, `POST /api/v1/workspaces/{ws}/members`, `PATCH /api/v1/projects/{project}` gaining `{workspace: str | null}`. Enforcement: `resolve_workspace_role` composes into the existing `ProjectScope` dependency exactly as planned in `implementation/v1.0.md` Step 1's absorption note — scope grows a field, no route signature changes.

Web: workspace switcher beside the project switcher; switcher selection threads into the existing client base-path handling (query param, no route rewrite).

Out of scope: nested workspaces/organizations, per-workspace budgets, invitations by email, SSO grouping, migrating the v0.8 one-role-per-user row model (that table stays as-is; workspace membership is additive).

Absorbs: organization-level grouping later extends `WorkspaceScope` with a parent field; role-matrix changes stay confined to `auth.py`/`workspaces.py` resolvers.

Verify: `uv run pytest src/ngen_weave/test_workspaces.py src/ngen_weave/db/test_migrations.py packages/ngen-weave-server/src/ngen_weave_server/test_projects_api.py` (extended); web `pnpm test`.

Commits: `feat(db): workspaces migration 0003`, `feat(core): workspace scopes and role composition`, `feat(server): workspace routes`, `feat(web): workspace switcher`

---

### Step 8: Ticket queues

`packages/ngen_weave/tickets.py` (extended), `alembic/versions/0006_ticket_queues.py` (**new**), server queue routes, web queue view.

Migration `0006`: `tickets` gains columns `assignee` str null, `queue` str null (defaults derived below), `picked_up_at` timestamptz null. Existing rows migrate with nulls; no backfill script needed since tickets were run-local lists before v0.8's table.

Semantics, decided here:

- Queue derivation: a ticket's queue defaults to the creating node's required role (`role` column already present); authors may override via ticket metadata `queue: str` in the serialized definition.
- Assignment: `POST /api/v1/runs/{run_id}/tickets/{ticket_id}/assign {"assignee": str|null}` gated to owners and reviewers; assigning sets `assignee`, clearing `picked_up_at`.
- Queues: `GET /api/v1/queues?role=reviewer&workspace=W` returns open tickets across the workspace's projects grouped by queue, ordered by age; a member sees only queues their role can act on. Pickup: `POST .../tickets/{id}/pickup` sets `picked_up_at` and assignee atomically (DB unique guard: pickup fails with 409 if already picked up and not completed).
- Closing flow unchanged from v0.5; closure clears assignee state.

Store: `TicketStore` methods extended (`assign`, `pickup`, `list_open(queue_filter)`) on both backends per the async protocols from v0.8 Step 5.

Web: "My queue" page listing assigned and claimable tickets, linking into run detail; component tests for the 409 pickup case.

Out of scope: SLAs, escalation, external tracker sync (still deferred per v0.5 non-goals), drag-and-drop triage boards, notifications on assignment (Step 10 covers group routing generally).

Absorbs: new queue dimensions (per-team, priority) land as filter fields on `list_open` plus columns following the JSONB-verbatim policy where possible; the pickup atomicity rule sits in one store method.

Verify: `uv run pytest src/ngen_weave/tickets.py src/ngen_weave/db/test_migrations.py` plus server queue-route tests; two-client 409 covered in `test_queues_api.py`.

Commits: `feat(db): ticket queue columns migration 0006`, `feat(core): queue assignment and pickup`, `feat(server): queue routes`, `feat(web): my-queue view`

---

### Step 9: Concurrent review sessions

`packages/ngen-weave-core/src/ngen_weave/review_sessions.py` (**new**), server `review_sessions` router (**new**), web presence component.

Contract:

```python
# review_sessions.py
@dataclass
class SessionMember:
    user: str
    joined_at: str
    last_seen: str                 # heartbeat, seconds resolution

@dataclass
class ReviewSession:
    artifact_id: str
    members: list[SessionMember]
    draft_slots: dict[str, dict[str, str]]   # slot_name -> user -> latest draft value

def apply_submission(session: ReviewSession, user: str, slots: dict[str, str]) -> ReviewSession:
    """Merge one user's submitted slots. Last-write-wins per slot per user;
    distinct users writing the same slot keep both, surfaced as a conflict list."""

@dataclass(frozen=True)
class MergeResult:
    merged: dict[str, str]         # final artifact response payload candidates
    conflicts: list[str]           # slot names claimed by >1 user
```

Rules, fixed here:

- Sessions exist server-side only, keyed by artifact id, held in memory on the server process with a `RunRepo`-persisted shadow (JSONB on the waiting record) so restarts lose only drafts, never submissions.
- Presence: clients connect to `GET /api/v1/runs/{run_id}/reviews/{artifact}/session` (SSE stream: member join/leave heartbeats, draft updates, submission events). Heartbeat timeout is 30s.
- Submission: `POST .../session/submit {"slots": {...}}` applies `apply_submission`. Completion still fires only through the existing v0.5 multi-reviewer completion check (`output_type.model_validate(response)` per reviewer); the session layer feeds each reviewer's candidate response into that unchanged path. Conflicting slots block auto-completion and surface in the artifact UI for explicit resolution by an owner.
- Roles: observers join read-only (SSE yes, submit 403), matching the existing matrix in `auth.py`.

Web: presence avatars, per-slot draft editing broadcast over the session stream, conflict banner on submission conflicts. Playwright two-context spec drives criterion 6.

Out of scope: operational-transform-grade draft merging (drafts are convenience; submissions are truth), review sessions on non-human nodes, mobile push, recording drafts into provenance (only submissions land in records, as today).

Absorbs: richer co-review mechanics (locking, approval gestures) extend `apply_submission` and the event stream inside one module; the completion/resume pipeline never learns about sessions.

Verify: `uv run pytest src/ngen_weave/test_review_sessions.py packages/ngen-weave-server/src/ngen_weave_server/test_review_sessions_api.py`; web `pnpm test` and the two-context Playwright spec.

Commits: `feat(core): review session merge semantics`, `feat(server): session stream and submit routes`, `feat(web): live review presence`

---

### Step 10: Notification routing to workspace groups

`packages/ngen_weave/notify.py` (rule target widening), provider plugins untouched, tests extended.

Change, one place by design (the dispatcher's rule parser): a rule's `target` field accepts, alongside the existing user-address forms, the group forms `"workspace:<ws>:<role>"` and `"project:<p>:<role>"`. At fire time the dispatcher expands the group through `resolve_workspace_role`/project-role lookups into concrete recipients, dedupes per user, and hands each to the existing provider plugins unchanged. Delivery attempt provenance records gain `recipient_group` alongside recipient (additive record payload, allowed by the JSONB policy).

Failure semantics inherit v0.5 rules exactly: best-effort, retried, never run-failing, attempts recorded.

Out of scope: digest/batching, per-user preference UI beyond existing rule config, new providers.

Absorbs: new addressable groups (teams, custom lists) add one expansion arm in the parser; providers never learn about groups.

Verify: `uv run pytest src/ngen_weave/test_notify_groups.py` (fake SMTP asserting one delivery per member, zero on empty group, group recorded in provenance).

Commit: `feat(notify): workspace and project group targets`

---

### Step 11: Collaborative editing: evidence spike and decision doc

`packages/ngen-weave-web/spikes/collab/` (**new**, throwaway), `design/collab-editing.md` (**new**). No product code.

Experiments fixed here, both run, both measured:

1. **CRDT spike**: Yjs `Y.Map` over the definition document model, two Yjs `WebsocketProvider` clients against a y-websockets relay, concurrent edits to the same node's fields and to different nodes, assert convergence and measure op payload size at 100 ops.
2. **OT spike**: server-authoritative op log (insert/update/remove on the same document model), two clients, same scenarios, assert convergence and measure server sequencing latency.

Rubric, applied mechanically, recorded in the doc: (a) convergence correctness on the scripted conflict scenarios is pass/fail first; (b) if both pass, prefer CRDT/Yjs unless measured op overhead exceeds 3× OT's at the 100-op mark; (c) either way the doc states the chosen mechanism, the losing option's results, and the relay boundary (identical for both, see Step 12). The doc also fixes the document model: the definition format mapped field-for-field into the mechanism's document type, with the mapping owned by one TS module `definitionDoc.ts` in ngen-weave-web.

Out of scope: shipping any of the spike code into the app, offline editing, presence cursors (nice-to-have listed, explicitly deferred), history/time-travel UI.

Verify: `cd ../ngen-weave-web && pnpm spikes/collab/*.test.ts` both suites green; `design/collab-editing.md` contains a "Decision" section naming one mechanism with the rubric outcome.

Commit: `docs(design): collaborative editing decision with spike evidence`

---

### Step 12: Collaborative editing implementation

`../ngen-weave-web/src/lib/collab/` (**new**), server `collab` router (**new**), `DefinitionStore` subscriber hook, editor integration.

Boundary, identical regardless of Step 11's winner:

- Transport: WebSocket `/api/v1/collab/{workflow_name}`, auth via the existing bearer token on upgrade, roles enforced at upgrade time (owner may edit, reviewer/observer read-only).
- Relay duty: the server forwards ops between connected clients and applies the chosen mechanism's convergence procedure; it holds no business logic (PRD web-API rule).
- Persistence: the relay commits snapshots on quiescence (no ops for 5s) and on disconnect of the last editor, calling `DefinitionStore.save(text)` with the definition-format serialization produced by `definitionDoc.ts` inverted; `definition_hash` updates accordingly, and stale-manifest startup checks keep working unchanged.
- Validation: saved snapshots pass through the existing definition validator before commit; an invalid transient state commits nothing and the relay broadcasts a validation-failed notice instead. Editors can therefore produce garbage mid-flight but never persist it.
- Concurrency with runs: a definition being edited while a run compiles from it reads the last committed snapshot; no locking, documented in `design/collab-editing.md`.

Implementation step order within the commit series: transport + relay skeleton with echo-only mode behind a flag; mechanism integration per the design doc; persistence + validation; editor wiring (two-pane conflict-free editing, connection status badge).

Playwright two-context specs cover criterion 7: concurrent field edits converge, disconnect/reconnect converges, persisted snapshot loads in a fresh session and launches a valid run.

Out of scope: cursors/presence (deferred by Step 11 doc), offline mode, editing composite structure graphically beyond what the v0.4 editor already does (this feature adds simultaneity, not new editing powers), version branching.

Absorbs: swapping the mechanism later touches `lib/collab/transport.ts` internals plus the relay's convergence call; editor features, validation, and persistence subscribe to snapshots and never see mechanism specifics.

Verify: `pnpm lint && pnpm test && pnpm build` in the web repo; `uv run pytest packages/ngen-weave-server/src/ngen_weave_server/test_collab_relay.py`.

Commits: `feat(web): collab transport and definition document mapping`, `feat(server): collab relay with validated snapshot persistence`, `feat(web): editor live collaboration`, `test(e2e): two-editor convergence`

---

### Step 13: v1.2 assembly

`packages/ngen-weave-server/tests/e2e/test_v12_criteria.py`, docs (collaboration guide covering workspaces, queues, sessions, co-editing), changelog, web polish pass.

E2E chains criteria 5–8: workspace-isolated access matrix, dual-session review with conflicting and non-conflicting submissions, dual-editor convergence with persisted launch, group notification deliveries counted.

Out of scope: admin dashboard, audit log UI, per-workspace settings pages.

Verify: `uv run pytest -m 'not live' && uv run pytest packages/ngen-weave-server/tests/e2e/test_v12_criteria.py` (server-mode e2e needs only Postgres + fake SMTP, not live-marked); web full suite.

Commits: `test(e2e): v1.2 success criteria`, `docs: collaboration guide`, `chore: v1.2 changelog`

---

### Step 14: Pack seam verification and pack conventions doc

`docs/packs.md` (**new**), conformance harness `packages/ngen-weave-core/tests/test_pack_seams.py` (**new**). No pack code yet.

Purpose: prove the v0.5 seams suffice before building on them, so any gap is fixed here as a core bug rather than discovered mid-pack. The harness installs a minimal fixture pack (already exists as the v0.5 demo plugin; extend if needed) and asserts a pack can, using only published interfaces: register workflows and node kinds with UI metadata, register services through service resolution, declare capability grants in project config, tag provenance with its plugin id, and expose workflows through MCP. Any assertion that fails becomes a fix commit in this step with a note in `docs/packs.md`.

Conventions doc fixes the pack layout every pack follows:

```
packs/ngen-weave-pack-<vertical>/
├── pyproject.toml              # entry points: ngen-weave.plugins (workflows, kinds, services),
│                               #   optionally ngen-weave.compile-targets / dispatch-backends
├── src/ngen_weave_pack_<vertical>/
│   ├── __init__.py             # plugin entry: registration functions only
│   ├── workflows/              # Workflow subclasses
│   ├── kinds/                  # node subclasses + NodeMeta + renderer registrations
│   └── services/
└── tests/
```

Out of scope: pack versioning policy beyond ordinary semver of the pack package, marketplace/discovery infra (non-goal), any core change not forced by a failed seam assertion.

Verify: `uv run pytest packages/ngen-weave-core/tests/test_pack_seams.py` green; if it was green with zero core fixes, that is itself the deliverable.

Commits: `test(core): pack seam conformance harness` (+ any `fix(core):` commits it forces)

---

### Step 15: Coding IDE pack

`packs/ngen-weave-pack-coding/` (**new**, layout per Step 14). Core receives zero changes unless Step 14's harness lied; if it did, stop and fix the seam in core first, per the release-doc rule.

Contents, fixed here:

- **Code-graph artifact type**: a node kind `code_graph` (registered via `kinds/`) producing a versioned artifact holding a repo dependency graph (tree-sitter-based extraction, `tree-sitter` + language grammars as pack deps); artifact stored through the ordinary content-addressed VersionLog; UI metadata renders a summary line and links to the artifact blob.
- **Repository-aware review workflows**: two `Workflow` subclasses, `repo_review` (graph extraction → per-file review workers fanned out → gate on severity → human review) and `pr_review` (same, seeded from a diff instead of the whole graph). Inputs are pydantic models (`RepoRef{url, ref}, DiffRef{...}`); strictness comes free from core validation.
- **CI hooks**: server route additions belong to the pack's own FastAPI router mounted by the server's plugin-router hook (if the v0.5 plugin surface lacks router mounting, this is a Step 14 seam fix, made there): `POST /api/v1/hooks/forgejo` and `POST /api/v1/hooks/github`, verifying webhook signatures from project config, mapping PR events to `pr_review` launches with `DiffRef` inputs, and posting findings back as PR comments through the respective API using a token from project config. Findings formatting is one function per provider in the pack.
- Budget defaults per launch pulled from project budget config; PermissionSets box any AgentNodes used in review workflows (autonomy rules apply to packs identically).

Live-marked e2e against a Forgejo fixture instance exercises criterion 10; unit tests cover webhook parsing/signature rejection/comment shaping with recorded HTTP fixtures.

Out of scope: GitHub App installation flow (raw webhook + token config only), other forges, monorepo path filtering UI, IDE editor integrations (the pack serves workflows and artifacts; editors consume them), publishing the pack to PyPI (installable from the monorepo path is enough for this release).

Verify: `uv run pytest packs/ngen-weave-pack-coding -q` green; `uv pip install -e packs/ngen-weave-pack-coding && uv run pytest packages/ngen-weave-server/tests/e2e/test_v13_criteria.py -k coding` (with Forgejo fixture up) passes criterion 10; uninstalling and re-running the server shows no pack traces.

Commits: `feat(pack-coding): scaffold and code-graph kind`, `feat(pack-coding): review workflows`, `feat(pack-coding): forgejo and github hooks`, `test(e2e): pr-triggered review`

---

### Step 16: Research pack

`packs/ngen-weave-pack-research/` (**new**), same layout and rules as Step 15.

Contents, fixed here:

- **Search provider service**: pack defines and registers a `SearchProvider` protocol (`async def search(self, query: str, limit: int) -> list[SearchHit]`, `SearchHit{title, authors, year, doi, abstract, source}`) through service resolution, with two implementations: `OpenAlexProvider` and `SemanticScholarProvider` (both polite-rate-limited, keyed by optional env tokens, fakes used in tests). Workflows reference providers by name in config, never by class.
- **Literature-review workflows**: `lit_review` (search → dedupe by DOI → relevance-screen worker → human review of the screen → deep-summarize survivors), citations carried as structured fields on artifacts.
- **Citation-bearing artifacts**: an artifact writer helper in the pack emitting BibTeX and CSL-JSON alongside the markdown summary; artifacts remain ordinary VersionLog entries.
- **Provenance export profile**: a pack-provided transform over PROV-JSON (pure function, applied post-export) annotating records with source DOIs and provider ids, shaped for open-science reproducibility checks; the core exporter stays untouched.

Out of scope: full-text retrieval/paywall handling, citation-graph traversal UI, reference-manager sync (Zotero etc.), writing the actual paper.

Verify: `uv run pytest packs/ngen-weave-pack-research -q` green with provider fakes; `pytest -k research` slice of `test_v13_criteria.py` asserts criterion 11's export annotation.

Commits: `feat(pack-research): search providers`, `feat(pack-research): literature review workflow and citation artifacts`, `feat(pack-research): provenance export profile`

---

### Step 17: Release closeout

Changelog entries for v1.1–v1.3, docs index updates (distribution, collaboration, packs guides linked from the MkDocs nav), version bumps across packages, `plans/releases/post-1.0.md` checkboxes reviewed against the success-criteria e2e names.

Final verification sweep: `uv run pytest -m 'not live' && uv run ruff check .`; live suites (`argodespatch`, Postgres, Forgejo fixture) where environments exist; web `pnpm lint && pnpm test && pnpm build`; fresh-clone smoke: install server + both packs, compose up, compile `repo_review`, launch against a fixture repo, answer review from a second token, export PROV-JSON with research-profile annotations absent (they are pack-scoped) but run JSON byte-compatible with the v0.6 writer.

Commits: `chore: v1.3 changelog and version bumps`, `docs: nav and index updates`

---

## Amendments to earlier plans

1. **Serialized definition metadata** (v0.4 format): gains optional per-node `retry: int` and `priority: int | null` (Step 4). Additive under the coverage-not-breaking rule; older definitions stay valid.
2. **Runner leaf invocation** (v0.1, async since v0.6 Step 2): the single inline leaf-execution call site becomes `await backend.dispatch(activation)` (Step 4); local backend reproduces today's behavior exactly.
3. **`tickets` table** (v0.8 Step 5, migration `0003_tickets`): gains `assignee`, `queue`, `picked_up_at` in migration 0006 (Step 8); existing columns untouched.
4. **`projects` table** (v1.0): gains nullable `workspace` FK in migration 0003 (Step 7).
5. **Plugin surface** (v0.5 Steps 2–4): if the conformance harness (Step 14) finds router-mounting or renderer-registration gaps, fixes land as `fix(core)` commits in Step 14 and are recorded here; no speculative widening before evidence.
6. **Entry-point groups** (v0.5 pattern): two new groups, `ngen-weave.compile-targets` and `ngen-weave.dispatch-backends`, loaded by the same machinery (Steps 2, 4).
7. **No semantic changes** to validation, provenance emission, artifact addressing, budget enforcement, or the auth matrix anywhere in this plan. If a step appears to require one, stop: the abstraction list above exists so variation lands at the edges.

## Definition of done

All eleven success criteria demonstrably pass through their named e2e files. The local engine path is byte-for-byte behaviorally unchanged when `dispatch.backend` is unset (full pre-existing suite green is the proof). Both packs install and uninstall cleanly through pip and project-config enablement alone, and the pack conformance harness passes with no pending core fixes. Web lint/unit/build green. `design/distribution.md` and `design/collab-editing.md` carry the decisions this plan references rather than restates, per the design-doc rule.
