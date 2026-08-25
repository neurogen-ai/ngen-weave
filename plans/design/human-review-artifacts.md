# Human review artifacts

Decisions and reasons for the v0.1 human-in-the-loop slice: artifact shape,
submission handling, and interrupt mechanics. The how-it-works detail lives
in `/docs/engine/execution.md`; this doc records why it is that way.

## Artifact file format

One YAML file per waiting review at
`.ngen-weave/runs/<run-id>/artifacts/<node_path>.yaml`, two sections:

```yaml
context: {...}     # edge input, validated against input_type, never edited
response: {...}    # null-seeded slots generated from state_type leaves
```

Reasons:

- The release requirements fix this exact on-disk shape; v0.2 remote JSON
  submission and the v0.4 UI read the same structure, so changing it later
  would break the first consumers rather than widen a format.
- The file name carries the full node path with dots replaced by `__`
  because identity is the class path: two humans may share a short class
  name across modules, and a collision here would silently overwrite one
  person's review with another's.
- Slots start null even when the state field has a default. Prefill fills
  but never completes an artifact; only a submission event does. Seeding
  defaults would blur the line between "nothing filled in" and "accepted as
  is", and completion validation could not tell them apart.

## Submission handling

`Engine.resume(run_id, payload=None)` is the only entry. Payload given means
remote JSON; None means read the local YAML's `response` section. Both are
validated identically against `state_type` before anything else happens.

Reasons:

- Validation-before-mutation means a rejected submission leaves the run
  exactly as it was (still `waiting_human`, no records appended), so a typo
  by a reviewer cannot wedge anything.
- The accepted response is recorded three ways on purpose: `artifact_write`
  provenance carries its SHA-256 over canonical JSON (tamper-evident),
  `RunFile.submissions` keeps it keyed by node path (the run file stays
  sufficient to re-run without the artifact directory), and the value itself
  travels into the graph.

## Interrupt mechanics

LangGraph interrupts pause the superstep and re-run the interrupted node
task from its start on resume. Two consequences shaped the design:

- Side effects before `interrupt()` must be skipped on replay. The engine
  marks continuation invocations with a `resuming` config flag; human nodes
  check it and skip artifact writing and provenance emission. A duplicate
  waiting record per replay would be defensible, but a rewritten artifact
  after submission would be a real bug.
- Nested humans need manual propagation. The engine invokes composite child
  graphs explicitly, so a child's interrupt surfaces as `__interrupt__` in
  the returned state rather than pausing the parent automatically. The
  composite node calls `interrupt(None)` to park every enclosing graph with
  real registered interrupts, while the submitted response travels down
  through config (`ngen_resume_value`) and each level forwards it as
  `Command(resume=...)`. One source of truth for the payload; the framework
  owns only pause/replay mechanics. Alternatives rejected: raising a bare
  `GraphInterrupt()` registers nothing resumable (probe-verified), and
  threading the response through framework interrupt values would make every
  enclosing level's resume value semantic-bearing instead of pass-through.

## One checkpointer per graph level

Parent and child graphs must not share a memory checkpointer instance. With
a shared saver, interrupt resume silently becomes a no-op: the root
invocation returns stale state without re-running any task (probe-verified).
Every compiled graph now owns a dedicated saver. SQLite mode is unaffected
in behavior because the file is the state, but uses per-level instances too
so both modes exercise identical code paths.
