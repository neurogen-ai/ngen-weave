"""LangGraph compilation and sequential execution.

Engine.compile turns a workflow class into a runnable graph: build() runs
once against the recording GraphBuilder adapter, and the recorded wiring is
replayed onto a production StateGraph whose per-key channels hold each child's
validated-output dump. Identity relays align parent depths so multi-parent
fan-in targets fire exactly once, after every parent has written; sequential
semantics stay deterministic at any shape. Node functions assemble each
child's input per its declared fan-in form, validate at the boundary, execute
leaves, and emit provenance unconditionally; authors write zero logging code.
Composites recurse: a composite child compiles eagerly to its own graph,
activates under its own checkpoint namespace, and reports its subtree's
accumulated usage upward, so per-scope RunMetadata attribution is correct at
any depth without special-casing levels.

Classes:
    CompiledGraph: A compiled workflow plus its frozen per-node variant table.
    Engine: Compile, run, and resume workflows on LangGraph.
"""

from __future__ import annotations

import dataclasses
import operator
import time
from asyncio import sleep as _sleep  # engine-owned alias; tests patch this
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, get_origin

try:  # langchain-core ships with langgraph; needed for config injection
    from langchain_core.runnables import RunnableConfig
except ImportError:  # pragma: no cover
    RunnableConfig = Any  # type: ignore[misc,assignment]

from pydantic import BaseModel, ValidationError

from ngen_weave.engine.state import RunResult, RunStatus
from ngen_weave.engine.store import RunStore
from ngen_weave.errors import ConfigError, DataError, InfraError
from ngen_weave.models.provider import CompletionProvider
from ngen_weave.provenance import (
    PROVENANCE_VERSION,
    ProvenanceRecord,
    RunMetadata,
    join_path,
)
from ngen_weave.registry import get as registry_get
from ngen_weave.workflow import (
    END,
    START,
    Control,
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


class CompiledGraph:
    """A compiled workflow plus its frozen per-node variant table.

    Attributes:
        root: The workflow class that was compiled.
        variants: Child class path -> resolved variant name for every
            model-calling leaf; recorded in each model_call provenance payload.
        builder: The underlying LangGraph StateGraph; compiled per invocation
            with the engine's checkpointer.
    """

    def __init__(self, root: type[Workflow], variants: dict[str, str], builder: Any) -> None:
        self.root = root
        self.variants = variants
        self.builder = builder


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
    """Parse a model reply as the control boolean; unparseable is DataError."""
    normalized = reply.strip().lower()
    if normalized in {"true", "false"}:
        return normalized == "true"
    for token in normalized.replace(",", " ").replace(".", " ").split():
        if token in {"true", "false"}:
            return token == "true"
    raise DataError(f"{node_path}: model reply {reply!r} is not a parseable boolean verdict")


def parse_output(output_type: type[BaseModel], text: str, node_path: str) -> BaseModel:
    """Validate a worker reply against its output schema.

    Object-shaped schemas parse from JSON first; on failure the raw text is
    validated directly, so single-value outputs work too.
    """
    try:
        try:
            return output_type.model_validate_json(text)
        except ValidationError:
            return output_type.model_validate(text)
    except ValidationError as exc:
        raise DataError(
            f"{node_path}: output does not match {output_type.__name__}: {exc}"
        ) from None


def _single_list_field(input_model: type[BaseModel]) -> str | None:
    """The one list field of an input model, for collected fan-in."""
    names = [
        name
        for name, fi in input_model.model_fields.items()
        if get_origin(fi.annotation) is list
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
    ("collect", field, srcs) appends parent dumps to the declared list field;
    ("dispatch", senders) takes the output of whichever node dispatched here,
    since a dispatch target's sender is its effective parent.
    """
    senders: dict[str, set[str]] = {}
    for src, branches in wiring.conditional:
        for target in branches.values():
            if target != END:
                senders.setdefault(target, set()).add(src)

    plan: dict[str, tuple] = {}
    for dst, parents in incoming.items():
        if dst == END:
            continue
        ordinary = [(s, f) for s, f in parents if s != START]
        slots = [(s, f) for s, f in ordinary if f is not None]
        if slots:
            plan[dst] = ("slots", slots)
        elif len(ordinary) == 1:
            plan[dst] = ("single", ordinary[0][0])
        elif ordinary:
            dst_cls = wiring.nodes.get(dst)
            field_name = _single_list_field(dst_cls.input_type) if dst_cls is not None else None
            plan[dst] = ("collect", field_name, [s for s, _f in ordinary])
        elif any(s == START for s, _f in parents):
            plan[dst] = ("entry", None)
        else:
            plan[dst] = ("dispatch", sorted(senders.get(dst, ())))
    for target in dispatched:
        plan.setdefault(target, ("dispatch", sorted(senders.get(target, ()))))
    return plan


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
        field_name, sources = kind[1], kind[2]
        missing = [src for src in sources if state.get(src) is None]
        if missing:
            raise DataError(f"{node_path}: collector parents not yet written: {missing}")
        return {field_name: [state[src] for src in sources]}
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


# --- leveling: relay insertion so joins wait for every parent -----------------


def _back_edges(edges: list[tuple[str, str]]) -> set[tuple[str, str]]:
    """Edges whose target is an unfinished ancestor in a DFS from START."""
    adjacency: dict[str, list[str]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, []).append(dst)
    color: dict[str, int] = {}  # 1 gray, 2 black
    back: set[tuple[str, str]] = set()

    def visit(node: str) -> None:
        color[node] = 1
        for nxt in adjacency.get(node, ()):
            if color.get(nxt) == 1:
                back.add((node, nxt))
            elif color.get(nxt) is None:
                visit(nxt)
        color[node] = 2

    visit(START)
    return back


def _levels(nodes: set[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """Longest-path depth per node over non-back static edges.

    START sits at -1 so its entry child lands at level 0. Relays lift shorter
    incoming paths so every parent chain reaches a joint target at the same
    depth, which is what makes LangGraph fire the target exactly once.
    """
    back = _back_edges(edges)
    live = [(s, d) for s, d in edges if (s, d) not in back and d != END]
    level = {n: 0 for n in nodes}
    changed = True
    guard = 0
    while changed:  # Bellman-style relaxation; acyclic after back-edge removal
        changed = False
        guard += 1
        if guard > len(nodes) + 2:
            break  # pragma: no cover - defensive
        for src, dst in live:
            base = -1 if src == START else level[src]
            if level[dst] < base + 1:
                level[dst] = base + 1
                changed = True
    return level


class Engine:
    """Compile, run, and resume workflows on LangGraph.

    Attributes:
        provider: Completion provider every model call goes through; exposes
            default_variant when it carries a model registry.
        store: Sole writer of run state under .ngen-weave/runs/.
        checkpointer: "sqlite" (durable, default) or "memory" (tests).
        db_path: SQLite checkpoint database path.
        max_retries: Retries after the initial attempt for InfraError only.
        retry_backoff_ms: Exponential backoff base in milliseconds; each retry
            waits twice the previous delay.
    """

    def __init__(
        self,
        provider: CompletionProvider,
        store: RunStore,
        checkpointer: str = "sqlite",
        db_path: Path = Path(".ngen-weave/checkpoints.db"),
        max_retries: int = 3,
        retry_backoff_ms: int = 1000,
    ) -> None:
        if checkpointer not in {"sqlite", "memory"}:
            raise ConfigError(f"checkpointer must be 'sqlite' or 'memory', got {checkpointer!r}")
        self.provider = provider
        self.store = store
        self.checkpointer = checkpointer
        self.db_path = Path(db_path)
        self.max_retries = max_retries
        self.retry_backoff_ms = retry_backoff_ms
        self._compiled: dict[tuple, CompiledGraph] = {}
        self._compiling: set[int] = set()  # ids of classes mid-compilation
        self._memory: Any = None  # shared MemorySaver, created lazily

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
        key = (root_path, tuple(sorted(models.items())))
        cached = self._compiled.get(key)
        if cached is not None:
            return cached

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
        cell = {
            "plan": plan,
            "variants": variants,
            "root_path": root_path,
            "base": base,
            "instances": {path: _instantiate(cls) for path, cls in wiring.nodes.items()},
            "children": children,
        }

        builder = self._build_production_graph(wiring, recorder.ops, cell)
        compiled = CompiledGraph(root=wf, variants=variants, builder=builder)
        self._compiled[key] = compiled
        return compiled

    def _build_production_graph(self, wiring: _Wiring, ops: list, cell: dict[str, Any]) -> Any:
        """Replay recorded ops onto a StateGraph with per-key channels.

        Relay nodes align parent depths on static edges so every multi-parent
        target fires once, after all its parents wrote. Conditional edges stay
        direct: dispatch re-entry is their purpose.
        """
        from typing import Annotated, TypedDict

        from langgraph.graph import StateGraph

        fields: dict[str, type] = {path: dict for path in wiring.nodes}
        fields[_INPUT_KEY] = dict

        def _last_wins(_old: str, new: str) -> str:
            return new

        fields[_LAST_KEY] = Annotated[str, _last_wins]
        fields[_USAGE_KEY] = Annotated[list, operator.add]
        schema = TypedDict("EngineState", fields, total=False)  # type: ignore[call-overload]
        builder = StateGraph(schema)

        node_fns = {path: self._node_fn(cls, cell) for path, cls in wiring.nodes.items()}
        for path, fn in node_fns.items():
            builder.add_node(path, fn)

        static_edges = [(s, d) for s, d, _into in wiring.edges]
        levels = _levels(set(wiring.nodes), static_edges)
        back = _back_edges(static_edges)
        relay_seq = 0
        for src, dst in static_edges:
            if dst == END:
                builder.add_edge(src, END)
                continue
            hops = 0
            if (src, dst) not in back and src != START:
                hops = max(0, levels[dst] - (levels[src] + 1))
            elif src == START:
                hops = max(0, levels[dst])
            upstream = src
            for _i in range(hops):
                relay_seq += 1
                relay_name = f"__relay_{relay_seq}__"

                async def relay(state: dict) -> dict:
                    return {}

                builder.add_node(relay_name, relay)
                builder.add_edge(upstream, relay_name)
                upstream = relay_name
            builder.add_edge(upstream, dst)

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
            run_id = config["configurable"]["thread_id"]
            node_path = join_path(cell["base"], path)
            emit = self._emitter(run_id, node_path)
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
                    attempt = 0
                    while True:
                        attempt += 1
                        try:
                            output = await self._execute_leaf(cls, path, model, ctx, usage, cell)
                            break
                        except InfraError:
                            if attempt > self.max_retries:
                                raise
                            emit("node_activation", {"status": "retry", "attempt": attempt})
                            await _sleep(self.retry_backoff_ms * 2 ** (attempt - 1) / 1000)
            except DataError:
                emit("node_activation", {"status": "invalid"})
                raise

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
        final = await self._invoke(
            compiled,
            {_INPUT_KEY: model.model_dump()},
            ctx.run_id,
            checkpoint_ns=f"{attempt_ns}:{ctx.node_path}",
        )
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

    def _emitter(self, run_id: str, node_path: str) -> Callable[[str, dict], None]:
        """Provenance sink for one activation; unconditional, author-invisible."""

        def emit(kind: str, payload: dict) -> None:
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
        Human submission lands with human-node support and raises until then.
        """
        run_file = self.store.load(run_id)
        if payload is not None or run_file.status == "waiting_human":
            raise ConfigError("human review submission arrives in a later step")
        if run_file.status == "completed":
            cls = registry_get(run_file.workflow)
            assert run_file.output is not None
            return RunResult(
                run_id, "completed", cls.output_type.model_validate(run_file.output), None
            )
        cls = registry_get(run_file.workflow)
        cached = next((c for c in self._compiled.values() if c.root is cls), None)
        compiled = cached if cached is not None else self.compile(cls)
        # No interrupts exist yet, so a stopped run re-executes from the top
        # under a fresh checkpoint namespace, seeded with its stored input.
        return await self._drive(compiled, {_INPUT_KEY: run_file.input}, run_id)

    async def _drive(self, compiled: CompiledGraph, seed: dict | None, run_id: str) -> RunResult:
        """Invoke the graph, then write the terminal transition to the run file.

        A completed run also emits the root scope's node_activation record on
        the root class path, so every level of nesting carries per-scope
        RunMetadata: composites report from their own node functions, the root
        reports here once its whole subtree succeeded.
        """
        status: RunStatus = "failed"
        error: dict[str, str] | None = None
        output_dump: dict | None = None
        sink: list[Usage] = []
        started = time.perf_counter()
        # Each drive gets a fresh checkpoint namespace: LangGraph does not
        # reschedule a node that raised, so replaying the old namespace would
        # end immediately. A failed run re-executes from the top instead.
        run_file = self.store.load(run_id)
        run_file.attempts += 1
        self.store.save(run_file)
        attempt_ns = f"attempt-{run_file.attempts}"
        try:
            final = await self._invoke(compiled, seed, run_id, checkpoint_ns=attempt_ns)
            output_dump = _select_output(workflow_class_path(compiled.root), final)
            compiled.root.output_type.model_validate(output_dump)
            status = "completed"
            sink.extend(final.get(_USAGE_KEY, ()))
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
        if status == "completed":
            metadata = RunMetadata(
                iterations=1,
                tokens_in_context=sum(u[0] for u in sink),
                tokens_total=sum(u[1] for u in sink),
                cost_usd=sum(u[2] for u in sink),
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                last_output_valid=True,
            )
            self._emitter(run_id, workflow_class_path(compiled.root))(
                "node_activation", {"status": "ok", "metadata": dataclasses.asdict(metadata)}
            )
        run_file = self.store.load(run_id)  # reload: nodes appended records meanwhile
        run_file.status = status
        run_file.error = error
        run_file.output = output_dump
        self.store.save(run_file)
        if error is not None:
            return RunResult(run_id, status, None, None)
        return RunResult(
            run_id, status, compiled.root.output_type.model_validate(output_dump), None
        )

    async def _invoke(
        self,
        compiled: CompiledGraph,
        seed: dict | None,
        run_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> dict:
        """Invoke the builder's graph under the run's checkpoint thread.

        checkpoint_ns isolates drive attempts and nested activations under
        the same run id; the root graph of the first attempt uses the empty
        namespace's attempt prefix set by _drive. Usage totals travel back
        through the graph's accumulated state channel, never through config.
        """
        config = {
            "configurable": {
                "thread_id": run_id,
                "checkpoint_ns": checkpoint_ns,
                "run_attempt": int(checkpoint_ns.split(":")[0].split("-")[1])
                if checkpoint_ns
                else 1,
            }
        }
        if self.checkpointer == "memory":
            if self._memory is None:
                from langgraph.checkpoint.memory import MemorySaver

                self._memory = MemorySaver()
            graph = compiled.builder.compile(checkpointer=self._memory)
            return await graph.ainvoke(seed, config=config)
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with AsyncSqliteSaver.from_conn_string(str(self.db_path)) as saver:
            graph = compiled.builder.compile(checkpointer=saver)
            return await graph.ainvoke(seed, config=config)
