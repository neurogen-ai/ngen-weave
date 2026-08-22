> Post-1.0 release requirements. Scope and decisions are governed by ../product/PRD.md, which wins on any conflict.

# ngen-weave post-1.0: 1.1, 1.2, 1.3

## v1.1: distribution

ngen-weave graphs compile to Argo Workflows the same way they map onto LangGraph: workers become containers, control booleans become conditional DAG edges, human nodes map to `suspend`, fan-in maps to DAG dependencies. Remote node dispatch: workers execute as containers, results return through the checkpoint store. Whatever scheduling gaps Argo leaves (cross-node retries, priority queues) gets bridged rather than rebuilt.

Constraints that make this cheap, already enforced by earlier versions:

- Node execution is a pure function of (definition, input, config); no engine process state access.
- Run state is externalizable data; checkpoints and provenance live in records any process can read.
- Every backend enters through RunService plus one compile target. Adapters translate; they never extend.
- Compilation reads serialized workflow definitions, not live Python objects.

Temporal stays a documented alternative backend, not built.

## v1.2: collaboration

Full team features on top of v0.6 auth/roles and the v0.5 slice: shared workspaces across projects, ticket workflows with role-based assignment queues, collaborative review sessions (multiple humans in one review artifact concurrently), notification routing per team. Simultaneous editing of documents remains adapter territory (external editors like Google Docs stay linked, not stored); simultaneous *workflow* editing in ngen-weave-web is the new ground here and will need an operational transform or CRDT decision made with evidence at design time.

## v1.3+: vertical packs

Workflow-type agnostic core, vertical-specific packs:

- **Coding IDE pack first**: code-graph artifacts, repository-aware review workflows, CI hooks launching runs on PRs. Chosen first because building the thing that builds things stress-tests the primitives hardest.
- **Research pack after**: OpenAlex/Semantic Scholar search provider plugins, literature-review workflows, citation-bearing document artifacts, provenance exports shaped for open-science reproducibility.

Packs install as ordinary plugin packages (v0.5 mechanism). If a pack needs a core change, that is a bug in the core's extension seams, fixed there before the pack ships.
