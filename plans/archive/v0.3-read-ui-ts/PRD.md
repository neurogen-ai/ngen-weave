# agent-loom v0.3 PRD

Status: draft
Prerequisite: plans/v0.2/PRD.md is complete. `packages/core` (loader, Zod schemas, validator) and the MCP server exist; the LangGraph backend is the canonical executor.

## What v0.3 adds

A read-only web interface for viewing workflows: the graph as a navigable canvas, per-node detail views, and workflow-level contracts — all served from the workflow directories on disk. No write capability of any kind ships in v0.3. The value is a foundation: clean primitives, a lossless read API, and UI seams sized so that run management, workflow editing, and custom-node editing land later without redesign.

## Stack

Decided up front, and why:

- **SvelteKit + TypeScript** — maintainer preference; Svelte Flow makes the graph-library objection moot.
- **Svelte Flow** (xyflow) — same team as React Flow, feature parity on pan/zoom/fit-view/filtering.
- **shadcn-svelte / bits-ui** — accessible headless primitives, the Svelte analogue of Radix.
- **Tailwind CSS** — no orphaned-CSS drift (styles die with their component), design tokens via CSS variables consumed by utilities (light/dark theming is a variables file), and it is the styling vocabulary agents produce most reliably, keeping agent-driven maintenance cheap.
- **Layout:** `apps/web` (SvelteKit SPA-mode app) + `packages/web-api` in the existing monorepo. The API reuses `packages/core`'s loader and validator; the browser never parses YAML.

## Scope

In:

1. Workflow list view with validation status.
2. Graph canvas view: auto-laid-out nodes and typed edges, pan/zoom, filtering, hover highlighting.
3. Node detail views: metadata, schemas as structured tables, prompt bodies rendered or verbatim.
4. A read-only HTTP API that exposes parsed workflows losslessly.
5. Light and dark themes from day one.

Out (named so their absence is a decision, not an omission):

- Any mutation: creating/editing workflows or nodes, writing review artifacts, launching or controlling runs.
- Live run inspection. Run state, events.jsonl, checkpoints are invisible to v0.3.
- Auth, multi-user, remote deployment.
- LLM-generated summaries (see Plain-language view below).
- Websockets / push updates.

## Serving

One command: `loom ui [dir]`. Starts a single process serving both the static web app and the web API. Localhost tool; the deployment story waits until runs and auth exist.

### Which workflows are exposed

The API reads the project-root `loom.json` manifest when present (same manifest the MCP server uses), falling back to a convention-based scan of `<dir>/workflows/*/graph.yaml`. Both paths go through `packages/core`; there is no second parser anywhere.

## Data freshness

Lightweight **polling**, not push. The client polls the list endpoint every 2–5 seconds; each response carries a content hash per workflow, so unchanged graphs skip refetch and re-render entirely. Rationale: file-watching plus a websocket channel buys little for a viewing tool and complicates a deliberately simple server. Revisit when run management lands — live runs will justify push, and this PRD does not preclude it.

## Invalid workflows

Workflows failing `loom validate` appear in the list view marked invalid, with the full validation error list one click away. They do not render a best-effort graph — that is a large amount of tolerant-parsing machinery for edge-case value, and hiding them entirely would hide useful signal. Fixing the YAML and watching the card flip to valid (on the next poll cycle) is the intended loop.

## Read API contract

The seam everything future-facing hangs off. Endpoints (shape indicative; exact paths settled at implementation):

- `GET /api/workflows` — list: name, description, content hash, validity + errors.
- `GET /api/workflows/:name` — full parsed structure: graph config, nodes (typed, with all frontmatter fields), edges, fan-in maps, schema references, content hash.
- `GET /api/workflows/:name/schemas/:file` — resolved JSON Schema documents.

Two properties are contractual:

1. **Lossless round-trip.** The API exposes enough structure that a future editor can reconstruct the source files byte-faithfully (raw file bytes available alongside the parsed form). Write endpoints, when built, use exactly this contract inverted. If any construct turns out to be un-representable through this API, that is a bug in the API, fixed here before editors are designed.
2. **Validator-gated.** Everything served has passed `loom validate`. The UI never renders unvalidated structures.

## Graph canvas

Auto-laid-out via dagre/elk, left-to-right, cycles rendered as back-edges. Baseline navigation:

- Fit-to-view button, minimap, keyboard zoom, smooth pan.
- Click-to-focus: camera centers the node and opens its detail panel.
- Hover highlighting: connected edges and nodes highlight; everything else dims.
- Filters apply to both sidebar and canvas (filtered elements dim/hide).

### Filtering and sorting

- **Node type filter chips**: worker / control / human.
- **Edge type filter chips**: normal edge, control pass branch, control fail branch, human route, observer reroute. Filtering by *the kind of node an edge goes to* is included — this is what makes "hide all observer wiring" possible, which matters because observers attached to heavily-connected control nodes would otherwise clutter the canvas.
- **Search**: matches node names and prompt text.
- **Sorting applies to the sidebar lists only** (by name, by type, by topological order). Canvas position encodes topology; sorting the canvas is meaningless.

## Detail views

Every node shows: name, type badge, model + variant, attached observers, input/output schema names, and the node's `description` field if present.

Two presentation modes everywhere, **tab-selected, plain language as the default**:

- **Plain-language tab**: deterministic template-generated summaries ("Calls model kimi-k3 to draft a response; passes output to quality_gate"). Mechanical, derived purely from structure. This is deliberate: no LLM-summarization layer exists in v0.3, and richer prose later must slot into the same tab without UI changes.
- **Author tab**: raw fidelity for workflow authors — markdown bodies with a rendered/verbatim toggle, and schemas expandable to structured property tables (not raw JSON dumps; verbatim JSON behind a further toggle).

Workflow-level views show the graph's input/output contracts and run configuration (`max_steps`, retries) in both modes.

## The `description` field

Node definition files gain an optional top-level attribute:

```yaml
---
name: draft
type: worker
description: Drafts a response from the user's brief using kimi-k3.
...
```

Naming note: this is deliberately `description`, matching the field name already used at the graph level for MCP tool descriptions — but they are distinct things. The graph-level `description:` is what an LLM sees about the *workflow-as-tool*; the node-level `description:` is prose for humans reading the workflow. There is no collision because MCP never surfaces node descriptions, and loom should never rename either to dodge a mismatch that doesn't exist. Validation treats the field as free text, optional, any length.

All canonical example workflows shipped with loom include `description` on every node, so the plain-language tab is populated out of the box and the templates demonstrate the convention.

This field is the primary seam for the future non-technical audience: editors and run managers will surface these strings instead of schema dumps.

## Future seams (designed now, built later)

Each future capability names the seam it will enter through. None ship in v0.3.

| Future capability | Seam reserved now |
|---|---|
| Creating / editing workflows | Lossless round-trip read API, inverted |
| Launching / managing runs | Polling architecture upgrades to push; list view gains run-status columns |
| Interacting with runs (human review) | Review artifacts already have a canonical JSON form (v0.2); the UI submits through langgraph-server interrupt/resume endpoints |
| Custom node editing for non-technical users | Node `description` fields + plain-language tabs as the presentation layer; author/raw tabs remain for technical users |
| Richer summaries | Plain-language tab swaps template generation for LLM generation behind the same component |

Design rule for all of it: simple, maintainable code over speculative scaffolding. The seams above are contracts and naming decisions, not stub modules.

## Success criteria

1. All workflows in a repo are visible within two clicks of opening the app.
2. A ≤15-node workflow is fully legible without scrolling at typical laptop size.
3. Any validation error is findable from the list view.
4. Zero write capability shipped — no endpoint mutates anything, verified in review.

## Non-goals

Everything listed under Scope → Out, plus: parallel-execution visualization concerns, subworkflow display (until nesting exists), templating in artifact previews, mobile layout beyond "usable," internationalization.
