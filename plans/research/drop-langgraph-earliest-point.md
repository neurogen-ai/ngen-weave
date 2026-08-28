# Dropping langgraph: earliest possible point

Status: research. No decision made here.
Sources current as of 2026-08-28. Repo version 0.2.1, tag v0.2.0 released.

## The question

At what point in the planned release sequence can langgraph be removed while leaving v0.3 and later features and reliability intact?

## Evidence

### What langgraph provides today, so what any drop must replace

The engine is one file: `packages/ngen-weave-core/src/ngen_weave/engine/runner.py`, 1541 lines, 49 references to langgraph APIs. The production graph replays recorded ops onto a `StateGraph` (`runner.py:709`, `runner.py:701` docstring), human nodes park via `interrupt` and `Command` (`runner.py:796`, `877`, `932`, `951`, `1203`), and checkpointing uses `MemorySaver` (`695`, `1498`) and `AsyncSqliteSaver` (`1506`). All imports are lazy. Verified by grep on 2026-08-28.

What must be re-owned at any drop point: durable execution, checkpointing, interrupts, resumability, superstep scheduling, fan-in assembly, and stream consumption. Nothing in plain Python provides these (`plans/research/refactor-lang.md`). That doc also records the reliability incidents that motivate removal: compile cache bugs and a gate bug in the v0.1.1 to v0.1.3 release notes, plus langgraph ignoring caller-supplied top-level checkpoint namespaces, which forced per-level thread ids (`product/PRD.md` decision log, `runner.py:1474`). It states plainly that attribution of those failures (langgraph bug, our integration bug, SQLite concurrency, our semantics) was never resolved.

### How much each planned release touches the engine

Locked versions in `uv.lock` as of the snapshot date: langgraph 1.2.11, langgraph-checkpoint 4.2.0, langgraph-checkpoint-sqlite 3.1.1. The core pin is lower-bound only, `langgraph>=0.2` (`packages/ngen-weave-core/pyproject.toml`).

**v0.3.** The least entangled release. It adds a read-only UI and API, and its implementation doc constrains the work to consume the engine read-only or not at all (`plans/implementation/v0.3.0.md:565`). The read model is pure functions over `Workflow` classes (`v0.3.0.md:181`). One langgraph touchpoint survives outside the engine: import-time validation runs `build()` against a real throwaway `StateGraph` (`plans/product/PRD.md`, sections on composite wiring and validation; `workflow.py:225` imports `StateGraph` for that throwaway pass). A drop at v0.3 start needs an owned validator, or a validation-only langgraph dependency. Unverified: whether the dry-run compile is the only non-engine langgraph call path.

**v0.4.** Small additive engine edits. Budget fields on `RunFile` and the effective-budget check touch `engine/store.py` and `runner.py` (`v0.4.0.md:16`, `487`). The artifact-versioning feature adds one `VersionLog.append` call to the engine's artifact-write hook (`v0.4.0.md:328`). The rest (storage format, editor, diff engine, PROV-JSON export) sits beside the engine and does not require langgraph. The editor writes through validation, so the v0.3 dry-run question carries forward. Unverified: whether editor saves recompile workflows at import time or through a separate path.

**v0.5.** Engine-touching but seam-shaped. `Engine.__init__` gains optional keywords for plugin provenance sources and agent loopback wiring (`v0.5.0.md:236`, `647`, `702`). Boxing enforcement lives in the permission gate wrapped around the executor (`v0.5.0.md:613`, `647`), and per-run-subtree budget caps reuse the existing `_check_budget` (`v0.5.0.md:616`). None of that requires langgraph as such. But v0.5 is the plugin stability release, so an engine swap here happens while third-party node types first arrive.

**v0.6.** The only planned release that grows langgraph coupling. The Postgres backend adds `langgraph-checkpoint-postgres` as a dev dependency and selects `AsyncPostgresSaver` as the checkpointer (`plans/implementation/v0.6.0.md:198`, `220`). Work spent there is thrown away if langgraph is dropped afterward. The drop-before/v0.6-versus-after trade is concrete: dropping first deletes that step; dropping second wastes it. An owned engine also has to decide where in-flight state lives in Postgres, since `PgRunStore` and the checkpointer currently split that job.

**v0.7.** The plan's designated debt slot: consolidation and deprecation flagging before the 1.0 freeze (`plans/releases/v0.7.0.md`). A drop here fits that framing, but the v0.6 Postgres saver work is dead code by then.

**v0.8.** Auth, roles, multi-reviewer human nodes, tickets, and `import-project` (`plans/releases/v0.8.0.md`). Engine-neutral on its face. Unverified: whether multi-reviewer human nodes need interrupt semantics changes beyond today's single-reviewer resume.

**v1.0.** Explicitly closed. The implementation doc puts "touching LangGraph-the-engine" and any behavior change out of scope (`plans/implementation/v1.0.0.md:128`). From 1.0 on, the API is semver-governed and config and definition files valid at 1.0 stay valid forever (`plans/releases/post-1.0.md`). A post-1.0 drop must preserve observable behavior through the published API and keep old runs resumable or ship a migration. That is the most expensive window in the plan.

### Reliability constraints on any drop

- Runs persist in SQLite and resume from stored input through langgraph checkpoints (README design notes). `RunFile` alone cannot reconstruct in-flight state today (`plans/research/refactor-lang.md`). A drop must reproduce the checkpoint format, drain old runs through the langgraph backend, or abandon them.
- No back-compat before 1.0 is the standing stance. A run parked `waiting_human` before an upgrade may fail loudly on resume, and that was accepted (`plans/design/loops-and-joins.md:37`). In one specific sense this lowers the cost of a pre-1.0 drop: old in-flight runs do not have to survive.
- `import-project` at v0.8 migrates runs, provenance records, and artifact manifests into Postgres (`plans/releases/v0.8.0.md`). It does not mention checkpoint state. Unverified: whether pre-v0.8 in-flight runs import as resumable or as archived.
- The existing refactor research makes two demands before any rewrite commitment: a failure inventory (reproducers on locked versions, expected vs observed, which run states it hits) and a written spec for the semantics currently "whatever langgraph does", namely cycles, fan-in ordering, duplicate human submissions, nested interruption, retry boundaries, budget pauses, cancellation races, and concurrent runs (`plans/research/refactor-lang.md`). No such inventory exists in the repo today (unverified beyond that document's own statement).
- The engine is deliberately "a pure LangGraph wrapper for graph scheduling" with no scheduling machinery of our own on top (`product/PRD.md` v0.2.x realignment). An owned engine has to add that machinery, not just port wiring.

### Prior decisions that shape the answer

- The recorded adapter already produces an op list that the production graph replays (`workflow.py:194` to `281`, `runner.py:654`, `runner.py:701`). `plans/research/refactor-lang.md` calls this half of an IR. But `product/PRD.md` also claims "no recorded op sequence" (PRD, composite wiring section). The two documents disagree, and refactor-lang.md flags the conflict as unresolved.
- A dual-implementation plan (adapter plus owned service through v0.6, retired at v0.7) was rejected as "a config-change switch nobody needed while costing four versions of duplicated maintenance" (`product/PRD.md` decision log). The strangler shape proposed in refactor-lang.md (feature-flagged owned backend, langgraph kept as a compatibility backend draining legacy checkpoints) is structurally similar. A proposal must either distinguish it or own the reversal.
- The Python 3.12 floor has a langgraph half: "3.12 is the floor until pydantic-core and langgraph officially support newer releases" (`product/PRD.md` stack decisions). Dropping langgraph removes one of the two stated reasons for the floor.

### Timing evidence, arranged along the timeline

- v0.3 is the release with the least engine entanglement (`v0.3.0.md:565`).
- v0.4 and v0.5 touch the engine only additively (`v0.4.0.md:16`, `v0.5.0.md:702`).
- v0.6 is the only release that adds new langgraph coupling (`v0.6.0.md:220`).
- After v1.0, semver and permanent format compatibility bind (`post-1.0.md`).

So the evidence-bound answer to "earliest": the code-level obstacles to dropping langgraph at the start of v0.3 are smaller than at any later point in the plan, and the cost curve rises at v0.6 and again at v1.0. Whether that earliest point counts as "minimal impact on reliability" rather than just "least wasted work" is what a proposal must argue, because the failure inventory that would answer it does not exist yet.

## Open questions a proposal must decide

1. Does the failure inventory get built first, and does its outcome change whether langgraph goes at all? `refactor-lang.md` treats removal as fixed policy while demanding the inventory it notes does not exist.
2. Which drop window: v0.3, v0.4, v0.5, v0.7, or post-1.0. Each carries a specific trade: the dry-run validator at v0.3, additive engine churn mid-flight at v0.4/v0.5, dead Postgres saver work after v0.6, semver constraints after v1.0.
3. What replaces the dry-run compile used by import-time validation and reached from the v0.3 read model. Can an owned validator reproduce the "structural problems raise from real compilation" guarantee without langgraph?
4. Who owns the scheduler semantics that are currently langgraph's. The loops-and-joins doc sketches join-freshness options but commits to none, and composite nesting (manual `graph.ainvoke` inside node functions versus native subgraphs) is flagged there as an open scheduling decision.
5. What happens to in-flight runs across the swap: drain through a compatibility backend, migrate checkpoint state, or abandon under the no-back-compat-before-1.0 stance. And does v0.8 `import-project` carry resumable state?
6. Does the strangler dual-backend shape conflict with the recorded rejection of the dual-implementation plan, and how is the new decision distinguished from the old one in the decision log?
7. Resolve the PRD-versus-code conflict over recorded op sequences. The PRD says none; `workflow.py`, `runner.py`, and the README show recorded `_Op`s replayed onto the production graph. The answer decides whether `_Op`s become the IR for an owned engine.
8. Where in-flight state lives in Postgres if the checkpointer is ours, and whether that changes the `PgRunStore` design at v0.6 or waits for the swap.
9. Pin policy during transition. Lower-bound-only pins make failure attribution ambiguous during and after the migration (`refactor-lang.md`); `uv.lock` holds 1.2.11 today.
10. Does the Python floor move once langgraph is gone, and is that wanted at the chosen window?
