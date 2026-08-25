# Engine execution model

How `Engine` in `ngen_weave.engine.runner` turns a workflow class into a
runnable graph and drives a run. Everything here describes implemented v0.1
behavior; the plans state the requirements, this page states the mechanism.

## One build, then a replay

`build()` runs exactly once, against the recording `_StateGraphAdapter`. The
adapter notes every call as an op (nodes, edges, conditional edges with their
routers). The production graph is then constructed by replaying those ops.

Why the indirection: the LangGraph state schema needs the full set of node
keys up front, and that set is only known after `build()` has run. Replaying
recorded ops onto a fresh StateGraph keeps the single-build guarantee from the
plans while letting the engine pick the schema second. The op list is never
used for validation; import-time checks keep their own dry-run build.

## State: one channel per child

The production graph's schema is a dynamically built `TypedDict` with
`total=False`, giving every wired child its own last-value channel. Two
reserved keys join them:

- `__ngen_input__`: the seeded root input dump.
- `__ngen_last__`: class path of the real node that wrote most recently,
  with a last-wins reducer so concurrent writers in one superstep cannot
  conflict.

A plain `dict` schema was rejected after testing: LangGraph treats it as a
single replaceable value, so each update wipes the rest of the state, and two
nodes in the same superstep clobber each other. Per-key channels merge safely.

Relay nodes (`__relay_N__`) write nothing; their only job is depth alignment
(below).

## Input assembly per fan-in form

Each destination's assembly form is fixed at compile time in the plan:

- entry: reads `__ngen_input__` (the composite input).
- single: passes the parent's dump through.
- slots: `{field: parent dump}` for each `into=` edge.
- collect: appends parent dumps to the input model's one list field.
- dispatch: takes the output of whichever node dispatched here.

Assembly raises a `DataError` naming missing parents if a form's sources have
not all written; with depth alignment (below) this is unreachable for static
fan-in and exists as a guard.

## Dispatch semantics

A target reached only through conditional edges has no static parents by
design (the canonical code-review example routes to `finalize` only by
dispatch). Its effective parent is the dispatching node: the engine delivers
the sender's validated output as its input, read through `__ngen_last__`.
Targets that also declare static parents ignore dispatch for assembly;
dispatch only triggers execution. This is why a reject edge back to the entry
child re-runs it on the original request.

## Depth alignment: relay nodes

LangGraph schedules breadth-first and fires a multi-parent target when any
trigger arrives, not when all parents have written. A diamond where the paths
have unequal lengths (the normal shape for collected fan-in: r1 feeds r2 and
r3, all three feed the reducer) therefore breaks: the reducer fires alongside
its siblings' children.

The engine computes longest-path levels over static edges (back edges excluded
so loops stay legal) and inserts identity relay nodes on shorter incoming
paths until every parent chain reaches a joint target at the same level. All
parents then complete in the previous superstep, and the target fires exactly
once. Conditional edges stay direct: dispatch is supposed to fire immediately,
and loop-back edges go straight to their target.

Relays are engine-internal. The editor and validators read topology from the
dry-run build of the author's own wiring and never see relays.

## Routers and retries at runtime

Author routers are wrapped once at replay: a return value absent from the
branch map raises `DataError` naming the node and value instead of leaking a
LangGraph internal error.

Node functions retry only `InfraError`: initial attempt plus `max_retries`
retries, waiting `retry_backoff_ms * 2**(n-1)` milliseconds between attempts
(engine-owned `_sleep` alias, patched in tests). Every retry emits a
`node_activation` record with `{"status": "retry", "attempt": n}`. A leaf
`DataError` emits one `{"status": "invalid"}` record and never retries.

## Run files and resume

`RunStore` owns `.ngen-weave/runs/`; the file format lives in
`engine/state.py` (format 1). Provenance appends are load-modify-save per
record, which is quadratic in run length but correct at CLI scale; the SQLite
swap at v0.2 replaces the backing behind this same class.

Checkpoints thread through LangGraph (`AsyncSqliteSaver` by default, a shared
`MemorySaver` for tests) under `thread_id = run_id`, namespaced per drive
attempt (see below). `resume` on a completed run returns the stored output
without touching checkpoints; on a stopped run it re-executes from the top
under a fresh namespace, seeded with the run file's stored input. Until Step
9 adds interrupts there is no mid-graph continuation to replay. Cross-process
resume of a crashed run needs the original model bindings, which the run file
does not carry; within one process the compile cache provides them, and Step
9's human flow is the first caller that relies on this path.

## Model variant resolution

Resolved at compile, frozen per compiled graph: exact class-path binding beats
innermost enclosing composite beats the provider's default variant (read off
the provider's `default_variant` attribute when present, else `"default"`).
The chosen variant lands in every `model_call` provenance payload, so resumed
runs and supervision see which variant actually ran.

## Nesting and cost attribution

Composite children compile eagerly at parent compile time. `compile` carries
an `outer_scopes` chain (innermost first), which does two jobs: model
bindings keyed on ancestors above the compiled class still govern its leaves,
and the full class-path chain becomes the base every inner activation path is
prefixed with, so node paths accumulate one segment per level
(`Root.Inner.Leaf`). A cycle guard rejects composite wiring that loops back
into its own subtree at compile time.

At runtime a composite child activates like any leaf from its parent's
perspective: assemble input per the declared fan-in form, validate against
the child's `input_type`, invoke the child's compiled graph under a nested
checkpoint namespace (`attempt-N:<node_path>`), validate the child's terminal
dump against the child's `output_type`, then write `{path: dump}` into parent
state exactly as a leaf would. Nothing above the composite knows it has
children.

Usage flows through state, not config. Every node returns its usage tuples
under an accumulated channel (`__ngen_usage__`, reduced with `operator.add`);
a composite reads its subtree total from the child's final state and folds it
into its own metadata. Each completed scope therefore emits one
`node_activation` record whose `metadata` covers the whole subtree:

- leaves report their own activation (attempts included),
- composites report once, summing all descendants,
- the root reports from `_drive` after successful completion, since the root
  workflow is not a node in its own graph.

Per-level cost attribution falls out of this without level-specific code:
inner totals are subsets of outer totals by construction.

Composites sit outside the retry loop. Leaves inside already apply the retry
policy; re-driving a whole subtree after its budget was exhausted would
multiply cost without changing the outcome, so an escaping `InfraError`
propagates to the top-level catch untouched.

## Attempts, resume, and why namespaces exist

LangGraph does not reschedule a node that raised: the failed superstep
completes, no triggers remain, and replaying the same checkpoint namespace
ends immediately with nothing written. `_drive` therefore bumps
`RunFile.attempts` on every drive and runs under namespace `attempt-N`
(nested activations under `attempt-N:<node_path>`). Until interrupts arrive
(Step 9), resuming a stopped run re-executes from the top, seeded with the
run file's stored input; the record stream keeps every attempt's provenance,
which is the honest history.
