"""LangGraph compilation and workflow execution."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import operator
import time
from asyncio import sleep as _sleep  # engine-owned alias; tests patch this
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_origin

try:  # langchain-core ships with langgraph; needed for config injection
    from langchain_core.runnables import RunnableConfig
except ImportError:  # pragma: no cover
    RunnableConfig = Any  # type: ignore[misc,assignment]

from pydantic import BaseModel, ValidationError

from ngen_weave.agent.errors import ReturnToReviewError
from ngen_weave.artifacts import ArtifactMeta, ArtifactStore, hash_value
from ngen_weave.config import RunSettings
from ngen_weave.constants import BUDGET_UNLIMITED, REPLY_EXCERPT_CHARS
from ngen_weave.engine.state import RunFile, RunResult, RunStatus
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import (
    AgentReplyError,
    ConfigError,
    DataError,
    InfraError,
)
from ngen_weave.human import apply_prefill, build_response_slots, validate_completion
from ngen_weave.models.provider import CompletionProvider
from ngen_weave.provenance import (
    PROVENANCE_VERSION,
    ProvenanceRecord,
    RunMetadata,
    join_path,
)
from ngen_weave.registry import get as registry_get
from ngen_weave.schema_errors import format_validation_error
from ngen_weave.service import UnknownRunError
from ngen_weave.workflow import (
    END,
    START,
    Control,
    Human,
    RunContext,
    Worker,
    Workflow,
    _incoming,
    _instantiate,
    _StateGraphAdapter,
    _Wiring,
    _wiring_from,
    resolve_model_variant,
    workflow_class_path,
)

# Reserved state keys: the seeded run input, which real node wrote last
# (used to deliver input to conditional-dispatch targets, whose effective
# parent is the dispatching node itself), and the usage tuples every node
# reports per activation (accumulated so composites read their subtree total).
_INPUT_KEY = "__ngen_input__"
_LAST_KEY = "__ngen_last__"
_USAGE_KEY = "__ngen_usage__"

Usage = tuple[int, int, float]  # tokens_in_context, tokens_total, cost_usd


@dataclasses.dataclass
class _DriveState:
    """Outcome bookkeeping for exactly one drive of one run.

    Owned by a single _drive invocation instead of the Engine, so two
    overlapping drives behind one engine (e.g. via LocalRunService) cannot
    erase each other's pause, cancel, or waiting decisions. The driver rides
    it through the invocation config under the ngen_drive_state key, which
    is how node-side code (emitters, interrupt paths) reaches the same object.
    """

    waiting: dict | None = None  # set by the emitter on waiting_human
    boundary_stop: dict | None = None  # outcome of the last boundary breach
    breach_emitted: set[str] = dataclasses.field(default_factory=set)  # budget records landed


class CompiledGraph:
    """A compiled workflow plus its frozen per-node variant table.

    Attributes:
        root: The workflow class that was compiled.
        variants: Child class path -> resolved variant name for every
            model-calling leaf; recorded in each model_call provenance payload.
        builder: The underlying LangGraph StateGraph; compiled per invocation
            with this graph's own checkpointer.
        wiring: The compiled graph's node table; the streaming driver matches
            update-event keys against it to spot committed activations.
        humans: Waiting node path -> Human class for every wired human leaf;
            resume uses it to validate submissions before continuing.
        children: Wired child path -> child CompiledGraph for composites; the
            observer boundary walks it to resolve any nested node's class.
        saver: This graph's dedicated memory checkpointer; levels must not
            share one, or interrupt resume silently becomes a no-op.
    """

    def __init__(
        self,
        root: type[Workflow],
        variants: dict[str, str],
        builder: Any,
        humans: dict[str, type[Workflow]] | None = None,
        wiring: Any = None,
        children: dict[str, CompiledGraph] | None = None,
    ) -> None:
        self.root = root
        self.variants = variants
        self.builder = builder
        self.wiring = wiring
        self.humans = humans or {}
        self.children = children or {}
        self.saver: Any = None


def _activation_metadata(record: ProvenanceRecord | None) -> RunMetadata | None:
    """Extract the RunMetadata payload of an ok node_activation record, else None."""
    if (
        record is not None
        and record.payload.get("status") == "ok"
        and isinstance(record.payload.get("metadata"), dict)
    ):
        return RunMetadata(**record.payload["metadata"])
    return None


def render_prompt(template: str, dump: dict, node_path: str) -> str:
    """Render a prompt template against an input dump, resolving dotted paths."""

    class _Access(dict):
        def __missing__(self, key: str) -> Any:
            raise KeyError(key)

        def __getitem__(self, key: str) -> Any:
            if key not in self:
                raise KeyError(key)
            return _wrap(dict.__getitem__(self, key))

    try:
        return template.format_map(_Access(dump))
    except (KeyError, AttributeError) as exc:
        raise DataError(f"{node_path}: prompt placeholder {exc} missing from input") from None


def _wrap(value: Any) -> Any:
    """Present dicts as attribute-accessible namespaces for str.format."""
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value


def parse_boolean(reply: str, node_path: str) -> bool:
    """Parse a model reply as the control boolean; unparseable is retryable."""
    normalized = reply.strip().lower()
    if normalized in {"true", "false"}:
        return normalized == "true"
    for token in normalized.replace(",", " ").replace(".", " ").split():
        if token in {"true", "false"}:
            return token == "true"
    raise AgentReplyError(
        f"{node_path}: model reply {reply!r} is not a parseable boolean verdict\n"
        f"last reply: {reply[:REPLY_EXCERPT_CHARS]!r}"
    )


def _strip_code_fence(text: str) -> str:
    """Remove one wrapping markdown code fence, a common real-model habit."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()[1:]  # drop the opening ```/```json line
    if lines and lines[-1].strip().endswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def parse_output(output_type: type[BaseModel], text: str, node_path: str) -> BaseModel:
    """Validate a worker reply against its output schema.

    A wrapping markdown code fence is stripped first; object-shaped schemas
    then parse from JSON, falling back to direct validation so single-value
    outputs work too.
    """
    candidate = _strip_code_fence(text)
    try:
        try:
            return output_type.model_validate_json(candidate)
        except ValidationError:
            return output_type.model_validate(candidate)
    except ValidationError as exc:
        raise AgentReplyError(
            f"{node_path}: reply does not match "
            f"{format_validation_error(output_type, exc)}\n"
            f"last reply: {text[:REPLY_EXCERPT_CHARS]!r}"
        ) from None


def _single_list_field(input_model: type[BaseModel]) -> str | None:
    """The one list field of an input model, for collected fan-in."""
    names = [
        name for name, fi in input_model.model_fields.items() if get_origin(fi.annotation) is list
    ]
    return names[0] if len(names) == 1 else None


def _assembly_plan(
    wiring: _Wiring,
    incoming: dict[str, list[tuple[str, str | None]]],
    dispatched: set[str],
) -> dict[str, tuple]:
    """Per-destination input assembly form, fixed at compile time.

    Kinds: ("entry", None) reads the seeded composite input; ("single", src)
    passes one parent's dump through; ("slots", pairs) assembles named slots;
    ("collect", field, srcs, sort_key) appends parent dumps to the declared
    list field, stable-sorted by the collector's collect_order path;
    ("dispatch", senders) takes the output of whichever node dispatched here,
    since a dispatch target's sender is its effective parent.
    """
    senders = _dispatch_senders(wiring)
    plan: dict[str, tuple] = {}
    for dst, parents in incoming.items():
        if dst != END:
            plan[dst] = _plan_form(wiring, dst, parents, senders)
    for target in dispatched:
        plan.setdefault(target, ("dispatch", sorted(senders.get(target, ()))))
    return plan


def _dispatch_senders(wiring: _Wiring) -> dict[str, set[str]]:
    """Map each conditional branch target to the nodes that may dispatch into it."""
    senders: dict[str, set[str]] = {}
    for src, branches in wiring.conditional:
        for target in branches.values():
            if target != END:
                senders.setdefault(target, set()).add(src)
    return senders


def _plan_form(
    wiring: _Wiring,
    dst: str,
    parents: list[tuple[str, str | None]],
    senders: dict[str, set[str]],
) -> tuple:
    """Classify one destination's assembly form from its incoming edges."""
    ordinary = [(s, f) for s, f in parents if s != START]
    slots = [(s, f) for s, f in ordinary if f is not None]
    if slots:
        return ("slots", slots)
    if len(ordinary) == 1:
        return ("single", ordinary[0][0])
    if ordinary:
        dst_cls = wiring.nodes.get(dst)
        field_name = _single_list_field(dst_cls.input_type) if dst_cls is not None else None
        order = dst_cls.collect_order if dst_cls is not None else None
        return ("collect", field_name, [s for s, _f in ordinary], order)
    if any(s == START for s, _f in parents):
        return ("entry", None)
    return ("dispatch", sorted(senders.get(dst, ())))


def _sort_value(dump: dict, path: str, node_path: str):
    """Read a dotted path off one parent's output dump."""
    value: Any = dump
    try:
        for segment in path.split("."):
            value = value[int(segment)] if isinstance(value, list) else value[segment]
    except (KeyError, IndexError, TypeError) as exc:
        raise DataError(f"{node_path}: collect_order {path!r} missing on an entry") from exc
    return value


def _collect_input(kind: tuple, state: dict, node_path: str) -> dict:
    """Assemble collected fan-in: parent dumps in stable collect_order."""
    field_name, sources, order = kind[1], kind[2], kind[3]
    missing = [src for src in sources if state.get(src) is None]
    if missing:
        raise DataError(f"{node_path}: collector parents not yet written: {missing}")
    entries = [state[src] for src in sources]
    if order is not None:
        entries.sort(key=lambda dump: _sort_value(dump, order, node_path))
    return {field_name: entries}


def _assemble_input(plan: dict[str, tuple], path: str, state: dict, node_path: str) -> Any:
    """Assemble one child's raw input per its compiled form."""
    kind = plan.get(path)
    if kind is None:
        raise DataError(f"{node_path}: no declared input assembly")
    form = kind[0]
    if form == "entry":
        return state[_INPUT_KEY]
    if form == "single":
        source = state.get(kind[1])
        if source is None:
            raise DataError(f"{node_path}: parent output absent when assembling input")
        return source
    if form == "slots":
        missing = [src for src, _f in kind[1] if state.get(src) is None]
        if missing:
            raise DataError(f"{node_path}: slot sources not yet written: {missing}")
        return {field_name: state[src] for src, field_name in kind[1]}
    if form == "collect":
        return _collect_input(kind, state, node_path)
    # dispatch: the sending node's validated output is the input
    last = state.get(_LAST_KEY)
    if last is None or last not in kind[1]:
        raise DataError(f"{node_path}: no dispatching node delivered an input")
    return state[last]


def _select_output(root_path: str, final: dict) -> dict:
    """Pick the terminal's dump: the last writer is the node that hit END."""
    last = final.get(_LAST_KEY)
    if isinstance(last, str) and final.get(last) is not None:
        return final[last]
    raise DataError(f"run finished without a terminal output on {root_path}")


def _wire_static_edges(builder: Any, wiring: _Wiring) -> None:
    """Add static edges verbatim; conditional edges are wired elsewhere.

    Scheduling stays LangGraph-native: nodes fire when a trigger arrives and
    multi-parent joins are restricted to equal-depth parents at compile time
    (validation in ngen_weave.workflow), so no depth-alignment nodes are
    inserted here.
    """
    for src, dst in [(s, d) for s, d, _into in wiring.edges]:
        if dst == END:
            builder.add_edge(src, END)
        else:
            builder.add_edge(src, dst)


def _state_schema(wiring: _Wiring) -> type:
    """Build the EngineState TypedDict: one dict channel per child plus reserved keys."""
    from typing import Annotated, TypedDict

    fields: dict[str, type] = {path: dict for path in wiring.nodes}
    fields[_INPUT_KEY] = dict

    def _last_wins(_old: str, new: str) -> str:
        return new

    fields[_LAST_KEY] = Annotated[str, _last_wins]
    fields[_USAGE_KEY] = Annotated[list, operator.add]
    return TypedDict("EngineState", fields, total=False)  # type: ignore[call-overload]


class Engine:
    """Compile, run, and resume workflows on LangGraph.

    Execution is LangGraph-native: nodes fire when a trigger arrives and
    superstep concurrency is LangGraph's. Determinism comes from compile-time
    shape checks -- equal-depth multi-parent joins (see plans/design/
    loops-and-joins.md), declaration-ordered collect assembly, and
    single-sender dispatch through the last-wins channel. Composites recurse
    eagerly under their own checkpoint namespaces and report accumulated
    subtree usage upward, so per-scope RunMetadata attribution needs no level
    special-casing. Human leaves park the run waiting_human until a resume
    validates the submitted response and continues the superstep.

    Attributes:
        provider: Completion provider every model call goes through; exposes
            default_variant when it carries a model registry.
        store: Sole writer of run state under .ngen-weave/runs/.
        checkpointer: "sqlite" (durable, default) or "memory" (tests).
        db_path: SQLite checkpoint database path.
        artifacts: Optional content-addressed store; workflows declaring
            artifacts persist nothing while it stays unset (tests mostly).
        max_retries: Retries after the initial attempt for AgentReplyError;
        InfraError retries are capped separately by infra_max_retries.
        retry_backoff_ms: Exponential backoff base in milliseconds; each retry
            waits twice the previous delay.
        settings: Run-level settings carrying the run budget; limits live on
            the engine, so resume-after-breach rebuilds it with raised caps.
    """

    def __init__(
        self,
        provider: CompletionProvider,
        store: RunStore,
        checkpointer: str = "sqlite",
        db_path: Path = Path(".ngen-weave/checkpoints.db"),
        max_retries: int = 3,
        infra_max_retries: int = 2,
        retry_backoff_ms: int = 1000,
        artifacts: ArtifactStore | None = None,
        settings: RunSettings | None = None,
    ) -> None:
        if checkpointer not in {"sqlite", "memory"}:
            raise ConfigError(f"checkpointer must be 'sqlite' or 'memory', got {checkpointer!r}")
        self.provider = provider
        self.store = store
        self.checkpointer = checkpointer
        self.db_path = Path(db_path)
        self.max_retries = max_retries
        self.infra_max_retries = infra_max_retries
        self.retry_backoff_ms = retry_backoff_ms
        self._artifacts = artifacts
        self._settings = (
            dataclasses.replace(
                settings,
                checkpointer=checkpointer,
                db_path=db_path,
                max_retries=max_retries,
                infra_max_retries=infra_max_retries,
                retry_backoff_ms=retry_backoff_ms,
            )
            if settings is not None
            else RunSettings(
                checkpointer=checkpointer,
                db_path=db_path,
                max_retries=max_retries,
                infra_max_retries=infra_max_retries,
                retry_backoff_ms=retry_backoff_ms,
            )
        )
        # Engine-level deliberately: cancel requests must be visible to
        # whichever drive is running that run id, not parked per-drive.
        self._cancel_flags: set[str] = set()  # per-run-id cancel requests
        self._compiled: dict[tuple, CompiledGraph] = {}
        self._compiling: set[int] = set()  # ids of classes mid-compilation
        self._memory: Any = None  # root-level memory checkpointer
        self._memory_by_graph: dict[int, Any] = {}  # one saver per graph level

    # --- cooperative cancellation and the activation boundary ------------------

    def cancel(self, run_id: str) -> None:
        """Request cooperative cancellation of run_id at its next boundary.

        A run that is actively driven stops after its current node finishes;
        the driver then persists status "cancelled". A paused or waiting run
        is cancelled immediately, and cancelling an already-terminal run is a
        no-op. If the run resolves to a terminal status between the peek and
        the transition, that terminal outcome wins and the cancel is treated
        as the no-op it races. Same-process only: the flag lives on this
        Engine instance.

        Raises:
            UnknownRunError: Unknown run id.
        """
        run_file = self.store.peek(run_id)
        if run_file is None:
            raise UnknownRunError(run_id)
        if run_file.status in {"completed", "failed", "cancelled"}:
            return  # already terminal; cancelling again is a no-op
        if run_file.status == "running":
            self._cancel_flags.add(run_id)
            return
        # Conditional in SQL: if the run reached a terminal status between the
        # peek above and this update, the update matches no row and returns
        # False -- that race resolves to the terminal status, equivalent to
        # the no-op case; do not retry or warn.
        self.store.set_status(run_id, "cancelled", expected=frozenset({"paused", "waiting_human"}))

    def _at_boundary(
        self,
        state: _DriveState,
        run_file: RunFile,
        node_path: str,
        metadata: RunMetadata | None,
    ) -> bool:
        """Return True when the driver must stop before the next activation.

        Checks run in order -- cancel flag first, then budget, then observers
        (evaluated by the driver's consumption loop right after this hook
        declines to stop); first breach wins, so a budget breach short-circuits
        any observer predicate evaluated at this same boundary. Budget totals
        come from the store's incrementally maintained columns via the run
        file's identity, never from rescanning records; cost_usd compares
        accumulated model-call spend and steps compare node_activation counts
        (a breach fires when observed reaches the limit; a limit of -1, the
        BUDGET_UNLIMITED sentinel, is uncapped). On a budget breach
        this emits exactly one budget_exhausted record naming the crossing
        activation and flips the drive's boundary_stop to a paused outcome
        whose waiting dict carries node_path plus reason; a paused run later
        resumes by
        driving with a None seed on the SAME checkpoint namespace, so
        LangGraph schedules the next node from its persisted channel state
        without replaying committed ones.
        """
        run_id = run_file.run_id
        if run_id in self._cancel_flags:
            state.boundary_stop = {"status": "cancelled"}
            return True
        budget = self._settings.budget
        if budget is None or run_id in state.breach_emitted:
            return False
        cost_usd, activations = self.store.usage_totals(run_id)
        # Treat None OR the -1 sentinel as unlimited BEFORE any comparison;
        # config parsing normalizes -1 away, but directly constructed budgets
        # may still carry it.
        if (
            budget.steps is not None
            and budget.steps != BUDGET_UNLIMITED
            and activations >= budget.steps
        ):
            dimension, limit, observed = "steps", budget.steps, activations
        elif (
            budget.cost_usd is not None
            and budget.cost_usd != BUDGET_UNLIMITED
            and cost_usd >= budget.cost_usd
        ):
            dimension, limit, observed = "cost_usd", budget.cost_usd, cost_usd
        else:
            return False
        state.breach_emitted.add(run_id)
        self._emitter(state, run_id, node_path)(
            "budget_exhausted",
            {"dimension": dimension, "limit": limit, "observed": observed},
        )
        state.boundary_stop = {
            "status": "paused",
            "waiting": {"node_path": node_path, "reason": "budget_exhausted"},
        }
        return True

    def _observe_boundary(
        self,
        state: _DriveState,
        compiled: CompiledGraph,
        run_id: str,
        batch: list[ProvenanceRecord],
    ) -> bool:
        """Evaluate declared observers over this boundary's committed activations.

        One evaluation per committed node_activation record, against that
        record's metadata payload; returns True when any pause fired. An
        observer applies only to the class that declares it, never implicitly
        to nested subgraphs -- a leaf watches its own activation metadata at
        its own boundary, a composite its subtree-aggregated metadata at its
        completion boundary (a completion artifact, so mid-scope subtree
        checks do not exist in v0.2 and mid-run cost control belongs to
        budgets). Two documented no-special-cases: a Human activation's
        metadata has empty usage (cost 0, iterations 1), and a leaf's
        iterations equals its retry attempt count, so gt(iterations, n) on a
        leaf watches retries. Each firing emits one observer_firing record;
        multiple firings at one boundary each land their record and the run
        pauses once via the same boundary_stop mechanism budgets use.
        """
        table = self._observer_table(compiled)
        fired = False
        for record in batch:
            meta = _activation_metadata(record)
            cls = table.get(record.node_path)
            if meta is None or cls is None:
                continue
            for obs in cls.observations:
                pred = obs.predicate
                if not pred.evaluate(meta):
                    continue
                self._emitter(state, run_id, record.node_path)(
                    "observer_firing",
                    {
                        "predicate": pred.describe(),
                        "field": pred.field,
                        "op": pred.op,
                        "value": pred.value,
                        "observed": getattr(meta, pred.field),
                        "action": obs.action,
                    },
                )
                state.boundary_stop = {
                    "status": "paused",
                    "waiting": {
                        "node_path": record.node_path,
                        "reason": "observer_firing",
                        "observer": pred.describe(),
                    },
                }
                fired = True
        return fired

    def _observer_table(self, compiled: CompiledGraph) -> dict[str, type[Workflow]]:
        """Map every full activation node path in the tree to its declaring class."""
        table: dict[str, type[Workflow]] = {}

        def walk(level: CompiledGraph, prefix: str) -> None:
            for path, cls in level.wiring.nodes.items():
                full = f"{prefix}.{path}"
                table[full] = cls
                child = level.children.get(path)
                if child is not None:  # composites recurse into their own graphs
                    walk(child, full)

        walk(compiled, workflow_class_path(compiled.root))
        return table

    # --- compilation ---------------------------------------------------------

    def compile(
        self, wf: type[Workflow], models: dict | None = None, outer_scopes: tuple = ()
    ) -> CompiledGraph:
        """Compile wf into a runnable graph, cached per (class, bindings, scopes).

        build() runs once against the recording adapter; the recorded ops are
        replayed onto the production StateGraph. Model-calling leaves resolve
        their variant before any run starts: exact class-path binding beats
        innermost enclosing composite beats the provider's default variant;
        outer_scopes carries the enclosing composites crossed to reach wf, so
        bindings keyed above wf still govern its leaves. Composite children
        compile eagerly here, so a binding anywhere takes effect before any
        run starts and cyclic composite wiring fails at compile time. The
        resolution freezes into the compiled graph, so resumed runs resolve
        identically and provenance records the variant actually used.
        """
        models = models or {}
        root_path = workflow_class_path(wf)
        key = (
            root_path,
            tuple(sorted(models.items())),
            tuple(workflow_class_path(s) for s in outer_scopes),
        )
        cached = self._compiled.get(key)
        if cached is not None:
            return cached
        if id(wf) in self._compiling:
            raise ConfigError(
                f"cyclic composite wiring through {root_path}; "
                "a workflow cannot wire itself into its own subtree"
            )
        self._compiling.add(id(wf))
        try:
            return self._compile_uncached(wf, models, outer_scopes, key)
        finally:
            self._compiling.discard(id(wf))

    def _compile_uncached(
        self, wf: type[Workflow], models: dict, outer_scopes: tuple, key: tuple
    ) -> CompiledGraph:
        root_path = workflow_class_path(wf)

        recorder = _StateGraphAdapter()
        instance = _instantiate(wf)
        try:
            instance.build(recorder)
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f"build() of {root_path} failed: {exc}") from exc

        wiring = _wiring_from(recorder.ops, recorder.node_classes)
        incoming, dispatched = _incoming(wiring)
        plan = _assembly_plan(wiring, incoming, dispatched)
        default_variant = getattr(self.provider, "default_variant", None) or "default"
        scopes = (wf, *outer_scopes)  # innermost first for resolve_model_variant
        # Full class-path chain from the run root down to wf; every activation
        # inside this graph prefixes its path with it.
        base = ".".join(workflow_class_path(c) for c in reversed(scopes))
        variants = {
            path: resolve_model_variant(cls, scopes, models, default_variant)
            for path, cls in wiring.nodes.items()
            if issubclass(cls, (Worker, Control))
        }
        children = {
            path: self.compile(cls, models, outer_scopes=scopes)
            for path, cls in wiring.nodes.items()
            if cls.run is Workflow.run  # composites recurse; every leaf overrides run
        }
        humans = {path: cls for path, cls in wiring.nodes.items() if issubclass(cls, Human)}
        for path, cls in wiring.nodes.items():
            if cls.run is Workflow.run:  # composites recurse; every leaf overrides run
                humans.update(children[path].humans)
        cell = {
            "plan": plan,
            "variants": variants,
            "root_path": root_path,
            "base": base,
            "instances": {path: _instantiate(cls) for path, cls in wiring.nodes.items()},
            "children": children,
        }

        builder = self._build_production_graph(wiring, recorder.ops, cell)
        compiled = CompiledGraph(
            root=wf,
            variants=variants,
            builder=builder,
            humans=humans,
            wiring=wiring,
            children=children,
        )
        if self.checkpointer == "memory":
            from langgraph.checkpoint.memory import MemorySaver

            compiled.saver = self._memory_by_graph.setdefault(id(compiled), MemorySaver())
        self._compiled[key] = compiled
        return compiled

    def _build_production_graph(self, wiring: _Wiring, ops: list, cell: dict[str, Any]) -> Any:
        """Replay recorded ops onto a StateGraph with per-key channels.

        Static edges are wired verbatim; multi-parent targets fire once per
        superstep under LangGraph's scheduling, with equal-depth parents
        enforced at compile time. Conditional edges stay direct: dispatch
        re-entry is their purpose.
        """
        from langgraph.graph import StateGraph

        builder = StateGraph(_state_schema(wiring))
        node_fns = {path: self._node_fn(cls, cell) for path, cls in wiring.nodes.items()}
        for path, fn in node_fns.items():
            builder.add_node(path, fn)
        _wire_static_edges(builder, wiring)
        for op in ops:
            if op.name != "add_conditional":
                continue
            src_path, mapping_pairs = op.args
            mapping = dict(mapping_pairs)
            router = self._router_fn(src_path, op.router, mapping)
            builder.add_conditional_edges(src_path, router, mapping)
        return builder

    def _router_fn(self, src_path: str, router: Callable[[dict], str], mapping: dict) -> Callable:
        """Wrap an author router so unmapped returns are runtime DataErrors."""

        def routed(state: dict) -> str:
            value = router(state)
            if value not in mapping:
                raise DataError(
                    f"{src_path}: router returned {value!r}, absent from its branch map"
                )
            return value

        return routed

    def _node_fn(self, cls: type[Workflow], cell: dict[str, Any]) -> Callable:
        """Build the executable node function for one wired child."""
        path = workflow_class_path(cls)

        async def fn(state: dict, config: RunnableConfig) -> dict:
            run_id = config["configurable"].get("ngen_run_id", config["configurable"]["thread_id"])
            node_path = join_path(cell["base"], path)
            drive_state = config["configurable"]["ngen_drive_state"]
            emit = self._emitter(drive_state, run_id, node_path)
            started = time.perf_counter()
            usage: list[Usage] = []

            raw = _assemble_input(cell["plan"], path, state, node_path)
            try:
                model = cls.input_type.model_validate(raw)
            except ValidationError as exc:
                emit("node_activation", {"status": "invalid"})
                raise DataError(
                    f"{node_path}: input fails {cls.input_type.__name__}: {exc}"
                ) from None

            ctx = RunContext(run_id=run_id, node_path=node_path, emit=emit, provider=self.provider)
            try:
                if cls.run is Workflow.run:
                    # Composite: the child graph runs to completion and its
                    # subtree's usage lands in `usage`, so this node's single
                    # ok record attributes the whole scope. Failures propagate
                    # untouched; leaves inside already applied retry policy.
                    attempt = 1
                    output = await self._activate_composite(
                        cls, path, model, ctx, usage, cell, config
                    )
                else:
                    instance = cell["instances"][path]
                    if isinstance(instance, Human):
                        attempt = 1
                        output = await self._execute_human(cls, instance, model, ctx, config)
                    else:
                        try:
                            output, attempt = await self._leaf_with_retries(
                                cls, path, model, ctx, usage, cell
                            )
                        except ReturnToReviewError:
                            # The gate routed a denied tool call back to human
                            # review: park as waiting_human with the review-reason
                            # contract and halt via interrupt(), so resume carries
                            # Command(resume=...) through the same checkpoint. On
                            # replay the whole node re-executes; the corrected
                            # permission set lets the gate pass this time.
                            emit(
                                "node_activation",
                                {"status": "waiting_human", "reason": "returned_to_review"},
                            )
                            interruption = {
                                "node_path": ctx.node_path,
                                "reason": "returned_to_review",
                            }
                            drive_state.waiting = dict(interruption)
                            from langgraph.types import interrupt

                            interrupt(interruption)
                            output, attempt = await self._leaf_with_retries(
                                cls, path, model, ctx, usage, cell
                            )
            except DataError:
                emit("node_activation", {"status": "invalid"})
                raise

            # Declared artifacts persist before the ok record lands, so the
            # scope's completion implies its artifacts are already on disk.
            if cls.artifacts and self._artifacts is not None:
                self._write_artifacts(cls, model.model_dump(), output.model_dump(), ctx)

            metadata = RunMetadata(
                iterations=attempt,
                tokens_in_context=sum(u[0] for u in usage),
                tokens_total=sum(u[1] for u in usage),
                cost_usd=sum(u[2] for u in usage),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                last_output_valid=True,
            )
            emit("node_activation", {"status": "ok", "metadata": dataclasses.asdict(metadata)})
            return {path: output.model_dump(), _LAST_KEY: path, _USAGE_KEY: usage}

        return fn

    def _write_artifacts(
        self,
        cls: type[Workflow],
        input_dump: dict,
        output_dump: dict,
        ctx: RunContext,
    ) -> None:
        """Persist each declared field of a successful activation's output.

        Every declared name must exist on output_type; import-time validation
        already guarantees that, so this only serializes, stores, links, and
        emits one artifact_write record per field naming the producing
        activation and the input hashes it was computed from.
        """
        assert self._artifacts is not None
        input_hashes = {name: hash_value(value) for name, value in input_dump.items()}
        for name in cls.artifacts:
            data = json.dumps(output_dump[name], sort_keys=True, ensure_ascii=False).encode("utf-8")
            meta = ArtifactMeta(
                run_id=ctx.run_id,
                node_path=ctx.node_path,
                name=name,
                input_hashes=input_hashes,
            )
            record = self._artifacts.put(data, meta)
            self._artifacts.link_meta(record)
            ctx.emit(
                "artifact_write",
                {
                    "artifact_sha256": record.sha256,
                    "name": name,
                    "input_hashes": input_hashes,
                },
            )

    async def _execute_human(
        self,
        cls: type[Workflow],
        instance: Human,
        model: BaseModel,
        ctx: RunContext,
        config: RunnableConfig,
    ) -> BaseModel:
        """Run one human activation: write the artifact, wait, validate.

        On first entry the engine generates response slots from state_type,
        seeds them via prefill, writes the two-section YAML artifact, and
        interrupts. On replay after submission (the resuming config flag marks
        it) the side effects are skipped and interrupt() returns the submitted
        response instead. The validated state passes through as the output
        unless the subclass overrides transform().
        """
        import yaml
        from langgraph.types import interrupt

        if not config["configurable"].get("resuming"):
            context_dump = model.model_dump()
            slots = build_response_slots(cls.state_type)
            apply_prefill(slots, context_dump, dict(cls.prefill or {}))
            name = ctx.node_path.replace(".", "__")
            artifact = self.store.save_review_artifact(
                ctx.run_id,
                name,
                yaml.safe_dump({"context": context_dump, "response": slots}, sort_keys=False),
            )
            ctx.emit("node_activation", {"status": "waiting_human", "artifact": str(artifact)})
        response = interrupt({"node_path": ctx.node_path})
        state = validate_completion(cls.state_type, dict(response))
        if type(instance).transform is Human.transform:
            raw = state.model_dump()
        else:
            produced = instance.transform(model, state)
            raw = produced.model_dump() if isinstance(produced, BaseModel) else produced
        try:
            return cls.output_type.model_validate(raw)
        except ValidationError as exc:
            raise DataError(
                f"{ctx.node_path}: human output does not match {cls.output_type.__name__}: {exc}"
            ) from None

    async def _activate_composite(
        self,
        cls: type[Workflow],
        path: str,
        model: BaseModel,
        ctx: RunContext,
        usage: list[Usage],
        cell: dict[str, Any],
        config: RunnableConfig,
    ) -> BaseModel:
        """Run one composite child's compiled graph to completion.

        The child activates under its own checkpoint namespace keyed by its
        accumulated node path. Its nodes report usage tuples through the
        child graph's accumulated state channel, which lands here as the
        subtree total and folds into the caller's list, so the parent's
        per-scope metadata sums the child's own records plus all descendants'.
        The child's terminal dump validates against the composite's
        output_type before it travels over the parent edge.
        """
        compiled = cell["children"][path]
        attempt_ns = f"attempt-{config['configurable'].get('run_attempt', 1)}"
        # An interrupt continuation forwards the submitted response into the
        # child graph as its resume command; a first entry seeds ordinary
        # input. A child that ends interrupted re-raises as this node's own
        # interrupt, pausing every enclosing graph up to the root.
        resume_value = config["configurable"].get("ngen_resume_value")
        if resume_value is not None:
            from langgraph.types import Command

            seed: Any = Command(resume=resume_value)
        else:
            seed = {_INPUT_KEY: model.model_dump()}
        final = await self._invoke(
            compiled,
            seed,
            ctx.run_id,
            checkpoint_ns=f"{attempt_ns}:{ctx.node_path}",
            resuming=bool(config["configurable"].get("resuming")),
            resume_value=resume_value,
            nested=True,
            drive_state=config["configurable"]["ngen_drive_state"],
        )
        if final.get("__interrupt__"):
            # Register a real interrupt on every enclosing graph so a root
            # Command(resume=...) can target it; the framework-delivered value
            # is ignored, the submitted response travels through config.
            from langgraph.types import interrupt

            interrupt(None)
        usage.extend(final.get(_USAGE_KEY, ()))
        output_dump = _select_output(path, final)
        try:
            validated = cls.output_type.model_validate(output_dump)
        except ValidationError as exc:
            raise DataError(
                f"{ctx.node_path}: composite output does not match "
                f"{cls.output_type.__name__}: {exc}"
            ) from None
        return validated

    async def _leaf_with_retries(
        self,
        cls: type[Workflow],
        path: str,
        model: BaseModel,
        ctx: RunContext,
        usage: list[Usage],
        cell: dict[str, Any],
    ) -> tuple[BaseModel, int]:
        """Execute one leaf under the retry policy, returning output and attempt count.

        AgentReplyError retries up to max_retries; InfraError (transport-level
        failures such as a wedged subprocess) retries only up to
        infra_max_retries with exponential backoff, then re-raises with a
        diagnosis so a persistent wedge fails the run loudly instead of
        silently respawning until the trial timeout. DataError failures
        (including DeniedToolError) never retry. The attempt count is the
        leaf's iterations for its ok record's metadata.
        """
        attempt = 0
        infra_attempt = 0
        while True:
            attempt += 1
            try:
                return await self._execute_leaf(cls, path, model, ctx, usage, cell), attempt
            except AgentReplyError:
                if attempt > self.max_retries:
                    raise
                ctx.emit("node_activation", {"status": "retry", "attempt": attempt})
                await _sleep(self.retry_backoff_ms * 2 ** (attempt - 1) / 1000)
            except InfraError as exc:
                infra_attempt += 1
                if infra_attempt > self.infra_max_retries:
                    raise InfraError(
                        f"{path}: persistent infrastructure failure after "
                        f"{infra_attempt} attempts; failing loudly rather than "
                        f"retrying (a wedged subprocess, dead endpoint, or "
                        f"blocked dialog indicates an environment fault, not a "
                        f"transient one)\ncause: {exc}"
                    ) from exc
                ctx.emit("node_activation", {"status": "retry", "attempt": attempt})
                await _sleep(self.retry_backoff_ms * 2 ** (attempt - 1) / 1000)

    async def _execute_leaf(
        self,
        cls: type[Workflow],
        path: str,
        model: BaseModel,
        ctx: RunContext,
        usage: list[Usage],
        cell: dict[str, Any],
    ) -> BaseModel:
        """Execute one leaf activation: workers render prompts, controls route.

        Workers render their template against the validated input and parse
        the reply into output_type. Controls use decide() when overridden,
        otherwise render their prompt and parse the reply as the boolean;
        either way the verdict lands in the validated pass field so routers,
        provenance, and per-scope metadata read the same value. Other leaves
        delegate to their own run().
        """
        instance = cell["instances"][path]
        variant = cell["variants"].get(path)

        if isinstance(instance, Worker):
            completion = await self._complete(instance.prompt or "", model, ctx, variant, usage)
            self._emit_model_call(ctx, variant, completion)
            return parse_output(cls.output_type, completion.text, ctx.node_path)

        if isinstance(instance, Control):
            if type(instance).decide is not Control.decide:
                verdict = instance.decide(model)
            else:
                completion = await self._complete(instance.prompt or "", model, ctx, variant, usage)
                self._emit_model_call(ctx, variant, completion)
                verdict = parse_boolean(completion.text, ctx.node_path)
            try:
                return cls.output_type(**{"pass": verdict})
            except ValidationError as exc:
                raise DataError(
                    f"{ctx.node_path}: control output construction failed: {exc}"
                ) from None

        return await instance.run(model, ctx)

    async def _complete(
        self,
        template: str,
        model: BaseModel,
        ctx: RunContext,
        variant: str | None,
        usage: list[Usage],
    ):
        text = render_prompt(template, model.model_dump(), ctx.node_path)
        message = [{"role": "user", "content": text}]
        completion = await ctx.provider.complete(message, variant=variant)
        usage.append((completion.tokens_in_context, completion.tokens_total, completion.cost_usd))
        return completion

    @staticmethod
    def _emit_model_call(ctx: RunContext, variant: str | None, completion: Any) -> None:
        ctx.emit(
            "model_call",
            {
                "variant": variant,
                "tokens_total": completion.tokens_total,
                "cost_usd": completion.cost_usd,
            },
        )

    def _emitter(
        self, state: _DriveState, run_id: str, node_path: str
    ) -> Callable[[str, dict], None]:
        """Provenance sink for one activation; unconditional, author-invisible.

        The owning drive's state rides in so the closure can register a
        waiting_human halt on that drive instead of a shared Engine attribute.
        """

        def emit(kind: str, payload: dict) -> None:
            if payload.get("status") == "waiting_human":
                state.waiting = {"node_path": node_path, "artifact": payload.get("artifact")}
            record = ProvenanceRecord(
                version=PROVENANCE_VERSION,
                run_id=run_id,
                node_path=node_path,
                kind=kind,  # type: ignore[arg-type]
                ts=datetime.now(UTC).isoformat(),
                payload=payload,
            )
            self.store.append(run_id, record)

        return emit

    # --- invocation ----------------------------------------------------------

    async def run(
        self, wf: type[Workflow], input: BaseModel, models: dict | None = None
    ) -> RunResult:
        """Run wf from scratch on the validated input and return the outcome."""
        compiled = self.compile(wf, models)
        run_id = self.store.create(workflow_class_path(wf), input.model_dump())
        return await self._drive(compiled, {_INPUT_KEY: input.model_dump()}, run_id)

    async def resume(self, run_id: str, payload: dict | None = None) -> RunResult:
        """Continue run_id from its checkpoint.

        Resuming a completed run is a no-op returning the stored output.
        A waiting_human run takes the submitted response from payload (remote
        JSON form) or from the artifact file on disk when payload is None
        (local YAML form); both carry identical payloads. The response is
        validated before anything moves, an artifact_write record captures
        its hash, and the interrupted superstep continues under the same
        checkpoint namespace. A waiting whose latest record carries reason
        "returned_to_review" (a permission gate's return_to_review policy)
        has no human slots to validate; the optional payload rides along and
        the interrupted leaf replays under its corrected permissions. A
        paused run (budget breach) drives with a None seed on the SAME
        namespace: limits live in engine settings rebuilt per
        invocation, so raising the cap takes effect without any payload. A
        failed or otherwise stopped run re-executes from the top under a fresh
        namespace, seeded with its stored input.
        An explicit payload on a non-waiting run is a ConfigError.
        """
        run_file = self.store.load(run_id)
        if payload is not None and run_file.status != "waiting_human":
            raise ConfigError(f"run {run_id} is not waiting for human input")
        cls = registry_get(run_file.workflow)
        cached = next((c for c in self._compiled.values() if c.root is cls), None)
        compiled = cached if cached is not None else self.compile(cls)
        if run_file.status == "completed":
            assert run_file.output is not None
            return RunResult(
                run_id, "completed", cls.output_type.model_validate(run_file.output), None
            )
        if run_file.status == "waiting_human":
            node_path, info = self._latest_waiting(run_file)
            if info.get("reason") == "returned_to_review":
                # A return_to_review pause parks at an AgentNode's interrupt;
                # there are no human slots to validate, so the payload (the
                # operator's optional review verdict) rides along unused and
                # the resumed superstep replays the guarded leaf. The gate
                # re-checks whatever permission set the class now carries.
                return await self._drive(
                    compiled, None, run_id, resume_payload=payload or {"reviewed": True}
                )
            human_key = max(
                (k for k in compiled.humans if node_path == k or node_path.endswith("." + k)),
                key=len,
                default=None,
            )
            if human_key is None:
                raise ConfigError(f"waiting node {node_path} is not a human node")
            human_cls = compiled.humans[human_key]
            if payload is not None:
                response = payload
            else:
                data = self.store.read_review_artifact(Path(info["artifact"]))
                response = data.get("response")
                if not isinstance(response, dict):
                    raise ConfigError(
                        f"review artifact {info['artifact']} has no submitted response"
                    )
            # Rejection leaves the run waiting; nothing else about it changes.
            validate_completion(human_cls.state_type, dict(response))
            digest = hashlib.sha256(json.dumps(response, sort_keys=True).encode()).hexdigest()
            # Throwaway state: this emitter only lands an artifact_write record,
            # which never registers a waiting halt.
            self._emitter(_DriveState(), run_id, node_path)(
                "artifact_write",
                {"artifact": info["artifact"], "artifact_sha256": digest},
            )
            run_file = self.store.load(run_id)  # reload above emitter's save
            run_file.submissions[node_path] = response
            self.store.save(run_file)
            return await self._drive(compiled, None, run_id, resume_payload=response)
        if run_file.status == "paused":
            # Budget pause: no payload path; the None-seed drive resumes from
            # the latest checkpoint of the existing namespace.
            return await self._drive(compiled, None, run_id, boundary_resume=True)
        return await self._drive(compiled, {_INPUT_KEY: run_file.input}, run_id)

    @staticmethod
    def _latest_waiting(run_file: Any) -> tuple[str, dict]:
        """Return (node_path, payload) of the most recent waiting_human record."""
        for record in reversed(run_file.records):
            if record.kind == "node_activation" and record.payload.get("status") == "waiting_human":
                return record.node_path, record.payload
        raise ConfigError(f"run {run_file.run_id} is waiting_human without a waiting record")

    async def _drive(
        self,
        compiled: CompiledGraph,
        seed: dict | None,
        run_id: str,
        *,
        resume_payload: dict | None = None,
        boundary_resume: bool = False,
    ) -> RunResult:
        """Invoke the graph, then write the terminal transition to the run file.

        A completed run also emits the root scope's node_activation record on
        the root class path, so every level of nesting carries per-scope
        RunMetadata: composites report from their own node functions, the root
        reports here once its whole subtree succeeded. A resume_payload drive
        continues the interrupted attempt under its existing checkpoint
        namespace instead of opening a new one; the resuming flag tells human
        nodes to skip artifact side effects on replay. The root stream is
        consumed superstep by superstep (stream_mode="updates",
        subgraphs=True): after every committed activation _at_boundary decides
        whether to stop, and a stop leaves the last checkpoint sitting at the
        boundary because that superstep completed cleanly -- a boundary resume
        then drives with a None seed on the same namespace.
        """
        from langgraph.types import Command

        status: RunStatus = "failed"
        error: dict[str, str] | None = None
        output_dump: dict | None = None
        waiting_info: dict | None = None
        sink: list[Usage] = []
        started = time.perf_counter()
        # One drive, one state: pause/cancel/waiting outcomes accumulate here
        # so overlapping drives on this engine cannot erase each other's.
        state = _DriveState()
        # Each fresh drive gets a new checkpoint namespace: LangGraph does not
        # reschedule a node that raised, so replaying the old namespace would
        # end immediately. Interrupt resumes deliberately reuse the namespace,
        # and so do boundary (paused/cancelled) resumes: attempt-<n> stays put.
        run_file = self.store.load(run_id)
        if resume_payload is None and not boundary_resume:
            run_file.attempts += 1
            self.store.save(run_file)
        attempt_ns = f"attempt-{run_file.attempts}"
        invoke_seed: Any = seed
        if resume_payload is not None:
            invoke_seed = Command(resume=resume_payload)
        try:
            final = await self._consume_root(
                compiled,
                invoke_seed,
                run_id,
                checkpoint_ns=attempt_ns,
                resuming=resume_payload is not None,
                resume_value=resume_payload,
                state=state,
            )
            sink.extend(final.get(_USAGE_KEY, ()))
            status, output_dump, waiting_info = self._resolve_outcome(state, compiled, final)
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
        if status == "completed":
            # The root workflow is not a node in its own graph, so artifacts
            # it declares persist here, beside its scope's ok record.
            self._emit_root_scope(compiled, run_id, output_dump or {}, started, sink, state)
        run_file = self.store.load(run_id)  # reload: nodes appended records meanwhile
        run_file.status = status
        run_file.error = error
        run_file.output = output_dump
        self.store.save(run_file)
        if status == "paused":
            return RunResult(run_id, status, None, waiting_info)
        # Every non-paused drive outcome (terminal or waiting_human) clears
        # any pending cancel request: later drives start clean. A paused run
        # keeps its flag so the request still applies when the drive resumes.
        self._cancel_flags.discard(run_id)
        if status == "waiting_human":
            assert waiting_info is not None
            return RunResult(run_id, status, None, waiting_info)
        if status == "cancelled":
            return RunResult(run_id, status, None, None)
        if error is not None:
            return RunResult(run_id, status, None, None)
        return RunResult(
            run_id, status, compiled.root.output_type.model_validate(output_dump), None
        )

    def _resolve_outcome(
        self, state: _DriveState, compiled: CompiledGraph, final: dict[str, Any]
    ) -> tuple[RunStatus, dict | None, dict | None]:
        """Classify the consumed stream's outcome: waiting, boundary, or completed.

        A human halt resolves through the drive's waiting flag (set by its
        emitters or by an interrupt path); a boundary breach through the
        drive's boundary_stop outcome (_at_boundary writes it there -- cancel
        flips to "cancelled", budget pauses carry the waiting contract with
        them); anything else must select and validate a terminal output.
        """
        if state.waiting is not None:
            return "waiting_human", None, dict(state.waiting)
        if state.boundary_stop is not None:
            return state.boundary_stop["status"], None, state.boundary_stop.get("waiting")
        output_dump = _select_output(workflow_class_path(compiled.root), final)
        compiled.root.output_type.model_validate(output_dump)
        return "completed", output_dump, None

    def _emit_root_scope(
        self,
        compiled: CompiledGraph,
        run_id: str,
        output_dump: dict,
        started: float,
        sink: list[Usage],
        state: _DriveState,
    ) -> None:
        """Persist root-scope artifacts and the root class path's ok record."""
        root_path = workflow_class_path(compiled.root)
        if compiled.root.artifacts and self._artifacts is not None:
            self._write_artifacts(
                compiled.root,
                self.store.load(run_id).input,
                output_dump,
                RunContext(
                    run_id=run_id,
                    node_path=root_path,
                    emit=self._emitter(state, run_id, root_path),
                    provider=self.provider,
                ),
            )
        metadata = RunMetadata(
            iterations=1,
            tokens_in_context=sum(u[0] for u in sink),
            tokens_total=sum(u[1] for u in sink),
            cost_usd=sum(u[2] for u in sink),
            elapsed_ms=int((time.perf_counter() - started) * 1000),
            last_output_valid=True,
        )
        self._emitter(state, run_id, root_path)(
            "node_activation", {"status": "ok", "metadata": dataclasses.asdict(metadata)}
        )
        # Root observers fire here, after the whole graph succeeded: they are
        # structural or informational and cannot pause anything -- run-level
        # caps belong to budgets (C1). The status stays completed either way.
        for obs in compiled.root.observations:
            pred = obs.predicate
            if not pred.evaluate(metadata):
                continue
            self._emitter(state, run_id, root_path)(
                "observer_firing",
                {
                    "predicate": pred.describe(),
                    "field": pred.field,
                    "op": pred.op,
                    "value": pred.value,
                    "observed": getattr(metadata, pred.field),
                    "action": obs.action,
                },
            )

    async def _consume_root(
        self,
        compiled: CompiledGraph,
        invoke_seed: Any,
        run_id: str,
        *,
        checkpoint_ns: str,
        resuming: bool,
        resume_value: Any | None,
        state: _DriveState,
    ) -> dict[str, Any]:
        """Consume the root graph's stream until it ends or a boundary stops it.

        Each qualifying update event means one or more node activations just
        committed -- their records are already in the store, since node
        functions append before the superstep surfaces. The stream is pinned
        to stream_mode="updates" with subgraphs=True; langgraph consumes it
        pull-driven, so a boundary break after a committed superstep means
        neither that superstep's successor nor any later one ever runs --
        the boundary lands exactly where _at_boundary decided, without
        raising anything through the graph and without interrupt(). The
        accumulated update dicts double as the completed-run final state;
        when no qualifying event surfaces at all (e.g. a boundary resume
        whose remaining work already ran inside one composite, leaving only
        END scheduled), the checkpointer's latest snapshot supplies the
        terminal channel state instead.

        Granularity note (approved deviation): composites nest via manual
        graph.ainvoke inside their node functions (_activate_composite), so
        depth>=2 activations never surface in this root stream even with
        subgraphs=True -- nested activations instead pause at the enclosing
        composite's commit boundary. Flat graphs get exact per-activation
        boundaries; budget totals are identical either way. The same deviation
        covers observers: depth>=2 activations are observed together with
        their enclosing composite at that composite's commit boundary.
        """
        config = self._invocation_config(run_id, checkpoint_ns, resuming, resume_value, state)
        async with self._open_graph(compiled) as graph:
            final: dict[str, Any] = {}
            committed_before = self.store.usage_totals(run_id)[1]
            stream = graph.astream(
                invoke_seed, config=config, stream_mode="updates", subgraphs=True
            )
            try:
                async for namespace, chunk in stream:
                    if namespace != ():  # subgraph events cannot occur under manual nesting
                        continue
                    keys = set(chunk)
                    if "__interrupt__" in keys or compiled.wiring.nodes.keys().isdisjoint(keys):
                        # Halts resolve through the drive's waiting state;
                        # every other update key is a wiring node.
                        continue
                    self._merge_update(final, chunk)
                    committed_now = self.store.usage_totals(run_id)[1]
                    fresh, committed_before = committed_now - committed_before, committed_now
                    if fresh <= 0:
                        # A boundary-resumed drive replays the interrupted
                        # superstep's update from cached checkpoint writes: the
                        # node did not re-execute (no new records), so nothing
                        # committed at this chunk. It is not a boundary -- feeding
                        # its stale metadata to _at_boundary/_observe_boundary
                        # would re-fire pause decisions forever.
                        continue
                    # One superstep can commit several activations when a composite
                    # encloses them; each is observed together at this boundary.
                    batch = self.store.node_activations_tail(run_id, fresh)
                    last = batch[-1] if batch else None
                    header = self.store.peek(run_id)
                    if self._at_boundary(
                        state,
                        header,
                        last.node_path if last else "",
                        _activation_metadata(last),
                    ):
                        break
                    if self._observe_boundary(state, compiled, run_id, batch):
                        break
            finally:
                await stream.aclose()
            if not final:
                # Nothing surfaced this consume: either nothing ran at all, an
                # interrupt halted before any commit (waiting_human), or every
                # remaining root node already committed behind one composite
                # and only END stayed scheduled. Read the checkpointer's
                # latest snapshot so these cases still classify; a genuinely
                # broken graph errors on its own terms. Querying by bare
                # thread id matters here: aget_state over the invocation
                # config would carry checkpoint_ns="attempt-N" and LangGraph
                # then resolves it as a subgraph namespace, raising "Subgraph
                # attempt-N not found" instead of returning root state.
                snapshot = await graph.aget_state(
                    {"configurable": {"thread_id": f"{run_id}:{checkpoint_ns}"}}
                )
                return dict(snapshot.values or {})
            return final

    @staticmethod
    def _merge_update(final: dict[str, Any], chunk: dict[str, Any]) -> None:
        """Fold one committed superstep's updates onto the final-state accumulator.

        An "updates" event maps graph node name -> that node function's return
        dict; the return dict itself carries the channel writes (child dumps,
        last-writer marker, usage tuples), which is what downstream consumers
        of the accumulated state read.
        """
        for writes in chunk.values():
            if not isinstance(writes, dict):
                continue
            for key, value in writes.items():
                if key == _USAGE_KEY:
                    final[key] = [*final.get(key, ()), *value]
                else:
                    final[key] = value

    def _invocation_config(
        self,
        run_id: str,
        checkpoint_ns: str,
        resuming: bool,
        resume_value: Any,
        drive_state: _DriveState | None = None,
    ) -> dict:
        """Build the LangGraph configurable shared by invoke and stream paths.

        checkpoint_ns isolates drive attempts and nested activations under the
        same run id. langgraph ignores a caller-supplied checkpoint_ns at
        top-level invocation, so the root graph threads on the raw run id;
        nested graphs derive a deterministic thread id from the attempt number
        plus full node path, which a resumed run regenerates exactly.
        drive_state is the owning drive's outcome bookkeeping; node functions
        read it back out of config so emitters and interrupt paths mutate
        that drive's state, never an Engine-level attribute.
        """
        thread_id = f"{run_id}:{checkpoint_ns}" if checkpoint_ns else run_id
        return {
            "configurable": {
                "thread_id": thread_id,
                "ngen_run_id": run_id,
                "checkpoint_ns": checkpoint_ns,
                "resuming": resuming,
                "ngen_resume_value": resume_value,
                "ngen_drive_state": drive_state,
                "run_attempt": int(checkpoint_ns.split(":")[0].split("-")[1])
                if checkpoint_ns
                else 1,
            }
        }

    @asynccontextmanager
    async def _open_graph(self, compiled: CompiledGraph):
        """Compile per-invocation graph under this level's checkpointer.

        The memory branch keeps one dedicated saver per compiled graph level;
        levels must never share one or interrupt resume becomes a no-op. The
        sqlite branch opens its connection per invocation via the saver's own
        context manager.
        """
        if self.checkpointer == "memory":
            saver = compiled.saver
            if saver is None:
                from langgraph.checkpoint.memory import MemorySaver

                saver = self._memory  # root graph compiled before this assignment
                if saver is None:
                    saver = self._memory = MemorySaver()
                    compiled.saver = saver
            yield compiled.builder.compile(checkpointer=saver)
            return
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with AsyncSqliteSaver.from_conn_string(str(self.db_path)) as saver:
            yield compiled.builder.compile(checkpointer=saver)

    async def _invoke(
        self,
        compiled: CompiledGraph,
        seed: Any,
        run_id: str,
        *,
        checkpoint_ns: str = "",
        resuming: bool = False,
        resume_value: Any = None,
        nested: bool = False,
        drive_state: _DriveState | None = None,
    ) -> dict:
        """Invoke the builder's graph under the run's checkpoint thread.

        This plain ainvoke path serves composite nesting: the driver no longer
        consumes it -- _drive streams the root graph itself. Nested graphs get
        their own thread id derived from the run id and namespace (see
        _invocation_config), which is what lets a depth-2 interrupt resume
        against the child graph's state. resuming marks an interrupt
        continuation so human nodes skip artifact side effects on replay;
        resume_value carries the submitted response down into nested graphs.
        Usage totals travel back through the graph's accumulated state channel,
        never through config.
        """
        thread_id = f"{run_id}:{checkpoint_ns}" if nested else run_id
        config = self._invocation_config(run_id, checkpoint_ns, resuming, resume_value, drive_state)
        config["configurable"]["thread_id"] = thread_id
        async with self._open_graph(compiled) as graph:
            return await graph.ainvoke(seed, config=config)
