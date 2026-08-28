# Loops and joins: deferred execution-model work

Status: deferred, not scheduled. Prerequisite for any fan-out or join feature work after v0.2.x. Authored alongside the v0.2.1 realignment (Branch D), which removed the relay depth-alignment machinery and restricted joins to equal-depth acyclic shapes.

## What v0.2.x does instead

The engine is a pure LangGraph wrapper as far as graph scheduling goes: nodes fire when any trigger arrives, superstep concurrency is LangGraph's business, and the engine adds no scheduling machinery on top. Compile-time validation keeps the supported shapes deterministic:

- multi-parent fan-in (slots, collected) requires equal static depth and an acyclic graph;
- single-parent conditional re-entry (retry loops) is supported with defined semantics — the retried node fires on its static parent's last written output; START-delivered entry inputs are always fresh;
- conditional dispatch works for single-sender supersteps (the canonical review pattern).

Known limitations (documented in `docs/engine/execution.md`):

- concurrent dispatch senders race on the `__ngen_last__` last-wins channel — delivery is nondeterministic and may deliver the wrong sender's payload to a dispatch-only target;
- concurrent terminals reaching END make recorded run output selection (`_select_output`) completion-order dependent;
- joins inside loops are forbidden by validation, not solved.

## Why relays were removed

Relay nodes lifted shorter parent chains so multi-parent targets fired exactly once, after all parents wrote — correct for acyclic graphs, and the reason unequal-depth diamonds (the documented "normal shape" for collected fan-in) worked at all. But the alignment was static, computed once over the acyclic projection. Under loops with partial re-entry — a loop-back that re-fires only one parent chain of a join — the join fires on the loop-side trigger using the stale channel value of the co-parent that did not re-fire. Silent wrong data on iteration 2+, exactly the class of behavior the no-merge-overwrite principle forbids. Relays also fenced basic structures behind validation: a control node retrying a mid-chain worker was a compile error because input assembly "cannot know how many parents fire."

The realignment chose loud, small semantics over quiet machinery: LangGraph-native firing, compile-time shape restrictions, documented limitations. The machinery below returns only when a concrete need justifies redesigning it.

## Deferred: join freshness semantics

Problem: a join (slots/collect) whose parents can re-fire at different times needs a definition of which parent writes count as "this iteration's" input. Channel persistence makes old writes indistinguishable from current ones; a per-sender step-stamped merge can disambiguate (LangGraph exposes the superstep index in `config["metadata"]["langgraph_step"]`), but the right semantics (error on mixed freshness vs. wait vs. deliver-last) is a product decision, not a mechanism decision. Options sketched, none committed:

1. wait-for-fresh (reintroduces scheduling fencing — the thing this realignment removed),
2. deliver-if-exactly-one-fresh-else-DataError (deterministic, loud, no fencing),
3. per-iteration scoping via checkpoint namespaces (heavy).

## Deferred: dispatch delivery

The dispatch race: two nodes writing in one superstep where at least one has a conditional edge to a dispatch-only target; the target reads `__ngen_last__` (last-wins across all writers) and may receive the wrong sender's payload.

Option C (state slots, previously vetted): per-target dispatch slots written eagerly by every node for its compile-time-possible conditional targets, value `{sender: (dump, langgraph_step)}` merged per-sender; the target filters entries whose step equals its own activation step minus 1; exactly one → deliver, zero → existing DataError, two → new DataError naming both senders. Corrections from vetting that any implementation must carry: the step index lives in `config["metadata"]["langgraph_step"]` (not `configurable`), steps start at 1, interrupt replay re-executes with the same step number so the −1 filter survives, and slot writes are eager over all possible targets because a node cannot know its router's choice at return time. Compile-time complement: reject fan-out siblings that share a dispatch-only target. Consequence to own: shapes that "sometimes worked" fail loudly. Cross-upgrade note: a run parked `waiting_human` pre-upgrade can hit the loud zero-fresh path on resume — accepted under the no-back-compat-before-1.0 stance.

Option C′ (Send-based): the engine's router wrapper returns `langgraph.Send(target, sender_dump)` for dispatch-only targets so the payload travels with the trigger and never touches a shared channel. Deletes the race at the source, no schema change, but changes the node-input contract for dispatch-only targets and couples delivery to Send task identity under checkpoint replay — needs its own LangGraph probe before any planning.

## Deferred: scheduling policy

Open question: should the engine ever again constrain scheduling (literal sequential node execution, custom executors, concurrency caps), or is "pure wrapper + compile-time shape checks" the permanent model? The relay removal was motivated by the wrapper stance; any return to fencing needs a use case relays/diamonds never had. Decide when fan-out features return.

One existing deviation belongs in this decision, not before it: composites nest via manual `graph.ainvoke` inside their node functions (the "Granularity note (approved deviation)" in `_consume_root`) — nested activations run to completion inside one parent superstep and never surface in the root stream, where LangGraph natively supports compiled subgraphs. That is scheduling behavior the engine overrides. When scheduling policy reopens, decide whether composite nesting moves to native subgraphs or stays manual; the boundary mechanism's per-superstep granularity note depends on it.

## Deferred: terminal selection

`_select_output` reads `__ngen_last__`, so concurrent terminals reaching END make the recorded output nondeterministic. Cheapest fix if it ever bites: compile rule "exactly one node may hold an edge to END" (kills the race outright, narrows early-exit authoring patterns that `_check_terminals` currently supports). Mechanism alternatives (per-edge output stamping) land alongside the dispatch-delivery work since they share the sender-identity problem.
