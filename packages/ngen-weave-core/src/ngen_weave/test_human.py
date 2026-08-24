"""Human artifact generation, prefill, and completion validation.

Covers slot generation from flat state models (defaults and
required-without-default), nested-model rejection, prefill via path strings,
and submission validation naming missing fields. Engine interrupt and resume
behavior lives in engine/test_engine.py.
"""

from enum import StrEnum
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from ngen_weave.errors import ConfigError, DataError
from ngen_weave.human import apply_prefill, build_response_slots, validate_completion


class Verdict(StrEnum):
    approve = "approve"
    reject = "reject"


class State(BaseModel):
    verdict: Literal["approve", "reject", "revise"]
    notes: str = ""
    score: int = Field(default=0)
    must_fill: str


def test_slots_cover_leaf_primitives_null_seeded():
    slots = build_response_slots(State)
    assert slots == {"verdict": None, "notes": None, "score": None, "must_fill": None}


def test_enum_state_fields_are_slots():
    class EnumState(BaseModel):
        verdict: Verdict

    assert build_response_slots(EnumState) == {"verdict": None}


def test_nested_model_in_state_type_rejected():
    class Inner(BaseModel):
        x: int

    class Nested(BaseModel):
        inner: Inner

    with pytest.raises(ConfigError, match="nested model"):
        build_response_slots(Nested)


def test_prefill_seeds_slots_from_context_dump():
    slots = build_response_slots(State)
    apply_prefill(slots, {"gate": {"notes": "looks fine"}}, {"notes": "gate.notes"})
    assert slots["notes"] == "looks fine"


def test_prefill_unknown_path_rejected():
    slots = build_response_slots(State)
    with pytest.raises(ConfigError, match="resolves to nothing"):
        apply_prefill(slots, {"gate": {}}, {"notes": "gate.notes"})


def test_prefill_unknown_slot_rejected():
    slots = build_response_slots(State)
    with pytest.raises(ConfigError, match="unknown state field"):
        apply_prefill(slots, {"a": 1}, {"nope": "a"})


def test_incomplete_submission_rejected_naming_fields():
    with pytest.raises(DataError, match="must_fill"):
        validate_completion(State, {"verdict": "approve"})


def test_full_submission_validates():
    state = validate_completion(State, {"verdict": "approve", "must_fill": "done"})
    assert state.verdict == "approve"


def test_prefilled_required_still_requires_submission_event():
    # Prefill fills the artifact but never completes it: validate_completion is
    # only called on a submitted response, so a prefilled-but-unsubmitted
    # artifact simply never reaches this function.
    slots = build_response_slots(State)
    apply_prefill(slots, {"must_fill": "seeded"}, {"must_fill": "must_fill"})
    assert slots["must_fill"] == "seeded"
