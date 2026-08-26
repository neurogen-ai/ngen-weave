"""Workflow definition: base classes, graph wiring, import-time validation."""

from __future__ import annotations

import ast
import builtins
import inspect
import re
import textwrap
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from string import Formatter
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError

from ngen_weave.errors import ConfigError
from ngen_weave.observers import (
    OBSERVER_ACTIONS,
    PREDICATE_FIELDS,
    PREDICATE_OPS,
    Observer,
    ObserverPredicate,
)

if TYPE_CHECKING:
    from ngen_weave.models.provider import CompletionProvider

START = "__start__"  # maps to langgraph START
END = "__end__"  # maps to langgraph END

_CLASS_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def workflow_class_path(target: type[Workflow] | Workflow) -> str:
    """Return the fully-qualified class path serving as identity.

    Args:
        target: A Workflow subclass or instance.

    Returns:
        "module.qualname", e.g. "examples.code_review.workflows.CodeReview".
        Nested classes keep their qualname segments joined by dots.
    """
    cls = target if isinstance(target, type) else type(target)
    return f"{cls.__module__}.{cls.__qualname__}"


@dataclass
class RunContext:
    """Per-activation context the engine hands to run().

    Attributes:
        run_id: Identifier of the enclosing run.
        node_path: Dot path of this activation, root class path first.
        emit: Provenance sink called as (kind, payload); supplied by the engine.
        provider: Completion provider for model calls; the engine's configured adapter.
    """

    run_id: str
    node_path: str
    emit: Callable[[str, dict], None]
    provider: CompletionProvider


class Workflow:
    """Base class for every node kind and composite.

    Class attributes carry the whole declaration; subclasses add behavior by
    overriding run() (leaves) or build() (composites). Identity is the fully-
    qualified class path, never an author-chosen name. Model assignment lives
    in the run config's models section, resolved at compile time, so classes
    stay free of model attributes.

    Attributes:
        description: Machine-facing text; the MCP tool description at v0.2.
        human_description: Person-facing text shown by listings and UIs.
        input_type: Required pydantic model validated at the entry boundary.
        output_type: Required pydantic model validated at every outgoing edge.
        prompt: Worker template string; override prompt(self, input) for logic.
        artifacts: Names of output_type fields persisted as content-addressed artifacts.
        observations: Observer rules supervised at activation boundaries; data only.
    """

    description: ClassVar[str] = ""
    human_description: ClassVar[str] = ""
    input_type: ClassVar[type[BaseModel]]
    output_type: ClassVar[type[BaseModel]]
    prompt: ClassVar[str | None] = None
    artifacts: ClassVar[Sequence[str]] = ()
    observations: ClassVar[Sequence[Observer]] = ()
    # Collected fan-in only: dotted path sorted on when assembling this
    # node's list input. None keeps edge-declaration order. See _check_multi_parent.
    collect_order: ClassVar[str | None] = None
    # Intermediate user-defined base classes set this to skip import-time
    # validation they cannot satisfy; concrete leaves are always validated.
    _defer_validation: ClassVar[bool] = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Bases declared in this module and classes explicitly deferring
        # validation skip the import-time pass; everything else is checked.
        if cls.__module__ == __name__ or cls.__dict__.get("_defer_validation"):
            return
        validate_structure(cls)

    def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        """Execute the leaf node; concrete leaves must override this."""
        raise NotImplementedError

    def build(self, g: GraphBuilder) -> None:
        """Wire graph structure; composites override, leaves inherit this default."""
        g.add_node(self)
        g.add_edge(START, self)
        g.add_edge(self, END)


class Worker(Workflow):
    """Leaf node that renders its prompt against validated input and calls the model."""

    def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        raise NotImplementedError


class Control(Workflow):
    """Leaf node producing a boolean verdict read from output_type.pass for routing."""

    def decide(self, input: BaseModel) -> bool:
        """Programmatic predicate; when not overridden, the rendered prompt is
        sent to the model and the reply parsed as the boolean."""
        raise NotImplementedError

    def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        raise NotImplementedError


class Human(Workflow):
    """Leaf node interrupting the run until a human submits the review artifact.

    Attributes:
        state_type: Internal pydantic model between input and output; its leaf
            primitives become the artifact's editable slots.
        verdict_field: Name of the enum/literal state field routing out of this node.
        prefill: Maps response slots to dotted paths into the edge input; fills
            the artifact, never completes it.
    """

    state_type: ClassVar[type[BaseModel]]
    verdict_field: ClassVar[str] = "verdict"
    prefill: ClassVar[Mapping[str, str]] = {}

    def transform(self, context: BaseModel, state: BaseModel) -> BaseModel:
        """Programmatic (context, state) -> output transformation; when not
        overridden the validated state passes through as the node's output."""
        raise NotImplementedError

    def run(self, input: BaseModel, ctx: RunContext) -> BaseModel:
        raise NotImplementedError


class GraphBuilder(Protocol):
    """Narrow protocol build() receives; delegates to an ordinary LangGraph StateGraph."""

    def add_node(self, node: Workflow) -> None: ...

    def add_edge(
        self,
        src: Workflow | Literal["__start__"],
        dst: Workflow | Literal["__end__"],
        *,
        into: str | None = None,
    ) -> None: ...

    def add_conditional_edges(
        self,
        src: Workflow,
        router: Callable[[dict], str],
        branches: Mapping[str, Workflow | Literal["__end__"]],
    ) -> None: ...


# --- dry-run builder --------------------------------------------------------


@dataclass(frozen=True)
class _Op:
    """One builder call noted during the build for replay and static checks."""

    name: str
    args: tuple  # class paths / END literals / into fields only; routers excluded
    router: Callable[[dict], str] | None = None  # conditional edges only


_BUILDER_DOC = """
    Sole GraphBuilder implementation: delegates every call to a throwaway
    LangGraph StateGraph, so structural problems surface from real compilation,
    and notes the wired edges for the static topology and fan-in checks.
    """


def _endpoint(value: Workflow | str) -> str:
    if isinstance(value, str):
        return value  # START/END literals
    return workflow_class_path(value)


class _StateGraphAdapter:
    """Sole GraphBuilder implementation: delegates to a throwaway LangGraph
    StateGraph, so structural errors come out of real compilation, and notes
    the wired edges for the static topology and fan-in checks."""

    def __init__(
        self,
        node_runner: Callable[[Workflow], Callable] | None = None,
        router_wrapper: Callable[[str, Callable, Mapping[str, str]], Callable] | None = None,
    ) -> None:
        from langgraph.graph import StateGraph

        self._g: StateGraph = StateGraph(dict)
        self.ops: list[_Op] = []
        self.node_classes: dict[str, type[Workflow]] = {}
        # The engine supplies both hooks to bind real node functions and
        # runtime-checked routers; validation builds keep the no-op defaults.
        self._node_runner = node_runner
        self._router_wrapper = router_wrapper

    def add_node(self, node: Workflow) -> None:
        path = workflow_class_path(node)
        fn = self._node_runner(node) if self._node_runner is not None else (lambda state: {})
        self._g.add_node(path, fn)
        self.ops.append(_Op("add_node", (path,)))
        self.node_classes[path] = node if isinstance(node, type) else type(node)

    def add_edge(
        self, src: Workflow | str, dst: Workflow | str, *, into: str | None = None
    ) -> None:
        try:
            self._g.add_edge(_endpoint(src), _endpoint(dst))
        except Exception as exc:
            raise ConfigError(
                f"langgraph rejected edge {_endpoint(src)} -> {_endpoint(dst)}: {exc}"
            ) from exc
        self.ops.append(_Op("add_edge", (_endpoint(src), _endpoint(dst), into)))

    def add_conditional_edges(
        self, src: Workflow, router: Callable[[dict], str], branches: Mapping[str, Workflow | str]
    ) -> None:
        mapping = {label: _endpoint(t) for label, t in branches.items()}
        if self._router_wrapper is not None:
            router = self._router_wrapper(workflow_class_path(src), router, mapping)
        try:
            self._g.add_conditional_edges(workflow_class_path(src), router, mapping)
        except Exception as exc:
            raise ConfigError(
                f"langgraph rejected conditional edges from {workflow_class_path(src)}: {exc}"
            ) from exc
        targets = tuple(sorted(mapping.items()))
        self.ops.append(_Op("add_conditional", (workflow_class_path(src), targets), router=router))


# --- structural checks ------------------------------------------------------


@dataclass
class _Wiring:
    """Topology extracted from the dry-run build."""

    nodes: dict[str, type[Workflow]]  # class path -> wired child class
    edges: list[tuple[str, str, str | None]]  # (src, dst, into-slot or None)
    conditional: list[tuple[str, dict[str, str]]]


def _wiring_from(ops: list[_Op], node_classes: dict[str, type[Workflow]]) -> _Wiring:
    w = _Wiring(nodes=dict(node_classes), edges=[], conditional=[])
    for op in ops:
        if op.name == "add_node":
            continue  # node classes arrive via the sink's node_classes map
        elif op.name == "add_edge":
            w.edges.append((op.args[0], op.args[1], op.args[2]))
        else:
            w.conditional.append((op.args[0], dict(op.args[1])))
    return w


_CONTAINER_ORIGINS = frozenset({list, set, frozenset, tuple, dict})


def _annotation_name(annotation: Any) -> str:
    """Readable name for error messages; handles parameterized annotations."""
    origin = get_origin(annotation)
    if origin is None:
        return getattr(annotation, "__name__", str(annotation))
    args = ", ".join(_annotation_name(a) for a in get_args(annotation))
    return f"{origin.__name__ if hasattr(origin, '__name__') else origin}[{args}]"


def _accepts(annotation: Any, source_type: type[BaseModel]) -> bool:
    """Whether a source output type fits an input annotation, per pydantic.

    Probes with an unvalidated instance of the source model so the check stays
    import-time only: smart-mode validation accepts subclass instances and
    rejects unrelated types without needing real field values.

    Container annotations are exempt rather than checked: a model instance
    iterates its fields, so pydantic would coerce it into list[...] even
    though the runtime port delivers a dict, which never fits. Collected
    fan-in covers list slots structurally instead.
    """
    if get_origin(annotation) in _CONTAINER_ORIGINS:
        return True
    try:
        TypeAdapter(annotation).validate_python(source_type.model_construct())
        return True
    except ValidationError:
        return False


def _instantiate(cls: type[Workflow]) -> Workflow:
    try:
        return cls()
    except Exception as exc:
        raise ConfigError(
            f"{workflow_class_path(cls)} must be constructible with no arguments for "
            f"import-time validation; give tunables defaults ({exc})"
        ) from exc


def _check_declarations(cls: type[Workflow]) -> None:
    path = workflow_class_path(cls)
    _check_type_attrs(cls, path)
    if issubclass(cls, Worker):
        _check_worker_prompt(cls, path)
    if issubclass(cls, Control):
        _check_control(cls, path)
    if issubclass(cls, Human):
        _check_human_state(cls, path)
    _check_prompt_and_artifacts(cls, path)
    _check_observations(cls, path)
    if not (getattr(cls, "collect_order", None) is None or isinstance(cls.collect_order, str)):
        raise ConfigError(f"{path}: collect_order must be None or a dotted path string")


def _check_observations(cls: type[Workflow], path: str) -> None:
    """Every declared observer carries an ObserverPredicate with known field/op/action."""
    for index, obs in enumerate(getattr(cls, "observations", ()) or ()):
        where = f"{path}: observations[{index}]"
        if not isinstance(obs, Observer):
            raise ConfigError(f"{where} must be an Observer built by ngen_weave.observers")
        if not isinstance(obs.predicate, ObserverPredicate):
            raise ConfigError(
                f"{where}: predicate must be an ObserverPredicate from ngen_weave.observers"
            )
        pred = obs.predicate
        if pred.field not in PREDICATE_FIELDS:
            raise ConfigError(
                f"{where}: unknown predicate field {pred.field!r}; expected one of "
                f"{', '.join(sorted(PREDICATE_FIELDS))}"
            )
        if pred.op not in PREDICATE_OPS:
            raise ConfigError(
                f"{where}: unknown predicate op {pred.op!r}; expected one of "
                f"{', '.join(sorted(PREDICATE_OPS))}"
            )
        if isinstance(pred.value, bool) or not isinstance(pred.value, (int, float)):
            raise ConfigError(f"{where}: predicate value must be int or float, got {pred.value!r}")
        if obs.action not in OBSERVER_ACTIONS:
            raise ConfigError(f"{where}: unknown action {obs.action!r}")


def _check_type_attrs(cls: type[Workflow], path: str) -> None:
    """input_type/output_type must be pydantic models; the class path must be well-formed."""
    for attr in ("input_type", "output_type"):
        value = getattr(cls, attr, None)
        if not (isinstance(value, type) and issubclass(value, BaseModel)):
            raise ConfigError(f"{path}: {attr} must be a pydantic BaseModel subclass")
    last_segment = cls.__qualname__.rsplit(".", 1)[-1]
    if not _CLASS_NAME_RE.match(last_segment):
        raise ConfigError(f"{path}: last path segment {last_segment!r} is not a valid identifier")


def _check_worker_prompt(cls: type[Workflow], path: str) -> None:
    """A Worker renders its prompt template; it must declare one."""
    if getattr(cls, "prompt", None) is None:
        raise ConfigError(f"{path}: Worker requires a prompt template or prompt() override")


def _check_control(cls: type[Workflow], path: str) -> None:
    """Model-mode Controls need a prompt; every Control outputs required bool 'pass'."""
    if cls.decide is Control.decide and getattr(cls, "prompt", None) is None:
        raise ConfigError(
            f"{path}: model-mode Control requires a prompt template or a decide() override"
        )
    f = cls.output_type.model_fields.get("pass")
    if f is None or not f.is_required() or f.annotation is not bool:
        raise ConfigError(f"{path}: Control output_type must declare required bool field 'pass'")


def _check_verdict_field(state_type: type[BaseModel], cls: type[Workflow], path: str) -> None:
    """The routing verdict must be an existing enum- or literal-typed leaf of state_type."""
    verdict = getattr(cls, "verdict_field", "verdict")
    vf = state_type.model_fields.get(verdict)
    ann = vf.annotation if vf is not None else None
    is_enum = isinstance(ann, type) and issubclass(ann, Enum)
    is_literal = get_origin(ann) is Literal
    if not (is_enum or is_literal):
        raise ConfigError(
            f"{path}: verdict field {verdict!r} must be on state_type, enum- or literal-typed"
        )


def _check_prefill_paths(cls: type[Workflow], path: str) -> None:
    """Every prefill slot names a real state field and a resolvable dotted input path."""
    state_type = cls.state_type
    for slot, slot_path in getattr(cls, "prefill", {}).items():
        if slot not in state_type.model_fields:
            raise ConfigError(f"{path}: prefill targets unknown state field {slot!r}")
        target: Any = cls.input_type
        for segment in slot_path.split("."):
            if not (isinstance(target, type) and issubclass(target, BaseModel)):
                raise ConfigError(f"{path}: prefill path {slot_path!r} traverses below a primitive")
            fi2 = target.model_fields.get(segment)
            if fi2 is None:
                raise ConfigError(
                    f"{path}: prefill path {slot_path!r} names no field {segment!r} "
                    f"of {cls.input_type.__name__}"
                )
            target = fi2.annotation


def _check_human_state(cls: type[Workflow], path: str) -> None:
    """Humans need a flat state_type with an enum/literal verdict and valid prefill paths."""
    state_type = getattr(cls, "state_type", None)
    if not (isinstance(state_type, type) and issubclass(state_type, BaseModel)):
        raise ConfigError(f"{path}: Human requires state_type as a pydantic BaseModel subclass")
    _check_verdict_field(state_type, cls, path)
    for name, fi in state_type.model_fields.items():
        if isinstance(fi.annotation, type) and issubclass(fi.annotation, BaseModel):
            raise ConfigError(
                f"{path}: state_type field {name!r} is a nested model; "
                "review artifacts cover flat models only"
            )
    _check_prefill_paths(cls, path)


def _check_prompt_and_artifacts(cls: type[Workflow], path: str) -> None:
    """Prompt placeholders resolve against input_type; artifact names exist on output_type."""
    prompt = getattr(cls, "prompt", None)
    if isinstance(prompt, str):
        placeholders = _placeholder_roots(prompt)
        for head in placeholders:
            if head not in cls.input_type.model_fields:
                raise ConfigError(f"{path}: prompt placeholder {head!r} not found on input_type")

    for name in getattr(cls, "artifacts", ()) or ():
        if name not in cls.output_type.model_fields:
            raise ConfigError(f"{path}: artifact {name!r} not found on output_type")


def _placeholder_roots(template: str) -> set[str]:
    roots: set[str] = set()
    for _, field_name, _, _ in Formatter().parse(template):
        if field_name:
            roots.add(field_name.split(".")[0].split("[")[0])
    return roots


def _all_edges(w: _Wiring) -> list[tuple[str, str]]:
    """Static edges plus every conditional branch target, as plain pairs."""
    all_edges: list[tuple[str, str]] = [(s, d) for s, d, _into in w.edges]
    for src, branches in w.conditional:
        all_edges.extend((src, t) for t in branches.values())
    return all_edges


def _successors(w: _Wiring, all_edges: list[tuple[str, str]]) -> dict[str, set[str]]:
    """Map each node to everything it can reach in one hop."""
    successors: dict[str, set[str]] = {}
    for src, dst in all_edges:
        successors.setdefault(src, set()).add(dst)
    return successors


def _reachable(entry: str, successors: dict[str, set[str]]) -> set[str]:
    """Nodes reachable from entry by any edge path; END terminates walks."""
    reached: set[str] = set()
    stack = [entry]
    while stack:
        current = stack.pop()
        if current in reached or current == END:
            continue
        reached.add(current)
        stack.extend(successors.get(current, ()))
    return reached


def _check_endpoints(known: set[str], path: str, all_edges: list[tuple[str, str]]) -> None:
    """Every edge endpoint is a wired node or the START/END sentinel."""
    for src, dst in all_edges:
        for endpoint in (src, dst):
            if endpoint not in known and endpoint not in (START, END):
                raise ConfigError(f"{path}: edge references {endpoint}, never added with add_node")


def _check_terminals(w: _Wiring, cls: type[Workflow], path: str) -> None:
    """Rule 4 last hop: terminals emit subtypes of the composite output.

    Early-exit paths may narrow the output but never emit an unrelated model.
    """
    terminals = [src for src, dst, _into in w.edges if dst == END]
    cond_to_end = [src for src, branches in w.conditional for t in branches.values() if t == END]
    for term in terminals + cond_to_end:
        child_cls = w.nodes.get(term)
        if child_cls is not None and not _accepts(cls.output_type, child_cls.output_type):
            raise ConfigError(
                f"{path}: terminal child {term} emits "
                f"{child_cls.output_type.__name__}, which is not a subtype of the "
                f"composite output {cls.output_type.__name__}"
            )


def _check_topology(w: _Wiring, cls: type[Workflow]) -> None:
    path = workflow_class_path(cls)
    known = set(w.nodes)
    all_edges = _all_edges(w)
    _check_endpoints(known, path, all_edges)

    entries = [dst for src, dst, _into in w.edges if src == START]
    cond_from_start = [c for c in w.conditional if c[0] == START]
    if len(entries) != 1 or cond_from_start:
        raise ConfigError(f"{path}: exactly one edge from START is required, found {len(entries)}")

    reached = _reachable(entries[0], _successors(w, all_edges))
    unreachable = sorted(known - reached)
    if unreachable:
        raise ConfigError(f"{path}: unreachable nodes: {', '.join(unreachable)}")
    if not any(dst == END and src in reached for src, dst in all_edges):
        raise ConfigError(f"{path}: END is not reachable")

    # Rule 4, first hop: entry edges are checked inside _check_fanin, where
    # START participates as a pseudo-source whose output_type is cls.input_type.
    _check_terminals(w, cls, path)
    _check_fanin(w, cls, path)


def _incoming(w: _Wiring) -> tuple[dict[str, list[tuple[str, str | None]]], set[str]]:
    """Static parents per destination, plus conditional dispatch targets.

    Plain add_edge calls create static parents; START joins its target as a
    pseudo-source whose output_type is the composite's input_type (rule 4),
    so entry edges reuse exactly the checks every other edge gets.
    Conditional branches dispatch to one source per activation, so they never
    contribute a second simultaneous input, but a target reached both
    statically and by dispatch has no unambiguous assembly.
    """
    incoming: dict[str, list[tuple[str, str | None]]] = {}
    dispatched: set[str] = set()
    for src, dst, into in w.edges:
        incoming.setdefault(dst, []).append((src, into))
    for _src, branches in w.conditional:
        for t in branches.values():
            if t != END:  # END takes no input
                dispatched.add(t)
    return incoming, dispatched


def _list_bounds(field_info: Any) -> tuple[int, int | None]:
    min_length = 0
    max_length: int | None = None
    for meta in field_info.metadata:
        lo = getattr(meta, "min_length", None)
        if isinstance(lo, int):
            min_length = max(min_length, lo)
        hi = getattr(meta, "max_length", None)
        if isinstance(hi, int):
            max_length = hi if max_length is None else min(max_length, hi)
    return min_length, max_length


def _check_slot_fit(
    w: _Wiring,
    cls: type[Workflow],
    path: str,
    dst: str,
    source: str,
    field_name: str,
) -> None:
    """One into= edge: the slot must exist on dst's input and accept its source.

    START counts as a pseudo-source whose output_type is the composite's
    input_type (rule 4). The fit check delegates to pydantic, so parameterized
    annotations (list[Model], unions, scalars) are checked too, not skipped.
    """
    child_cls = w.nodes.get(dst)
    if child_cls is None:
        return
    input_model = child_cls.input_type
    fi = input_model.model_fields.get(field_name)
    if fi is None:
        raise ConfigError(
            f"{path}: slot {field_name!r} on {dst} is not a field of {input_model.__name__}"
        )
    if source == START:
        source_output, source_desc = cls.input_type, "the composite input"
    else:
        source_output, source_desc = w.nodes[source].output_type, f"{source}.output_type"
    if not _accepts(fi.annotation, source_output):
        raise ConfigError(
            f"{path}: {source_desc} {source_output.__name__} does not fit slot "
            f"{field_name!r} of type {_annotation_name(fi.annotation)} on {dst}"
        )


def _check_entry_target(
    w: _Wiring,
    cls: type[Workflow],
    path: str,
    dst: str,
    entry_slot: str | None,
) -> None:
    """Entry-only target (rule 4): START delivers the composite input.

    Either the edge targets a slot that must exist and accept that input, or
    the child receives the whole validated composite input.
    """
    if entry_slot is not None:
        _check_slot_fit(w, cls, path, dst, START, entry_slot)
        return
    child_cls = w.nodes.get(dst)
    if child_cls is not None and not _accepts(child_cls.input_type, cls.input_type):
        raise ConfigError(
            f"{path}: entry child {dst} declares input_type "
            f"{child_cls.input_type.__name__}, which does not accept "
            f"the composite input {cls.input_type.__name__}"
        )


def _check_single_parent(path: str, dst: str, slots: list[tuple[str, str | None]]) -> None:
    """A lone parent may carry no into= slot; fan-in forms need many parents."""
    if not slots:
        return
    if slots[0][0] == START:
        raise ConfigError(
            f"{path}: {dst} pairs the START entry edge with into=; "
            "an entry slot cannot coexist with an ordinary parent"
        )
    raise ConfigError(
        f"{path}: {dst} has a single parent; into= is only legal on multi-parent fan-in"
    )


def _check_slots_form(
    w: _Wiring,
    cls: type[Workflow],
    path: str,
    dst: str,
    slots: list[tuple[str, str | None]],
    input_model: type[BaseModel],
) -> None:
    """Named-slot fan-in: no duplicate slots, every slot fits, none uncovered."""
    seen: dict[str, str] = {}
    for source, field_name in slots:
        if field_name in seen:
            raise ConfigError(
                f"{path}: slot {field_name!r} on {dst} targeted twice "
                f"({seen[field_name]}, {source})"
            )
        seen[field_name] = source
        _check_slot_fit(w, cls, path, dst, source, field_name)
    uncovered = [
        name
        for name, fi in input_model.model_fields.items()
        if fi.is_required() and name not in seen
    ]
    if uncovered:
        raise ConfigError(
            f"{path}: named-slot fan-in on {dst} leaves required fields of "
            f"{input_model.__name__} uncovered: {', '.join(uncovered)}"
        )


def _check_collect_form(
    path: str,
    dst: str,
    parent_count: int,
    input_model: type[BaseModel],
) -> None:
    """Collected fan-in: one list[<model>] field whose bounds admit the parents."""
    list_fields = [
        (name, fi)
        for name, fi in input_model.model_fields.items()
        if get_origin(fi.annotation) is list
    ]
    names = ", ".join(name for name, _fi in list_fields)
    if len(list_fields) != 1:
        raise ConfigError(
            f"{path}: collected fan-in on {dst} needs exactly one list[...] field on "
            f"{input_model.__name__}, found: {names or 'none'}"
        )
    field_name, fi = list_fields[0]
    args = get_args(fi.annotation)
    elem = args[0] if args else None
    if not (isinstance(elem, type) and issubclass(elem, BaseModel)):
        raise ConfigError(
            f"{path}: collector {field_name!r} on {dst} must be typed list[<pydantic model>]"
        )
    low, high = _list_bounds(fi)
    bound = f"between {low} and {high}" if high is not None else f"at least {low}"
    if parent_count < low or (high is not None and parent_count > high):
        raise ConfigError(
            f"{path}: {dst} expects {bound} parents ({field_name!r} bounds), got {parent_count}"
        )


def _resolve_schema_path(model: type[BaseModel], path: str) -> bool:
    """Whether path resolves on model's fields; segments name fields or index lists."""
    current: Any = model
    for segment in path.split("."):
        if isinstance(current, list) or get_origin(current) is list:
            elem = get_args(current)[0] if get_origin(current) is list else current[0]
            if not segment.isdigit():
                return False
            current = elem
            continue
        if not (isinstance(current, type) and issubclass(current, BaseModel)):
            return False
        field = current.model_fields.get(segment)
        if field is None:
            return False
        current = field.annotation
    return True


def _check_multi_parent(
    w: _Wiring,
    cls: type[Workflow],
    path: str,
    dst: str,
    parents: list[tuple[str, str | None]],
    ordinary: list[tuple[str, str]],
    slots: list[tuple[str, str | None]],
) -> None:
    """Multi-parent target: human cap, one declared form, then per-form rules."""
    child_cls = w.nodes.get(dst)
    if child_cls is not None and issubclass(child_cls, Human):
        raise ConfigError(
            f"{path}: human node {dst} accepts at most one parent, found {len(ordinary)}"
        )
    if slots and len(slots) != len(parents):
        raise ConfigError(
            f"{path}: {dst} mixes into= slot edges with plain untargeted edges; "
            "a multi-parent target must fit one declared form"
        )
    if child_cls is None:
        return
    if slots:
        _check_slots_form(w, cls, path, dst, slots, child_cls.input_type)
    else:
        _check_collect_form(path, dst, len(ordinary), child_cls.input_type)
        order = child_cls.collect_order
        if order is not None:
            offenders = [
                workflow_class_path(w.nodes[src])
                for src, _f in ordinary
                if src in w.nodes and not _resolve_schema_path(w.nodes[src].output_type, order)
            ]
            if offenders:
                raise ConfigError(
                    f"{path}: collect_order {order!r} on {dst} does not resolve "
                    f"against output_type of: {', '.join(offenders)}"
                )


def _check_fanin(w: _Wiring, cls: type[Workflow], path: str) -> None:
    """Rule 3b: every multi-parent target must fit exactly one declared form.

    Rule 4 rides the same machinery: START joins as a pseudo-source whose
    output_type is the composite's input_type, so entry edges (plain or into=)
    are checked here like any other edge, and nothing about boundaries is a
    second type-checking notion.
    """
    incoming, dispatched = _incoming(w)
    for dst, parents in incoming.items():
        if dst == END:
            continue
        # START delivers the run input rather than a state-keyed parent dump,
        # so it never creates assembly ambiguity with dispatch and never
        # counts toward parent counts; rule 4 still type-checks its edge.
        ordinary = [(s, f) for s, f in parents if s != START]
        entry_slot = next((f for s, f in parents if s == START), None)
        if ordinary and dst in dispatched:
            raise ConfigError(
                f"{path}: {dst} receives both static and conditional edges; "
                "input assembly cannot know how many parents fire"
            )
        slots = [(s, f) for s, f in parents if f is not None]
        if not ordinary:
            _check_entry_target(w, cls, path, dst, entry_slot)
        elif len(ordinary) == 1:
            _check_single_parent(path, dst, slots)
        else:
            _check_multi_parent(w, cls, path, dst, parents, ordinary, slots)


def _check_leaf_composite(cls: type[Workflow]) -> None:
    path = workflow_class_path(cls)
    # A leaf's inherited build() wires the node too, so compositeness is an
    # overridden build(), never the presence of nodes in the wiring.
    composite = "build" in cls.__dict__
    # Leaf kinds ship placeholder bodies filled by later engine steps, so only
    # the bare base implementation counts as missing.
    overrides_run = cls.run is not Workflow.run
    if composite and overrides_run:
        raise ConfigError(f"{path}: composites must not override run(); they wire in build()")
    if not composite and not overrides_run:
        raise ConfigError(f"{path}: leaf workflows must define run() concretely")


def validate_structure(cls: type[Workflow]) -> None:
    """Run the full import-time validation pass over a workflow class.

    Instantiates cls (tunables need defaults), builds build() once against a
    throwaway StateGraph so structural problems surface from real compilation,
    lints build()'s source against environment-dependent or stateful wiring,
    then applies the structural, leaf/composite, and fan-in checks. Raises
    ConfigError naming the class path on the first problem; returns None when
    clean.
    """
    path = workflow_class_path(cls)
    _check_declarations(cls)
    wf = _instantiate(cls)
    builder = _StateGraphAdapter()
    try:
        wf.build(builder)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"dry run build() of {path} failed: {exc}") from exc
    _lint_build_source(cls)
    _check_leaf_composite(cls)
    _check_topology(_wiring_from(builder.ops, builder.node_classes), cls)


# --- static lint over build()'s source --------------------------------------

_CLOCK_RANDOM_MODULES = frozenset({"random", "time", "uuid"})


def _dotted(node: ast.expr) -> str | None:
    """Return a.b.c for pure Name/Attribute chains, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _root_name(target: ast.expr) -> str | None:
    """Base name of an attribute/subscript chain, e.g. self for self._count."""
    while isinstance(target, (ast.Attribute, ast.Subscript)):
        target = target.value
    return target.id if isinstance(target, ast.Name) else None


def _is_set_iteration(iter_expr: ast.expr) -> bool:
    if isinstance(iter_expr, ast.Set):
        return True
    return (
        isinstance(iter_expr, ast.Call)
        and isinstance(iter_expr.func, ast.Name)
        and iter_expr.func.id == "set"
    )


class _BuildLinter:
    """Walk build()'s AST once and reject environment-dependent wiring.

    Builds the stored-name and parameter sets once, then applies each
    prohibition in a dedicated method so no single check exceeds the mccabe
    ceiling. A violation raises ConfigError naming the offending line.
    """

    def __init__(self, path: str, func: ast.FunctionDef) -> None:
        self._path = path
        self._stored = {
            n.id for n in ast.walk(func) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }
        self._params = {
            a.arg for a in (*func.args.posonlyargs, *func.args.args, *func.args.kwonlyargs)
        }
        self._builtins = set(dir(builtins))

    def is_outer_root(self, name: str | None) -> bool:
        return name is not None and name not in self._stored and name not in self._params

    def fail(self, line: int, message: str) -> None:
        raise ConfigError(f"{self._path}: build() line {line}: {message}")

    def lint(self, func: ast.FunctionDef) -> None:
        for node in ast.walk(func):
            self._check_forbidden_calls(node)
            self._check_env_reads(node)
            self._check_mutation(node)
            self._check_set_iteration(node)

    def _check_forbidden_calls(self, node: ast.AST) -> None:
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            return
        dotted = _dotted(node.func.value)
        head = dotted.split(".")[0] if dotted else None
        if head in _CLOCK_RANDOM_MODULES:
            self.fail(
                node.lineno, f"call into {head}.*; build() must not depend on the environment"
            )
        if (
            dotted is not None
            and (dotted == "datetime" or dotted.endswith(".datetime"))
            and node.func.attr in {"now", "utcnow"}
        ):
            self.fail(node.lineno, "datetime.now/utcnow; build() must not read the clock")

    def _check_env_reads(self, node: ast.AST) -> None:
        if not isinstance(node, (ast.Attribute, ast.Subscript)):
            return
        dotted = _dotted(node)
        if dotted is not None and dotted.startswith("os.environ"):
            self.fail(node.lineno, "read of os.environ; build() must not depend on the environment")

    def _check_mutation(self, node: ast.AST) -> None:
        if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            return
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if not isinstance(target, (ast.Attribute, ast.Subscript)):
                continue
            root = _root_name(target)
            if root == "self":
                self.fail(node.lineno, "mutation through self; build() must be stateless")
            elif self.is_outer_root(root) and root not in self._builtins:
                self.fail(node.lineno, f"mutation of global state ({root}); build() must be pure")

    def _check_set_iteration(self, node: ast.AST) -> None:
        if not isinstance(node, (ast.For, ast.comprehension)):
            return
        if _is_set_iteration(node.iter):
            self.fail(
                getattr(node.iter, "lineno", 0),
                "iterating over a set; set order is nondeterministic, iterate a list or tuple",
            )


def _lint_build_source(cls: type[Workflow]) -> None:
    """Enforce the build() wires-not-computes contract statically.

    Rejects environment/clock/randomness reads, mutation of self or module
    globals, and iteration over sets (unordered, hence nondeterministic wiring).
    Source that cannot be retrieved or parsed (dynamic definitions) is skipped.
    """
    fn = cls.__dict__.get("build")
    if fn is None:
        return  # inherited default build() wires two edges and nothing else
    path = workflow_class_path(cls)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    except (OSError, TypeError, SyntaxError):
        return
    func = tree.body[0]
    _BuildLinter(path, func).lint(func)


# --- model binding ----------------------------------------------------------


def resolve_model_variant(
    leaf: type[Workflow],
    scopes: Sequence[type[Workflow]],
    models: Mapping[str, str],
    default_variant: str,
) -> str:
    """Resolve the model variant for a leaf from exact-path bindings.

    Args:
        leaf: The model-calling leaf class about to be compiled.
        scopes: The leaf's enclosing composites ordered innermost first.
        models: Mapping of fully-qualified class paths to variant names; keys
            match concrete class paths exactly, inheritance plays no role.
        default_variant: defaultVariant from models.json, used when nothing binds.

    Returns:
        The leaf's own binding if present, else the innermost enclosing scope's
        binding, else default_variant. A key naming a composite therefore
        governs every model-calling leaf in its subtree however deep, unless a
        deeper scope or the leaf itself is bound directly.
    """
    own = models.get(workflow_class_path(leaf))
    if own is not None:
        return own
    for scope in scopes:
        variant = models.get(workflow_class_path(scope))
        if variant is not None:
            return variant
    return default_variant
