# Refactoring the engine: which language?

Status: research. No decision made here. Motivation: langgraph must go long term for
reliability reasons, so we evaluate what to rebuild the graph logic on top of:
python, go, typescript, zig, or baml for parts of it.

## The premise needs evidence first

"LangGraph is unreliable" is the reason we're doing this, but no document in this repo
lists the failures behind it. v0.1.1, v0.1.2, and v0.1.3 release notes record compile
cache bugs, resume nondeterminism, and a gate bug; `product/PRD.md` records that
langgraph ignores caller-supplied top-level checkpoint namespaces, which forced
per-level thread ids (`runner.py:1474`). Those are real incidents, but nobody has yet
answered: are these langgraph bugs, our integration bugs, SQLite concurrency issues, or
underspecified semantics on our side?

Before choosing anything we should build a failure inventory: reproducer, locked
version from `uv.lock`, expected vs observed behavior, and which run states it hits
(interrupted runs, nested runs, retries, budgets). If an upgrade or tighter pin fixes
most of it, the rewrite calculus changes completely. This plan treats the reliability
motivation as fixed policy anyway (the owner has decided langgraph goes), but claims
about what another language would fix stay conditional on this inventory.

One honest caveat up front: most of what langgraph gives us is durable execution,
checkpointing, interrupts, and resumability. Nothing about Go or TypeScript or Zig
does any of that natively. Owning those semantics ourselves is the actual work, and
that work exists in every option. The language choice decides where that code lives
and what it costs to maintain, not whether reliability comes free.

## What the engine actually does today

Facts that constrain the choice:

- **The workload is IO-bound orchestration.** Model calls happen in exactly two places
  (`runner.py:1063`, `agent/harness.py:95`). Everything else moves dicts around, checks
  budget totals against indexed single-row reads, and appends records to SQLite. There
  is no CPU-bound inner loop a faster language would accelerate.
- **Langgraph coupling is real but narrow in shape.** We use `StateGraph`,
  `interrupt`/`Command`, `MemorySaver`/`AsyncSqliteSaver`, `RunnableConfig`
  threading, and stream consumption via `astream(stream_mode="updates",
  subgraphs=True)`. All imports are lazy inside `runner.py`. The authoring side already
  sits behind our own hand-written `GraphBuilder` protocol (`workflow.py:162`) backed by
  a recording adapter (`_StateGraphAdapter`), which replays `_Op`s onto the production
  graph. That recording adapter is effectively half of an IR already.
- **Workflows are Python classes with pydantic boundaries, Python routers, Python
  `run()` methods, and entry-point plugin discovery.** Any non-Python engine either
  keeps calling that code somehow, restricts workflows to a declarative subset, or
  breaks the authoring contract.
- **LiteLLM is the only model touchpoint** (`models/registry.py`), behind our own
  `CompletionProvider` protocol. Replacing provider routing, usage accounting, cost
  math, streaming, tool calls, and error mapping is a product-sized project in any
  language.
- **Seams exist on purpose:** `RunService` protocol (in-process FastAPI vs HTTP),
  `CompletionProvider` protocol, content-addressed `ArtifactStore`, backend-neutral
  conformance test suite. The product PRD explicitly kept RunService as the seam for a
  future backend swap.
- **No API compatibility before 1.0**, per the decision log. We can reshape formats
  now cheaply; after 1.0 it gets expensive.

Historical note worth taking seriously: this project started as a thin sequential
TypeScript engine, then deliberately moved to wrapped-langgraph-python at v0.2 for
"interface economics", and that decision log also rejected a dual-implementation plan
as "a config switch nobody needed while costing four versions of duplicated
maintenance". A second language decision here walks back into that same trap unless
the straggler path is explicit. Revisiting a closed question is allowed if the ground
truth changed; the ground truth that changed is langgraph itself.

## Evaluating each candidate

### Python

The default answer, and I think the right one for the engine core. Arguments:

- Zero bridge costs. Workflow classes, pydantic validation, plugins, LiteLLM, FastAPI,
  CLI, MCP all keep working unchanged while only the graph runtime underneath swaps.
- The hard part of dropping langgraph is defining our own scheduler semantics: ready
  queues, channel state, fan-in ordering, suspend/resume continuations, nested scope
  state. We can express and test that in Python as easily as anywhere, and the
  existing engine test suites port directly.
- Performance is not a factor until someone shows the driver loop matters under
  real workloads, and model latency dominates wall time by orders of magnitude.

Counterargument, stated fairly: Python doesn't give reliability automatically. Our own
dynamic user code, async cancellation races, mutable shared state, and dependency
churn remain problems regardless of framework. An owned Python scheduler needs strict
state ownership, versioned checkpoint formats, and replay tests, or it just becomes
langgraph's ambiguity moved into our code.

### Go

Best deployment story: static binaries, excellent concurrency primitives, one
operable service binary. Where it fits: a remote execution *service* once serialized
definitions and a stable wire protocol exist (the post-1.0 Argo path wants exactly
this). Where it fails today: Python workflows can't run inside it. Options are
embedding CPython (ABI, GIL, allocator, packaging pain) or a worker subprocess
protocol (version negotiation, serialization, backpressure, cancellation, error
mapping, supervision), either of which duplicates schema and error semantics across
the boundary. Plugin discovery becomes impossible or RPC-based. Two runtimes means
two debuggers, two CI matrices, and harder contributions. Strongest future role:
standalone executor service consuming JSON-Schema definitions over an owned protocol.

### TypeScript

The ecosystem argument is real: MCP SDKs are first-class, Node async IO is fine, and
the browser-adjacent tooling story beats everyone else's. And there's precedent here;
v0.1 was TS. But runtime types vanish, so pydantic contracts need a replacement story
per node, existing Python workflow methods need migration or RPC, ESM churn is a tax,
and provider abstraction would be re-platformed off LiteLLM. Sensible only if the
product pivots to a data-only declarative workflow format (no Python callbacks) and
prioritizes web/Node integration over current authors. That's a product decision we
haven't made.

### Zig

Small binaries and explicit control over memory and resources, at the cost of owning
nearly everything else: LLM clients, MCP transports, JSON Schema handling, persistence,
web serving, package ecosystem. Hiring and contribution pools are thin. For a system
whose hot path is waiting on a model API, the performance gains buy nothing. If a
component ever needs Zig, it would be a narrow native utility (say, a fast artifact
hasher), not the engine. Not recommended for any phase of this refactor.

### BAML

Different category than the other four, so it needs different framing. BAML is a DSL
for LLM interaction: prompts as files, typed function signatures with schema-guaranteed
structured outputs via recovery-and-retry parsing, provider config in manifest form,
and generated typed clients for Python, TypeScript, and Go. It is not a graph or
workflow engine; it has no notion of checkpoints, interrupts, or durable execution.

Where it could genuinely earn a place:

- **The conversation loop and structured output boundary.** Today `_complete` +
  `parse_output` + retry policy reimplements what BAML gives for free: type-safe
  structured extraction, automatic malformed-reply recovery, retry heuristics tuned by
  people who fight providers daily. `agent/harness.py`'s gated tool-use loop maps
  naturally onto BAML's function paradigm.
- **Model binding configuration.** Our `models.json` variants overlap heavily with
  BAML's client/provider config. If BAML covers the variants we use, it deletes a
  chunk of `models/registry.py`.

Where it fights the project:

- **It stays a Python dependency compiled ahead-of-time.** Codegen (CLI or VS Code
  extension) into committed sources adds a build step to DX and breaks the "plain
  Python classes" authoring story unless managed carefully.
- **Worker prompt templating currently lives in Python classes** with template
  inheritance across composites, prefill for human nodes, and model selection frozen
  at compile time for deterministic resume. Moving to .baml files changes how authors
  write and share prompts; that is a workflow-authoring-contract change, not a drop-in.
- **Lock-in direction reverses**: instead of wrapping langgraph, we'd wrap a vendor's
  codegen pipeline whose scheduler behavior and parse fallbacks live outside our
  control. That is exactly the reliability trap we're leaving. Deciding to adopt BAML
  should demand the same failure inventory standard: reproducers on our version, exit
  ramps, and behavioral parity tests for structured parsing before cutover.

Verdict on BAML: evaluate as a replacement for the completion+parse layer only
(Structured output for workers, controls without decide(), AgentNode), under the same
CompletionProvider seam so it remains swappable. Do not treat it as an engine
replacement and do not let it become the new langgraph. Worth prototyping against the
fake_provider test suite before any commitment; open questions include custom provider
endpoints (llama.cpp local servers), per-variant cost accounting parity, and whether
BAML's retry semantics map cleanly onto DataError-vs-InfraError classification.

## Recommended shape of the migration

Strangler, not big-bang rewrite:

1. Build the failure inventory first. It may kill the urgency, redirect it to a
   version pin, or confirm it.
2. Introduce an owned `ExecutionBackend`/`GraphRuntime` protocol. Convert validated
   wiring plus recorded `_Op`s into an explicit internal IR (the recording adapter is
   halfway there; formalize it).
3. Write an owned Python scheduler implementing explicit semantics: superstep or
   ready-queue execution, fan-in assembly, interrupt-as-parked-state, retries,
   budgets. Own checkpoint format carrying definition hash, engine version, node
   state, pending human input, budget counters, nested scope state. RunFile alone
   cannot reconstruct in-flight state today; the new format must, because old-run
   resumption silently ignoring internal channel state is unacceptable.
4. Feature-flag new runs onto the owned backend; keep langgraph as compatibility
   backend draining legacy checkpoints; compare using deterministic fixtures and
   recorded replies, never double-calling real models.
5. Frontends, adapters, and protocols stay untouched throughout.
6. Only after serialized definitions, checkpoint format, and run protocol stabilize:
   consider Go as a standalone executor service if measured requirements justify two
   runtimes. TS and Zig stay out of the engine.

Two documentation conflicts must be resolved before step 3: `plans/product/PRD.md`
claims "no recorded op sequence / topology from dry-run compile" while
`workflow.py` records `_Op`s and README documents it. Decide whether `_Op` debt
becomes the IR or gets deleted. Second: lower-bound-only pins
(`langgraph>=0.2`, `litellm>=1.40`) make failure attribution ambiguous during and
after transition; tighten them.

## Concrete claims to verify before asserting further

- LangGraph's exact guarantees for at-most-once node execution, crash replay,
  interrupt delivery, nested subgraphs, and checkpoint ordering, on the exact locked
  versions.
- Whether reported failures reproduce on `uv.lock` versions and whether an upgrade
  fixes them.
- Whether all current workflows survive v0.4's serialized-definition format without
  executing arbitrary Python, and which features (routers, method-form prompts,
  validators, observers, plugin nodes) break.
- Required semantics needing a spec before any reimplementation: cycles, fan-in
  ordering, duplicate human submissions, nested interruption, retry boundaries,
  budget pauses, cancellation races, concurrent runs.
- BAML specifics: current client-generator workflow, Python API ergonomics in-library
  versus generated code, support for custom OpenAI-compatible endpoints, cost/usage
  reporting parity with litellm, license and self-hosting posture.
- MCP SDK maturity in Go and TypeScript for our required transports (stdio, HTTP) if
  those languages enter the picture later.
