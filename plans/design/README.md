# Technical design

Planned contents, per the documentation hierarchy:

- `system-architecture.md` — components, protocols, data flow (see the diagram in ../product/PRD.md for the current sketch)
- Per-module designs as modules stabilize: core model and engine, registration/config, serving and RunService, web API contract, storage and provenance, plugins (see plugins-and-ui.md).

Settled elsewhere and recorded in the decision log: every source file opens with a ≤20-line semantic module docstring (see PRD decision log; convention specified in ../implementation/README.md); observer predicate purity checking was rejected entirely (predicates are user code; we validate declarations and surface errors, we do not police bodies).

Rule: a design doc exists to record decisions with their reasons, not to spec every function. Implementation plans under ../implementation/ reference these; they never restate them.

## Open design questions (deferred deliberately, must be settled here before their version ships)

| question | blocks | notes |
|---|---|---|
| Push vs polling for run screens | v0.4 | decide on evidence once run management UI exists (v0.4 plan reserves this as an amendment decision) |

Settled in implementation planning and recorded there: nothing currently; former v0.2 questions (RunService identity mapping, langgraph-server packaging friction) dissolved when the dual-implementation serving plan was dropped for a single owned FastAPI service.
