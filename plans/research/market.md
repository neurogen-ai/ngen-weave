# Research: pi-subagents, the durable-execution field, and where ngen-weave separates

Status: research input to the decision log, not an implementation plan. Companion to
`plans/product/PRD.md`; where the two conflict, the PRD wins until this research is
adopted into a release doc.

Sources, all current as of 2026-08-26:

- pi-subagents v0.57, installed locally at `~/.pi/agent/npm/node_modules/pi-subagents/`
  (README, docs/workflows.md, docs/observability.md, docs/watchdog.md, docs/missions.md,
  docs/extension-api.md, docs/configuration.md).
- pi-subagents public signals: 3.3k GitHub stars, 330k npm downloads/month, near-daily
  releases, MIT license. Host runtime: the pi coding agent (badlogic/pi-mono, ~97k
  stars).
- ngen-weave v0.1.2 codebase and `plans/` as read by recon (`plans/product/PRD.md` is
  the authoritative product statement).
- A four-way assessment in this repo's session history: recon scout, pi-subagents
  direct reading, web checks (npm API, GitHub API, HN Algolia), and an oracle PMF
  review. This doc is the follow-up the user asked for, written in detail.

## 1. What pi-subagents is, and why we care

pi-subagents is a TypeScript extension for the pi coding agent. It gives the parent pi
session a `subagent` tool: spawn child pi sessions as focused agents, run them in
parallel, script them from sandboxed JavaScript, review them with a watchdog, track
them in a fleet view, and wrap their runs in durable mission records with schedules.
Its adult features are the same shapes ngen-weave wants: multi-step AI pipelines,
parallelism, budgets, durable run records, and a human somewhere in the loop.

The similarity we noticed is real at the concept level and shallow at the runtime
level. pi-subagents composes live agent sessions; control flow lives in JavaScript
strings or parent-model improvisation; its human watches a TUI and steers. ngen-weave
composes typed steps with pydantic boundaries, control flow compiled at import time;
its human is a node that halts the graph and emits an artifact whose contents decide
routing. Those are different products that share territory.

The oracle verdict from the earlier assessment stands: do not vendor pi-subagents as
a sub-module. A TypeScript extension bound to pi's runtime cannot be imported from
Python. It ships near-daily, so any pinned copy goes stale fast. Vendoring it also
reads as appropriation of a healthy independent project for zero executable benefit.
What we do steal is architecture. This document specifies that at the level of
mechanisms, so the steal is concrete rather than vibes.

## 2. What we copy from pi-subagents

Each item names the mechanism in pi-subagents, what we take, and which roadmap slot
it feeds. The order is roughly priority. A summary table sits at the end.

### 2.1 Contract preflight: resolve without executing

pi-subagents exposes `resolveSubagentLaunchContract`: a side-effect-free resolution
of the full child launch contract before anything runs. It returns the agent
identity, a digest of the effective system prompt plus model, tools, skills, output
binding, and schema, and the effective tool allowlist. Failures are stable enum codes
(`missing_agent`, `denied_required_tool`, `invalid_cwd`, and friends), not prose.
Facts only the host can prove come back as `host_required` diagnostics instead of
guessed values. It never creates sessions, temp files, or run artifacts.

What we take: ngen-weave already dry-run compiles graphs at import time and freezes
the per-node model variant table at compile time. The gap is exposing that resolution
as a callable service. The v0.2 RunService should expose a validate/resolve endpoint
that returns the frozen contract for a workflow: resolved schemas, frozen model
variants, config bindings, permission requirements, all without launching. The MCP
server should register tools from that same resolution, so every tool description is
mechanically generated from the compiled graph and cannot drift from it. That turns
"usage is validated" from a CLI check into a property of the API surface.

Where it lands: v0.2 RunService and MCP server. Adopt the stable error-enum style for
the HTTP API from day one; prose messages drift, codes do not.

### 2.2 Logical identity separate from attempt identity

The structured delegation API identifies one logical node as `ownerRunId` plus
`nodeId`, and marks each attempt with a separate `requestId`. A second active attempt
for the same logical node receives `duplicate_node` without disturbing the original.
Cancellation targets the exact tuple, including cancel-before-start races. Each
attempt emits exactly one terminal response.

What we take: ngen-weave already namespaces retry attempts, but the distinction
between "this node, logically" and "this attempt" is implicit in the engine rather
than explicit in the store. The v0.2 run/thread API should make `node_path` the
logical identity and the attempt namespace the request identity, with duplicate
submissions rejected by that tuple. This matters most for remote resume: an MCP
client retrying a human submission must either hit the same node or get a
`duplicate_node` answer, never a double-execution.

Where it lands: v0.2 run/thread API, with implications for remote dispatch at v1.1.

### 2.3 Lifecycle artifact discipline

Async runs in pi-subagents write four files per run: `status.json` (point-in-time
snapshot, authoritative for recovery), `events.jsonl` (append-only lifecycle events,
versioned, unknown fields ignored by consumers), a bounded live log tail, and a
terminal proof file. Consumers are told to read these files, never to scrape
terminal output. The big rules: status snapshots drive recovery, events are hints;
event envelopes carry a version and consumers must ignore unknown fields; `endedAt`
and result-file existence never count as proof the process exited (there is an
explicit `process-terminal` proof with `observed`/`unknown` states); oversize output
fails per protocol bounds (16 MiB protocol lines, 128 KiB retained stderr).

What we take: this is the store design ngen-weave already half-planned. v0.2 moves
the run ledger to SQLite append, but the discipline is the copy: a versioned event
envelope so readers ignore unknown fields, a separate authoritative status snapshot
(the RunFile already plays this role) that is what any resume UI reads, and a
distinct notion of "the process that ran this is terminal" separate from "the run
completed". For a server product the last one is a real question: a crashed worker
holding a checkpoint must not block resume forever, and "failed" must not be
inferred from a missing heartbeat alone.

Where it lands: v0.2 SQLite store, v0.3 UI reads. Take the concrete byte bounds into
`constants.py` rather than leaving truncation unspecified.

### 2.4 Steering receipts: accepted is not followed

When a workflow steers a running child, pi-subagents returns a receipt with one of
`queued`, `delivered`, `missed`, or `failed`. `delivered` means the child session
accepted the input, explicitly not that the model followed it. `missed` means the
child went terminal or lost its route before delivery; the script decides whether to
continue. Receipts are single-sided: there is no callback API, no inbox access.

What we take: the human node resume path has the same two-step shape. Accepting a
submitted artifact into the store is not the same as the graph having processed it.
The run ledger should record payload delivery state separately from node execution,
so a UI can tell a reviewer "your decision was accepted" before the backend finished
replaying. It also legitimizes the `missed` semantics for remote resume: a resume
targeting a completed run must say so instead of pretending to queue.

Where it lands: v0.2 resume path and the v0.4 review UI.

### 2.5 Missions: durable records with strict self-restraint

Missions wrap runs as recovery records. The rules are the interesting part:

- The ledger only records. It never schedules, restarts, or replans work by itself.
- Durable `state.get`/`state.set` for workflows: one writer at a time via a file
  lock, atomic merge-and-write, hard size cap.
- Persistence failures never block the run; they surface as a warning field.
- Goal missions drive continuation: after each idle turn a notice names the next
  ready action (read from state, from an open decision, or from a linked run) and
  the remaining token budget. Reaching the budget produces `budget-exhausted`, a
  terminal state distinct from success, and stops the notices.
- Receipts are durable links to external outcomes (PR, CI, deploy) recording
  delivery state only. They do not merge, poll, or deploy.
- Closing a mission records a terminal status and a summary.

What we take: the ledger-never-launches rule is exactly the line between ngen-weave
and a supervisor that oversteps. v0.2 observers start at pause only; this research
makes that discipline explicit at the store level: provenance records and run state
are evidence, the only launcher is the engine and (later) the scheduler. The
`budget-exhausted` terminal state should join the RunStatus enum for v0.2 budget
enforcement, so a budget-capped run lands in a distinct state rather than failing
ambiguously. The state file pattern (single-writer lock, atomic merge, cap) is the
right mechanical model for our own per-run durable state when AgentNode arrives.

Where it lands: v0.2 budgets and observers; the state file pattern for v0.2 AgentNode,
harder forms at v0.5.

### 2.6 Schedules: external launcher, no daemon

Schedules store per project, append-only events, mode 0600. `overlap` is fixed to
`skip`, `catchUp` supports `latest` or `none`. The notable mechanism is
`schedule.run-due`: an external cron job triggers due work through an endpoint, so
the package never needs to be a daemon.

What we take: ngen-weave's scheduling story is thin (nothing before 1.x at best),
and the run-due pattern is the right server shape: a thin scheduler that owns the
"what is due" calculation plus an API the OS cron or a container can hit. No
persistent scheduler process, no heartbeats to babysit. The overlap/catchUp
vocabulary is stable and should be reused verbatim when the scheduler appears so
users coming from other systems recognize it.

Where it lands: post-1.0 or 1.x scheduler; nothing in v0.1-v0.2 needs it. Keep it
out of v0.2; scheduling is not the gap we must close first.

### 2.7 Watchdog and permission gates: adversarial review at safe boundaries

The watchdog is an adversarial change reviewer, distinct from the reviewer agent.
It runs at the safe `agent_end` boundary, only when the final repo state changed
since the start of the turn; multiple edits coalesce into one review of the final
state; reverted diffs are skipped; generated artifacts do not trigger it. Scoped
child permissions are `allow`/`ask`/`deny` per tool, where `ask` pauses that exact
call and sends a bounded, redacted preview to an arbiter model that returns only
approve or deny, with decisions audited. The watchdog model is chosen to be strong
and complementary to the session model, not cheap.

What we take: three mechanisms map directly onto the v0.2 observer design and the
v0.5 PermissionSets. Review coalescing: an observer should review the state a node
left behind, not every transition, and should skip runs where nothing changed.
The predicate idea stays, but the "one review of the final state" cadence comes from
here. The `allow`/`ask`/`deny` vocabulary plus a gate that calls an arbiter model is
the right shape for PermissionSets, with one deliberate divergence: pi-subagents
gates live tool calls in an interactive session, ngen-weave gates autonomy engine-
side at v0.5, so the arbiter lands as a Control-like node evaluated at the engine
boundary, not a tool hook. Audit on every gate decision, bounded, with the decision
source recorded. That is provenance by default applied to policy.

One thing we refuse to copy: pi-subagents passes `bash` through ungated. Our
PermissionSets gate every tool including shell.

Where it lands: v0.2 observers (coalescing cadence, complementary-model guidance
for observer model choice), v0.5 PermissionSets (rule vocabulary, arbiter pattern,
audit).

### 2.8 Capability ceilings: monotonic, audited, honest

Out-of-band ceilings intersect `allowedAgents` and `allowedTools` and OR
`denyExtensions` across registrations. An empty list means nothing is allowed for
that field. Ceilings propagate monotonically to nested and async children and are
retained for recovery. Restricted agents stay visible in listings, clearly marked
non-executable, rather than silently hidden. The resolved policy surfaces bounded
audit counts and sources, never full extension paths.

What we take: the propagation semantics are the design for budget scopes. A
composite's budget cap must flow to every leaf in its subtree, tightest wins, and a
denied capability must be visible in status output with a reason, never silent.
These two rules, monotonicity and honesty, are cheap to state now and expensive to
retrofit: record them in the v0.2 budget design doc.

Where it lands: v0.2 budget enforcement; reused for PermissionSets at v0.5.

### 2.9 Fleet observability: compact summary, authoritative snapshots

FleetView is a persistent compact line (active count, spend) that expands to a tree
of children with agent, state, elapsed, and token usage. Nested runs appear under
their parent in the tree. Costs are reported two ways: `window` (latest turn input
plus cache reads) and `spent` (cumulative total). Status snapshots stay
authoritative for recovery; events are hints.

What we take: the tree shape for the v0.3 read UI, and the `window` versus `spent`
distinction for provenance metadata. ngen-weave's six-field metadata records tokens
in context and cumulative totals per scope already; naming them `window` and `spent`
in the UI and docs avoids the confusion pi-subagents hit. The rule that the read UI
renders from snapshots, not the event stream, keeps the UI simple and the store free
to append.

Where it lands: v0.3 UI; the window/spent naming can land in v0.2 metadata docs
immediately.

### 2.10 What we do not copy from pi-subagents

- The workflowScript sandbox. Model-authored JavaScript orchestration is the right
  answer for a live agent host and the wrong answer for a durable graph engine. Our
  graph is the artifact, and it must be reviewable, import-time compiled, and free
  of model improvisation in structure. We keep Python classes.
- Dynamic fanout. pi-subagents bounds fanout through structured output lists with
  explicit `maxItems`. We adopt the bound, not the mechanism; our fan-in is
  statically compiled.
- Prompt-template frontmatter as a workflow language (`/prompt-workflow`). Our YAML
  config layer already covers data-only configuration; workflow packs at v0.5 are
  the place to revisit this idea, and they will be Python, not frontmatter.
- The churn treadmill. Near-daily releases serve a solo maintainer's live users; we
  version against a release schedule and keep the API semver'd from 1.0.

### 2.11 Summary table

| Mechanism in pi-subagents | What we take | Roadmap slot |
|---|---|---|
| Contract preflight, stable error codes | Resolve-without-execute as a service; MCP tools from compiled graphs | v0.2 |
| ownerRunId/nodeId vs requestId | Logical node identity in run/thread API; duplicate rejection | v0.2 |
| status.json + events.jsonl discipline | Versioned event envelope; snapshot authoritative; terminal proof | v0.2 store, v0.3 UI |
| Steering receipts | Delivery state separate from processing, incl. `missed` | v0.2 resume, v0.4 UI |
| Mission ledger never launches | Observers pause-only at the store level; `budget-exhausted` status | v0.2 |
| State file lock + atomic merge + cap | Per-node durable state pattern | v0.2/v0.5 AgentNode |
| run-due external launcher | Thin scheduler with external trigger | 1.x |
| Watchdog coalescing + arbiter gate | Observer cadence; allow/ask/deny vocabulary | v0.2, v0.5 |
| Capability ceiling monotonic + honest | Budget scope propagation rules | v0.2 |
| FleetView tree, window vs spent | v0.3 UI shape; metadata naming | v0.2 docs, v0.3 |
| workflowScript sandbox | Explicitly not copied | never |
| Prompt frontmatter workflows | Revisit as Python workflow packs | v0.5 |

## 3. The Temporal, Conductor, Airflow comparison in detail

The PRD claims a gap above these systems: queuing, scheduling, sharding, and failure
domains are owned and solved; none of them offer typed node contracts, human review
as a structured artifact, cost policy per node, or workflows that nest like
components. That claim deserves pressure, because LangGraph already provides two of
the four pillars ngen-weave leans on, and the PRD's own decision log uses them.

### 3.1 What the incumbents actually own

Temporal owns the durable-execution model: workflow code runs as an event-sourced
history with deterministic replay, signals and sleeps suspend long-running work, and
activities carry retries, timeouts, and heartbeats. It is the strongest answer to
"a process keeps running even when the machine dies." Its human step is a Signal
that interrupts and a workflow waiting on it. It has no opinion about typing between
steps, no default cost accounting, no artifact store, and no provenance beyond
history you must query for.

Conductor owns declarative task graphs: JSON task definitions, worker pools, wait
tasks for humans, retries and rate limits. It is the closest declarative analog to
what ngen-weave draws. Its input/output maps are untyped by default; human tasks
wait on external completion without a structured artifact; cost is an external
conversation; wiring is JSON, which is why the ngen-weave decision log rejected
hand-authored YAML graph files in favor of Python primitives.

Airflow owns scheduling and DAG semantics at batch scale. Its xcoms are untyped by
default, human steps are external sensors or short-lived operators, provenance is
DAG-run metadata rather than per-task linear provenance with content hashes, and
cost accounting is external. It is the right tool for scheduled batch pipelines,
which is precisely the tool researchers already have in their labs.

LangGraph, the engine under ngen-weave, already contributes checkpoint-resume and
interrupt-based human-in-the-loop. So the durable-execution ground is jointly
claimed: Temporal owns it at the process level, LangGraph at the graph level, and
the marketing words "durable" and "human-in-the-loop" are contested everywhere.
ngen-weave cannot win on the durability story alone, and the PRD's language should
stop treating that as the moat.

### 3.2 Where the gap genuinely is

What none of the four offer, in one list:

- Typed contracts enforced by the engine at every boundary. Temporal's determinism
  is a runtime constraint on workflow code, not a type between steps. Conductor's
  maps are untyped. LangGraph state is a schema, but the schema is the whole graph
  state, not per-node input/output contracts validated independently.
- Human review as a first-class typed artifact. None of them model an internal
  state model, prefilled from context, validated on submission, whose verdict
  statically decides routing. Temporal signals and Conductor wait tasks punt the
  artifact to whatever the caller wrote. This is ngen-weave's sharpest feature and
  the hardest to copy because it requires the whole stack: pydantic models,
  artifact store, resume semantics, and routing declared in build().
- Default-on provenance with per-scope metadata and content-addressed artifacts.
  Temporal history is a trace, not a provenance record with cost, tokens, and
  validity per activation. Airflow metadata is DAG-run level. Nobody else emits
  provenance without opt-in and links artifact hashes to the producing activation
  and its input hashes.
- Workflows that nest like components, with per-subgraph checkpoints and interrupts
  that propagate up, identity being the class path. Temporal nests by composing
  workflows in code; Conductor nests by JSON references; neither gives you a
  composite that is itself a node with the same validation, cost binding, and
  provenance treatment as a leaf.
- Per-node cost policy in the config layer, frozen at compile time so a resumed run
  resolves identically. Nobody else freezes model binding into the compiled graph.

### 3.3 The honest positioning

Temporal ensures the process runs. ngen-weave models what the process is, with
types, artifacts, and provenance. That division is more useful than "a gap above
Temporal": it tells adopters exactly where the boundary sits. A shop that needs
indestructible infrastructure keeps Temporal and drives ngen-weave activities from
it, or not, once remote dispatch arrives. Nothing in v0.1-v0.5 needs Temporal; the
engine already checkpoints.

The competitive set ngen-weave should watch is not Temporal and Airflow at all. It
is LangGraph Platform (checkpointing plus hosting plus LangSmith observability),
Conductor OSS with typing bolted on by users, Restate and Inngest positioning
themselves for agent workloads, and on the research side the incumbents Snakemake
and Nextflow, which are adequate for most lab pipelines and free. The research fit
only wins where a human gate sits mid-pipeline with validated routing and where
provenance needs to be reproducible rather than merely stored. That is a real niche
and a narrow one. The PRD's research pack is last on the roadmap for a reason.

## 4. What to focus on to differentiate

Priority order, each paired with the roadmap slot that owns it.

1. Human artifact semantics. The internal `state_type`, verdict-driven routing,
   prefill from context, transform on submit, resume-until-submitted model is the
   one thing nobody else has. Double down: artifact diffs at v0.4, multi-reviewer
   at v0.8, remote review through the API, and the delivery-state discipline from
   section 2.4 so reviewers get trustworthy feedback. Keep the flat-slot model until
   a real workflow demands nested slots; the decision log already said this once and
   the discipline still holds.

2. Provenance by default with an export path. The six-field metadata per scope, the
   content-addressed artifact store with input hashes, unconditional emission: these
   are the reproducibility story and the academic wedge. Publish the record format
   as a documented, versioned thing early, PROV-JSON at v0.4, so the format exists
   before the UI needs to render it. Format first, visualization later.

3. The compiler story. Dry-run compilation, AST lint, frozen variant tables, and
   now contract resolution as a service (2.1) are the equivalent of a type system
   for workflows. Make validation the obvious entry point: `ngen-weave validate`
   already prints the resolution table; the v0.2 API should return the same fact
   set to any caller, including MCP clients.

4. Supervision with pause-only, review-coalesced observers (2.7). Structured
   predicate builders with machine-generated descriptions, one review of each
   node's final state, arbiter-gated asks only where autonomy makes them
   necessary (v0.5). This keeps the v0.1 promise that supervision is evidence
   reading, and the evidence keeps coming from provenance records.

5. Budgets as first-class policy with a distinct terminal state. Per-node cost
   policy in config, frozen bindings, `budget-exhausted` in RunStatus from v0.2,
   monotonic scope propagation (2.8). Cost is how a solo researcher or a small lab
   decides to trust unattended runs; it is a feature, not an admin detail.

6. The MCP server as the integration seam. The fastest path to users is making
   ngen-weave workflows callable as tools from pi, pi-subagents, Codex, and
   whatever comes next. That converts the pi-subagents relationship from
   competition into plumbing: their 330k monthly downloads become reach, not a
   benchmark. The same MCP layer is what a pi-subagents agent definition would
   point at, a few lines of frontmatter on the user side, no code coupling.

7. What not to compete on. Session velocity, chat UX, live steering ergonomics,
   template scripting, editor integrations. Those are the agent host's game and
   pi-subagents is winning it daily. The durable backend under any agent host is
   the position that makes the competition irrelevant.

## 5. Decisions this research raises for the roadmap

These are open questions the release docs should settle, roughly in the order v0.2
will hit them:

1. RunStatus enum: add `budget-exhausted` in v0.2 (running, waiting_human, paused,
   completed, failed, budget-exhausted). Confirm nothing in the v0.1 store breaks
   by adding a status value.
2. RunService contract endpoint: does the v0.2 validate/resolve endpoint return the
   same frozen contract the MCP server uses to register tools? It should; that is
   the mechanical drift guard from 2.1.
3. Store event envelope: adopt an explicit versioned envelope for provenance
   records in the v0.2 SQLite store, with unknown-field tolerance for readers.
   Keep the single-file JSON export byte-identical to today's format.
4. Resume delivery state: record payload accepted versus node processed for human
   nodes (2.4), and define the `missed` answer for resume attempts against
   completed runs.
5. Observer cadence: one review per node's final state, skipping unchanged
   transitions (2.7). Settle this when the v0.2 scheduler is planned, along with
   the pause-only rule restated at the store level (2.5).
6. Budget scope propagation: monotonic, tightest-wins, denied capabilities visible
   with reasons in status output (2.8). This one is a design doc in its own right
   before any budget code lands.
7. Scheduler shape: adopt the run-due external-launcher pattern (2.6) when
   scheduling is planned, and no earlier. The overlap/catchUp vocabulary is
   reserved.
8. PermissionSet vocabulary at v0.5: `allow`/`ask`/`deny` with an arbiter node and
   audit records, gating every tool including shell (2.7), applied at the engine
   boundary rather than as a runtime tool hook.
