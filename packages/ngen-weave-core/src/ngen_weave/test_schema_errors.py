"""Tests for the friendly pydantic validation formatter."""

from enum import StrEnum

from pydantic import BaseModel, ValidationError

from ngen_weave.schema_errors import format_validation_error


class Verdict(StrEnum):
    approve = "approve"
    reject = "reject"


class Item(BaseModel):
    name: str


class State(BaseModel):
    verdict: Verdict
    notes: str
    items: list[Item]


def _exc(response: dict) -> ValidationError:
    try:
        State.model_validate(response)
    except ValidationError as exc:
        return exc
    raise AssertionError("expected validation to fail")


def test_reports_each_violation_on_its_own_line():
    report = format_validation_error(State, _exc({"verdict": "nope", "items": []}))
    lines = report.splitlines()
    assert lines[0] == "State:"
    assert any(line.startswith("  - verdict:") for line in lines[1:])
    assert any(line.startswith("  - notes:") for line in lines[1:])
    assert len([line for line in lines[1:] if line.startswith("  - ")]) >= 2


def test_nested_location_joins_with_dots():
    report = format_validation_error(
        State, _exc({"verdict": "approve", "notes": "ok", "items": [{"name": 1}, {}]})
    )
    assert "  - items.1.name: " in report


def test_no_pydantic_url_in_output():
    report = format_validation_error(State, _exc({}))
    assert "pydantic.dev" not in report
    assert "https://" not in report
