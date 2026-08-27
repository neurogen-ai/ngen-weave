# Research brief (UNANSWERED): containerisation as permission gate

Status: open question. This brief frames a research task; it draws no conclusions and no
research has been executed.

## Problem statement

ngen-weave currently gates agent tool use in-process: a `PermissionSet` per activation is
enforced by `PermissionGate` around a wrapped `ToolRegistry`. The open question is whether
that approach should be replaced (or supplemented) by running tools inside containers, so
that the container runtime — not our code — is the enforcement boundary. Which mechanism
should carry the security boundary, and what do we gain or lose either way?

## Baseline snapshot (current in-process machinery)

What the existing code actually does, by function:

- `PermissionGate.__init__` (`agent/gate.py`) binds the inner registry, the activation's
  `PermissionSet`, and its `RunContext`; counters start at zero per activation.
- `PermissionGate.call` checks, pre-call: tool name in `PermissionSet.allowed_tools`,
  executed-call count under `max_calls`, and accumulated tool-reported spend under
  `budget_usd`. On any breach it denies; otherwise it delegates to the inner registry and
  adds `cost_usd` from the result dict.
- `PermissionGate._deny` emits exactly one `permission_denied` provenance record, then
  raises per `denied_policy`: `DeniedToolError` ("fail_node") or `ReturnToReviewError`
  ("return_to_review").
- `PermissionGate.specs` exposes tool specs without exposing the registry, so the agent
  loop can only reach tools through `call`.
- `PermissionSet` (`agent/permissions.py`) is a frozen dataclass: `allowed_tools`,
  `denied_policy`, `max_calls`, `budget_usd`.

Implied properties of this design: enforcement is same-process with the agent loop; the
trust boundary is a Python object graph; accounting depends on tools self-reporting
`cost_usd`; a malicious or buggy tool shares the interpreter's privileges.

## Options

Option A — in-process gate (status quo): keep `PermissionGate`/`PermissionSet` as the only
mechanism.

- Pros: no infrastructure; denial/provenance semantics already centralised in one call path;
  budget and call accounting integrate with run state; fast (no cold starts); testable as a
  plain object.
- Cons: enforcement and the untrusted code run in one process; a tool bug is a process bug;
  no filesystem/network isolation beyond what tool code chooses to do; "no regex"-style
  coding standards carry security weight here.

Option B — containerisation as the permission gate: run each tool (or each activation's
tools) in a container; the runtime's isolation primitives express the permission set.

- Pros: OS-level isolation of filesystem, network, and process space; a crashing tool does
  not take down the engine; resource ceilings (CPU/memory) come from the runtime; less
  custom security code to get wrong.
- Cons: cold-start latency and lifecycle management per activation; mapping `PermissionSet`
  semantics (allowed tool names, `max_calls`, `budget_usd`, `denied_policy` including
  return-to-review) onto container policy is nontrivial and partially lossy; provenance
  emission (`permission_denied`) must still cross the boundary; images must be built,
  versioned, and pinned; local dev and CI get heavier; Windows/macOS portability questions.

## Evaluation criteria

The research task should compare both options against, at minimum:

1. Security properties: what exactly does each mechanism contain — filesystem writes,
   network egress, process escape, spend, call volume? Where can enforcement be bypassed?
2. Developer friction: time from "new tool" to "tool runnable under the gate"; local
   debugging experience; CI cost.
3. Maintenance: who maintains the boundary code (ours vs container runtime + images);
   upgrade and vulnerability-patching burden.
4. Portability: behaviour across dev machines, CI, and likely deployment targets; does the
   permission model behave identically everywhere?

## Expected deliverables

A research report answering: (a) a concrete threat model for the current in-process gate;
(b) a mapping table from `PermissionSet` fields to container-policy equivalents, with gaps
marked; (c) measured cold-start and per-call overhead for a containerised tool; (d) a
recommendation (possibly hybrid: in-process gate retained for accounting/denial semantics,
containers for execution isolation) with the criteria above as its justification, plus a
costed implementation sketch if the answer is not "status quo".
