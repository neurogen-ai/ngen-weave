# agent-loom v1.0 PRD

Status: vision and constraints. Nothing here is scheduled; everything here constrains what v0.2 is allowed to build.

## Ambition, stated precisely

The long-term goal is a competitor to Netflix Conductor for agentic workflows. Stated that baldly it sounds crazy, because generic distributed orchestration is a mature bloodbath: Conductor, Temporal, Airflow, and Argo own queuing, scheduling, sharding, and failure domains. Rebuilding that layer is not the plan.

What none of them have is loom's actual product: schema-gated boundaries between steps, boolean control routing in declarative YAML, human review as a structured artifact, and cost/token observer policies on every node. LLM pipelines are a genuinely underserved niche for exactly these mechanisms. So v1.0 means: loom as the agentic workflow layer, someone else's machinery as the distribution layer.

## The three paths

1. **Compile to Argo Workflows** (assumed first target). Loom graphs compile to Argo templates the same way they compile to LangGraph. Workers become containers, control booleans become conditional DAG edges, human nodes map to Argo `suspend`, fan-in maps to DAG dependencies. This reuses the exporter pattern from v0.2 almost verbatim, which is why it is cheap.
2. **Temporal as substrate.** Durable execution with signals mapping naturally onto human interrupts. More power, significantly more concepts imposed on users. Kept as a documented alternative, not built.
3. **Own the backend.** A custom distributed executor. Not until users or revenue force it.

## Constraints on earlier versions

These are the rules v0.x must follow so that path 1 stays cheap. Each costs nothing now and is exactly what gets violated accidentally if nobody writes it down.

- Node execution stays a pure dispatchable function of `(node definition, input, config)`. No node may reach into engine process state. This is what makes remote dispatch possible later.
- Run state remains externalizable data: checkpoints and event logs are files or records any process can read. No state lives only in memory or only behind a live process.
- Every execution backend enters through the same two interfaces (`CompletionProvider`, `NodeExecutor`) plus one compile/export target. Adapters translate; they never extend.
- Workflow YAML is the single source of truth. Compiled artifacts (LangGraph projects, future Argo templates) are generated, committed, and never hand-edited.

## What v1.0 adds, when it happens

- An Argo export target and its CI pipeline.
- Remote node dispatch: workers execute as containers, results return through the checkpoint store.
- Multi-tenant serving: workflow registries per project, MCP server pointed at remote backends.
- Whatever scheduling gaps Argo leaves (retries across nodes, priority queues) bridged rather than rebuilt.

## Non-goals

- Building queueing, sharding, or scheduler infrastructure.
- Abandoning the single-node story. Local file-based runs remain fully supported forever; distribution is an additional mode, not a replacement.
