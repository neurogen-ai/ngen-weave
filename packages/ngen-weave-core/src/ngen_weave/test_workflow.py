"""Tests for workflow definition, dry-run validation, and model binding."""

from __future__ import annotations

import os
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from ngen_weave.errors import ConfigError
from ngen_weave.provenance import join_path
from ngen_weave.workflow import (
    END,
    START,
    Control,
    Human,
    RunContext,
    Worker,
    Workflow,
    resolve_model_variant,
    validate_structure,
    workflow_class_path,
)


class In(BaseModel):
    text: str


class Out(BaseModel):
    result: str


# `pass` is a keyword, so the control output schema is built dynamically.
GateOut = type("GateOut", (BaseModel,), {"__annotations__": {"pass": bool}})


def make_worker(name: str, prompt: str | None = "do {text}", input_type=In, output_type=Out):
    def run(self, input, ctx):
        return output_type(result="x")

    body: dict = {
        "__module__": __name__,
        "__qualname__": name,
        "input_type": input_type,
        "output_type": output_type,
        "run": run,
    }
    if prompt is not None:
        body["prompt"] = prompt
    return type(name, (Worker,), body)


def make_control(name: str, output_type: type[BaseModel] = GateOut):
    def decide(self, input):
        return True

    return type(
        name,
        (Control,),
        {
            "__module__": __name__,
            "__qualname__": name,
            "input_type": In,
            "output_type": output_type,
            "decide": decide,
        },
    )


# --- valid definitions ------------------------------------------------------


def test_valid_leaf_passes():
    w = make_worker("Leaf")
    validate_structure(w)
    assert workflow_class_path(w).endswith(".Leaf")


def test_valid_linear_composite_passes():
    draft = make_worker("Draft")
    finalize = make_worker("Finalize")

    class Chain(Workflow):
        input_type = In
        output_type = Out

        def build(self, g):
            g.add_node(draft)
            g.add_node(finalize)
            g.add_edge(START, draft)
            g.add_edge(draft, finalize)
            g.add_edge(finalize, END)

    validate_structure(Chain)


def test_canonical_code_review_shape_compiles():
    class Req(BaseModel):
        request: dict

    class ReviewedDiff(BaseModel):
        reviewed_diff: str

    draft = make_worker(
        "draft", prompt="review {request.diff}", input_type=Req, output_type=ReviewedDiff
    )
    gate = make_control("gate")
    finalize = make_worker("finalize", output_type=ReviewedDiff)

    class ReviewState(BaseModel):
        verdict: Literal["approve", "reject"]
        notes: str = ""

    def human_run(self, input, ctx):
        return ReviewState(verdict="approve")

    human = type(
        "human_review",
        (Human,),
        {
            "__module__": __name__,
            "__qualname__": "human_review",
            "input_type": GateOut,
            "output_type": ReviewState,
            "state_type": ReviewState,
            "run": human_run,
        },
    )

    class CodeReview(Workflow):
        input_type = Req
        output_type = ReviewedDiff

        def build(self, g):
            g.add_node(draft)
            g.add_node(gate)
            g.add_node(human)
            g.add_node(finalize)
            g.add_edge(START, draft)
            g.add_edge(draft, gate)
            g.add_conditional_edges(gate, lambda s: "pass", {"pass": human, "fail": draft})
            g.add_conditional_edges(
                human, lambda s: "approve", {"approve": finalize, "reject": draft}
            )
            g.add_edge(finalize, END)

    validate_structure(CodeReview)


def test_same_short_names_in_different_modules_coexist():
    a = make_worker("Shared", input_type=In, output_type=Out)

    def run(self, input, ctx):
        return Out(result="x")

    b = type(
        "Shared",
        (Worker,),
        {
            "__module__": "other.module",
            "__qualname__": "Shared",
            "input_type": In,
            "output_type": Out,
            "prompt": "p",
            "run": run,
        },
    )
    validate_structure(a)
    validate_structure(b)
    assert workflow_class_path(a) != workflow_class_path(b)
    assert workflow_class_path(a).endswith(".Shared")
    assert workflow_class_path(b).endswith(".Shared")


# --- declaration failures ---------------------------------------------------


def test_missing_input_type_fails():
    with pytest.raises(ConfigError, match="input_type"):

        class Bad(Workflow):
            output_type = Out


def test_non_pydantic_output_type_fails():
    with pytest.raises(ConfigError, match="output_type"):

        class Bad(Workflow):
            input_type = In
            output_type = dict  # type: ignore[assignment]


def test_worker_without_prompt_fails():
    base = type(
        "NoPromptBase",
        (Worker,),
        {
            "__module__": __name__,
            "__qualname__": "NoPromptBase",
            "input_type": In,
            "output_type": Out,
            "_defer_validation": True,
        },
    )

    with pytest.raises(ConfigError, match="prompt"):

        class Bad(base):
            pass


def test_prompt_placeholder_not_on_input_type_fails():
    with pytest.raises(ConfigError, match="placeholder"):
        make_worker("BadPlaceholder", prompt="use {missing_field}")


def test_dotted_prompt_placeholder_ok():
    class Req(BaseModel):
        request: dict

    make_worker("DottedOk", prompt="diff is {request.diff}", input_type=Req)


def test_control_without_required_bool_pass_fails():
    class NotBool(BaseModel):
        ok: int

    with pytest.raises(ConfigError, match="'pass'"):
        make_control("NotBoolControl", output_type=NotBool)


def test_control_optional_bool_pass_fails():
    class OptionalBool(BaseModel):
        ok: bool = True

    with pytest.raises(ConfigError, match="'pass'"):
        make_control("OptionalControl", output_type=OptionalBool)


def test_human_bad_state_type_fails():
    with pytest.raises(ConfigError, match="state_type"):

        class Bad(Human):
            input_type = In
            output_type = Out
            state_type = dict  # type: ignore[assignment]


def test_human_verdict_field_wrong_type_fails():
    class Plain(BaseModel):
        verdict: str

    with pytest.raises(ConfigError, match="verdict"):

        class Bad(Human):
            input_type = In
            output_type = Out
            state_type = Plain


def test_prefill_unknown_state_field_fails():
    class S(BaseModel):
        verdict: Literal["approve", "reject"]

    with pytest.raises(ConfigError, match="unknown state field"):

        class Bad(Human):
            input_type = In
            output_type = Out
            state_type = S
            prefill = {"nope": "x"}


def test_prefill_unknown_context_path_fails():
    class S(BaseModel):
        verdict: Literal["approve", "reject"]
        note: str = ""

    with pytest.raises(ConfigError, match="prefill path"):

        class Bad(Human):
            input_type = In
            output_type = Out
            state_type = S
            prefill = {"note": "nowhere.to.be"}


def test_prefill_dotted_context_path_passes():
    class Nested(BaseModel):
        leaf: str

    class Source(BaseModel):
        nested: Nested

    class S(BaseModel):
        verdict: Literal["approve", "reject"]
        note: str = ""

    class Ok(Human):
        input_type = Source
        output_type = Out
        state_type = S
        prefill = {"note": "nested.leaf"}


def test_nested_state_type_field_fails_at_import():
    class Inner(BaseModel):
        x: int

    class Nested(BaseModel):
        verdict: Literal["approve", "reject"]
        inner: Inner

    with pytest.raises(ConfigError, match="nested model"):

        class Bad(Human):
            input_type = In
            output_type = Out
            state_type = Nested


def test_artifact_name_not_on_output_type_fails():
    base = make_worker("ArtifactBase")

    with pytest.raises(ConfigError, match="artifact"):

        class Bad(base):
            artifacts = ("nope",)


def test_unconstructible_for_validation_fails():
    with pytest.raises(ConfigError, match="no arguments"):

        class NeedsArg(Worker):
            input_type = In
            output_type = Out
            prompt = "p {text}"

            def __init__(self, n: int):
                self.n = n

            def run(self, input, ctx):
                return Out(result=str(self.n))


# --- wiring failures --------------------------------------------------------


def test_no_start_edge_fails():
    a = make_worker("NoStartChild")

    with pytest.raises(ConfigError, match="START"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                g.add_node(a)
                g.add_edge(a, END)


def test_two_start_edges_fail():
    a = make_worker("TwoStartA")
    b = make_worker("TwoStartB")

    with pytest.raises(ConfigError, match="START"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                g.add_node(a)
                g.add_node(b)
                g.add_edge(START, a)
                g.add_edge(START, b)
                g.add_edge(a, END)
                g.add_edge(b, END)


def test_unreachable_node_fails():
    a = make_worker("Reachable")
    orphan = make_worker("Orphan")

    with pytest.raises(ConfigError, match="[Uu]nreachable.*Orphan"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                for n in (a, orphan):
                    g.add_node(n)
                g.add_edge(START, a)
                g.add_edge(a, END)


def test_end_unreachable_fails():
    a = make_worker("NoEndChild")

    with pytest.raises(ConfigError, match="END"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                g.add_node(a)
                g.add_edge(START, a)


def test_unknown_child_reference_fails():
    ghost = make_worker("Ghost")

    with pytest.raises(ConfigError, match="Ghost"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                g.add_edge(START, ghost)
                g.add_edge(ghost, END)


def test_conditional_target_must_be_added():
    a = make_worker("CondSrc")
    missing = make_worker("NeverAdded")

    with pytest.raises(ConfigError, match="NeverAdded"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                g.add_node(a)
                g.add_edge(START, a)
                g.add_conditional_edges(a, lambda s: "x", {"x": missing})
                g.add_edge(a, END)


def test_boundary_mismatch_entry_child_fails():
    class OtherIn(BaseModel):
        text: str

    entry = make_worker("EntryMismatched", input_type=OtherIn)

    with pytest.raises(ConfigError, match="entry child"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                g.add_node(entry)
                g.add_edge(START, entry)
                g.add_edge(entry, END)


def test_entry_child_accepting_supertype_input_passes():
    class GenIn(BaseModel):
        text: str

    gen = make_worker("SupertypeEntry", input_type=GenIn)

    class SpecificIn(GenIn):
        count: int = 0

    class Wf(Workflow):
        input_type = SpecificIn
        output_type = Out

        def build(self, g):
            g.add_node(gen)
            g.add_edge(START, gen)
            g.add_edge(gen, END)

    validate_structure(Wf)


def test_terminal_emitting_subtype_passes():
    class SubOut(Out):
        extra: str = "e"

    early = make_worker("SubtypeTerminal", output_type=SubOut)

    class Wf(Workflow):
        input_type = In
        output_type = Out

        def build(self, g):
            g.add_node(early)
            g.add_edge(START, early)
            g.add_edge(early, END)

    validate_structure(Wf)


def test_terminal_emitting_unrelated_model_fails():
    class Unrelated(BaseModel):
        x: int = 0

    bad = make_worker("UnrelatedTerminal", output_type=Unrelated)

    with pytest.raises(ConfigError, match="not a subtype"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                g.add_node(bad)
                g.add_edge(START, bad)
                g.add_edge(bad, END)


def test_composite_overriding_run_fails():
    a = make_worker("CompChild")

    def run_fn(self, input, ctx):
        return Out(result="x")

    with pytest.raises(ConfigError, match="must not override run"):

        class Bad(Workflow):
            input_type = In
            output_type = Out
            run = run_fn

            def build(self, g):
                g.add_node(a)
                g.add_edge(START, a)
                g.add_edge(a, END)


def test_leaf_without_concrete_run_fails():
    with pytest.raises(ConfigError, match=r"run\(\)"):

        class Bad(Workflow):
            input_type = In
            output_type = Out


# --- build() static lint ----------------------------------------------------


def test_os_environ_read_in_build_fails():
    a = make_worker("EnvChild")

    with pytest.raises(ConfigError, match="os.environ"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                tier = os.environ.get("NGW_TIER", "fast")
                chosen = a if tier == "fast" else a
                g.add_node(chosen)
                g.add_edge(START, chosen)
                g.add_edge(chosen, END)


def test_clock_read_in_build_fails():
    import time

    a = make_worker("ClockChild")

    with pytest.raises(ConfigError, match="time"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                if time.time() > 0:
                    g.add_node(a)
                g.add_edge(START, a)
                g.add_edge(a, END)


_MODULE_STATE: dict = {}


def test_module_global_mutation_in_build_fails():
    a = make_worker("MutChild")

    with pytest.raises(ConfigError, match="global state"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                _MODULE_STATE["n"] = 1
                g.add_node(a)
                g.add_edge(START, a)
                g.add_edge(a, END)


def test_self_mutation_in_build_fails():
    a = make_worker("SelfMutChild")

    with pytest.raises(ConfigError, match="stateless"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                self.calls = getattr(self, "calls", 0) + 1  # type: ignore[attr-defined]
                g.add_node(a)
                g.add_edge(START, a)
                g.add_edge(a, END)


def test_set_iteration_in_build_fails():
    a = make_worker("SetA")
    b = make_worker("SetB")

    with pytest.raises(ConfigError, match="set"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                for node in {a, b}:
                    g.add_node(node)
                    g.add_edge(START, node)
                    g.add_edge(node, END)


def test_local_mutation_and_list_iteration_pass():
    a = make_worker("LocalOk")

    class Fine(Workflow):
        input_type = In
        output_type = Out

        def build(self, g):
            order = [a]
            seen = {}
            for node in order:
                seen[workflow_class_path(node)] = True
                g.add_node(node)
            g.add_edge(START, a)
            g.add_edge(a, END)

    validate_structure(Fine)


# --- fan-in surface (rule 3b) -----------------------------------------------


class DraftOut(BaseModel):
    draft: str


class CritiqueOut(BaseModel):
    critique: str


class MergeInput(BaseModel):
    draft: DraftOut
    critique: CritiqueOut


def _fanin_composite(synth_input: type[BaseModel], wire, *, defer: bool = False) -> type:
    """Composite: entry worker feeds two branches; `wire` finishes the graph.

    With defer=True the class is created without import-time validation; the
    test then calls validate_structure explicitly inside its raises block.
    """
    entry = make_worker("FanEntry", input_type=In, output_type=DraftOut)
    left = make_worker(
        "FanLeft", prompt="critique {draft}", input_type=DraftOut, output_type=CritiqueOut
    )
    right = make_worker(
        "FanRight", prompt="revise {draft}", input_type=DraftOut, output_type=DraftOut
    )
    synth = make_worker("Synth", prompt="merge", input_type=synth_input)

    class Composite(Workflow):
        input_type = In
        output_type = Out
        _defer_validation = defer

        def build(self, g):
            for n in (entry, left, right, synth):
                g.add_node(n)
            g.add_edge(START, entry)
            g.add_edge(entry, left)
            g.add_edge(entry, right)
            wire(g, left, right, synth)
            g.add_edge(synth, END)

    return Composite


def test_named_slot_fan_in_passes():
    Composite = _fanin_composite(
        MergeInput,
        lambda g, left, right, synth: (
            g.add_edge(right, synth, into="draft"),
            g.add_edge(left, synth, into="critique"),
        ),
    )
    validate_structure(Composite)


def test_duplicate_slot_fails():
    Composite = _fanin_composite(
        MergeInput,
        lambda g, left, right, synth: (
            g.add_edge(right, synth, into="draft"),
            g.add_edge(left, synth, into="draft"),
        ),
        defer=True,
    )
    with pytest.raises(ConfigError, match="twice"):
        validate_structure(Composite)


def test_uncovered_required_field_fails():
    class PartialMerge(BaseModel):
        draft: DraftOut
        critique: CritiqueOut
        extra: str

    Composite = _fanin_composite(
        PartialMerge,
        lambda g, left, right, synth: (
            g.add_edge(right, synth, into="draft"),
            g.add_edge(left, synth, into="critique"),
        ),
        defer=True,
    )
    with pytest.raises(ConfigError, match="uncovered.*extra"):
        validate_structure(Composite)


def test_slot_source_type_mismatch_fails():
    class WrongMerge(BaseModel):
        draft: CritiqueOut  # right branch produces DraftOut, not CritiqueOut
        critique: CritiqueOut

    Composite = _fanin_composite(
        WrongMerge,
        lambda g, left, right, synth: (
            g.add_edge(right, synth, into="draft"),
            g.add_edge(left, synth, into="critique"),
        ),
        defer=True,
    )
    with pytest.raises(ConfigError, match="does not fit"):
        validate_structure(Composite)


def test_union_slot_fan_in_passes():
    class UnionMerge(BaseModel):
        draft: DraftOut | CritiqueOut
        critique: CritiqueOut

    Composite = _fanin_composite(
        UnionMerge,
        lambda g, left, right, synth: (
            g.add_edge(right, synth, into="draft"),
            g.add_edge(left, synth, into="critique"),
        ),
    )
    validate_structure(Composite)


def test_scalar_slot_mismatch_fails():
    class ScalarMerge(BaseModel):
        draft: str  # no model output fits a str slot
        critique: CritiqueOut

    Composite = _fanin_composite(
        ScalarMerge,
        lambda g, left, right, synth: (
            g.add_edge(right, synth, into="draft"),
            g.add_edge(left, synth, into="critique"),
        ),
        defer=True,
    )
    with pytest.raises(ConfigError, match="does not fit"):
        validate_structure(Composite)


def test_plain_edge_into_multi_parent_fails():
    Composite = _fanin_composite(
        MergeInput,
        lambda g, left, right, synth: (
            g.add_edge(right, synth),
            g.add_edge(left, synth),
        ),
        defer=True,
    )
    with pytest.raises(ConfigError, match="exactly one list"):
        validate_structure(Composite)


def test_collected_fan_in_passes():
    class ReduceInput(BaseModel):
        reviews: list[Out] = Field(min_length=2, max_length=5)

    entry = make_worker("CollEntry")
    p1 = make_worker("CollP1")
    p2 = make_worker("CollP2")
    reducer = make_worker("CollReducer", prompt="reduce {reviews}", input_type=ReduceInput)

    class Coll(Workflow):
        input_type = In
        output_type = Out

        def build(self, g):
            for n in (entry, p1, p2, reducer):
                g.add_node(n)
            g.add_edge(START, entry)
            g.add_edge(entry, p1)
            g.add_edge(entry, p2)
            g.add_edge(p1, reducer)
            g.add_edge(p2, reducer)
            g.add_edge(reducer, END)

    validate_structure(Coll)


def test_arity_below_min_fails():
    class TightMin(BaseModel):
        reviews: list[Out] = Field(min_length=3)

    entry = make_worker("ArEntry")
    p1 = make_worker("ArP1")
    p2 = make_worker("ArP2")
    reducer = make_worker("ArReducer", prompt="reduce {reviews}", input_type=TightMin)

    with pytest.raises(ConfigError, match="at least 3 parents"):

        class Coll(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                for n in (entry, p1, p2, reducer):
                    g.add_node(n)
                g.add_edge(START, entry)
                g.add_edge(entry, p1)
                g.add_edge(entry, p2)
                g.add_edge(p1, reducer)
                g.add_edge(p2, reducer)
                g.add_edge(reducer, END)


def test_arity_above_max_fails():
    class TightMax(BaseModel):
        reviews: list[Out] = Field(max_length=2)

    entry = make_worker("MaxEntry")
    ps = [make_worker(f"MaxP{i}") for i in range(3)]
    reducer = make_worker("MaxReducer", prompt="reduce {reviews}", input_type=TightMax)

    with pytest.raises(ConfigError, match="between 0 and 2 parents"):

        class Coll(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                for n in (entry, *ps, reducer):
                    g.add_node(n)
                g.add_edge(START, entry)
                for p in ps:
                    g.add_edge(entry, p)
                    g.add_edge(p, reducer)
                g.add_edge(reducer, END)


def test_two_list_fields_fail_collected():
    class TwoLists(BaseModel):
        reviews: list[Out] = Field(default_factory=list)
        extras: list[Out] = Field(default_factory=list)

    entry = make_worker("TwoListEntry")
    p1 = make_worker("TwoListP1")
    reducer = make_worker("TwoListReducer", prompt="reduce {reviews}", input_type=TwoLists)

    with pytest.raises(ConfigError, match="exactly one list"):

        class Coll(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                for n in (entry, p1, reducer):
                    g.add_node(n)
                g.add_edge(START, entry)
                g.add_edge(entry, p1)
                g.add_edge(entry, reducer)
                g.add_edge(p1, reducer)
                g.add_edge(reducer, END)


def test_human_multi_parent_fails():
    review_state = type(
        "ReviewState",
        (BaseModel,),
        {
            "__module__": __name__,
            "__annotations__": {"verdict": Literal["approve", "reject"], "notes": str},
        },
    )
    human = type(
        "multi_human",
        (Human,),
        {
            "__module__": __name__,
            "__qualname__": "multi_human",
            "input_type": In,
            "output_type": Out,
            "state_type": review_state,
            "run": lambda self, input, ctx: Out(result="x"),
        },
    )
    a = make_worker("HumanParentA")
    b = make_worker("HumanParentB")

    with pytest.raises(ConfigError, match="at most one parent"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                for n in (a, b, human):
                    g.add_node(n)
                g.add_edge(START, a)
                g.add_edge(a, b)
                g.add_edge(a, human)
                g.add_edge(b, human)
                g.add_edge(human, END)


def test_into_on_single_parent_fails():
    a = make_worker("SingleIntoSrc")
    b = make_worker("SingleIntoDst")

    with pytest.raises(ConfigError, match="single parent"):

        class Bad(Workflow):
            input_type = In
            output_type = Out

            def build(self, g):
                g.add_node(a)
                g.add_node(b)
                g.add_edge(START, a)
                g.add_edge(a, b, into="result")
                g.add_edge(b, END)


def test_entry_into_slot_passes():
    class Slotted(BaseModel):
        text: In

    entry = make_worker("SlottedEntry", prompt="go", input_type=Slotted)

    class Wf(Workflow):
        input_type = In
        output_type = Out

        def build(self, g):
            g.add_node(entry)
            g.add_edge(START, entry, into="text")
            g.add_edge(entry, END)

    validate_structure(Wf)


def test_entry_slot_unknown_field_fails():
    entry = make_worker("UnknownSlotEntry")

    with pytest.raises(ConfigError, match="is not a field"):

        class Bad(Workflow):
            input_type = In
            output_type = Out
            _defer_validation = True

            def build(self, g):
                g.add_node(entry)
                g.add_edge(START, entry, into="nope")
                g.add_edge(entry, END)

        validate_structure(Bad)


def test_entry_slot_type_mismatch_fails():
    class Other(BaseModel):
        zzz: int

    class Slotted(BaseModel):
        text: Other  # the composite input In does not fit Other

    entry = make_worker("MismatchSlotEntry", prompt="go", input_type=Slotted)

    with pytest.raises(ConfigError, match="does not fit"):

        class Bad(Workflow):
            input_type = In
            output_type = Out
            _defer_validation = True

            def build(self, g):
                g.add_node(entry)
                g.add_edge(START, entry, into="text")
                g.add_edge(entry, END)

        validate_structure(Bad)


# --- model binding ----------------------------------------------------------


def _wf_class(name, bases=(Workflow,)):
    def run(self, input, ctx):
        return Out(result="x")

    return type(
        name,
        bases,
        {
            "__module__": __name__,
            "__qualname__": name,
            "input_type": In,
            "output_type": Out,
            "run": run,
        },
    )


BindOuter = _wf_class("BindOuter")
BindMid = _wf_class("BindMid")
BindLeaf = _wf_class("BindLeaf")
BindUnrelated = _wf_class("BindUnrelated")


def test_binding_default_when_no_bindings():
    assert resolve_model_variant(BindLeaf, [BindMid, BindOuter], {}, "sonnet") == "sonnet"


def test_own_exact_key_beats_all_scopes():
    models = {
        workflow_class_path(BindLeaf): "opus",
        workflow_class_path(BindMid): "haiku",
        workflow_class_path(BindOuter): "cheap",
    }
    assert resolve_model_variant(BindLeaf, [BindMid, BindOuter], models, "default") == "opus"


def test_innermost_enclosing_scope_wins():
    models = {
        workflow_class_path(BindOuter): "haiku",
        workflow_class_path(BindMid): "sonnet",
    }
    assert resolve_model_variant(BindLeaf, [BindMid, BindOuter], models, "opus") == "sonnet"


def test_outer_scope_governs_when_inner_unbound():
    models = {workflow_class_path(BindOuter): "haiku"}
    assert resolve_model_variant(BindLeaf, [BindMid, BindOuter], models, "opus") == "haiku"


def test_unbound_scope_does_not_shadow_outer():
    # A mid scope without a binding must not block the outer scope's binding.
    models = {
        workflow_class_path(BindOuter): "haiku",
        workflow_class_path(BindMid): "sonnet",
    }
    assert resolve_model_variant(BindLeaf, [BindMid], models, "opus") == "sonnet"


def test_inheritance_never_binds():
    # Keys match concrete class paths exactly; a bound base class says nothing
    # about its subclasses.
    SubLeaf = _wf_class("SubLeaf", (BindLeaf,))
    models = {workflow_class_path(BindLeaf): "cheap"}
    assert resolve_model_variant(SubLeaf, [], models, "sonnet") == "sonnet"


def test_three_level_tree_binding():
    models = {
        workflow_class_path(BindOuter): "outer-tier",
        workflow_class_path(BindMid): "mid-tier",
        workflow_class_path(BindLeaf): "leaf-tier",
    }
    assert resolve_model_variant(BindLeaf, [BindMid, BindOuter], models, "dflt") == "leaf-tier"
    assert resolve_model_variant(BindMid, [BindOuter], models, "dflt") == "mid-tier"
    assert resolve_model_variant(BindUnrelated, [], models, "dflt") == "dflt"


# --- misc surface -----------------------------------------------------------


def test_join_path_convention_via_class_paths():
    root_path = workflow_class_path(make_worker("PathRoot"))
    inner = "pkg.mod.Inner"
    gate = "pkg.mod.Gate"
    assert join_path(root_path, inner, gate) == f"{root_path}.{inner}.{gate}"


def test_run_context_shape():
    calls = []
    ctx = RunContext(
        run_id="r1", node_path="a.b.C", emit=lambda k, p: calls.append((k, p)), provider=None
    )
    ctx.emit("node_activation", {})
    assert calls == [("node_activation", {})]
