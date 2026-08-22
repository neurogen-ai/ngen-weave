# Technical design

Planned contents, per the documentation hierarchy:

- `system-architecture.md` — components, protocols, data flow (see the diagram in ../product/PRD.md for the current sketch)
- Per-module designs as modules stabilize: core model and engine, registration/config, serving and RunService, web API contract, storage and provenance, plugins.

Rule: a design doc exists to record decisions with their reasons, not to spec every function. Implementation plans under ../implementation/ reference these; they never restate them.

## Open design questions (deferred deliberately, must be settled here before their version ships)

| question | blocks | notes |
|---|---|---|
| Observer predicate purity checking mechanism | v0.1 | "reject obvious I/O imports" is the loose bar; pick AST scan, import hook, or runtime guard |
| RunService ↔ langgraph-server identity mapping | v0.2 | how ngen-weave run-ids map to LangGraph threads/runs; needed for resume semantics |
| langgraph-server packaging friction evidence | v0.2 | record what feeding ngen-weave's registry/config into its deployment model actually costs |
| Push vs polling for run screens | v0.4 | decide on evidence once run management UI exists |
