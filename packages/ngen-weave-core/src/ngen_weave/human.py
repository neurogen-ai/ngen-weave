"""Human review artifacts: response slots, prefill, completion validation.

The artifact's editable surface is generated, never hand-written: a human
node's state_type leaves become the response slots, prefill seeds them from
the edge input via path strings, and submission is plain state_type
validation. Prefill fills the artifact but never completes it; submitting the
reviewed artifact is the act that resumes the run.

Functions:
    build_response_slots: Null-seeded slot map from state_type leaf primitives.
    apply_prefill: Seed slots from the context dump via dotted paths.
    validate_completion: Validate a submitted response as the state model.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ValidationError

from ngen_weave.errors import ConfigError, DataError
from ngen_weave.schema_errors import format_validation_error

_PRIMITIVES = (str, int, float, bool)


def _is_slot_leaf(annotation: Any) -> bool:
    """True for str/int/float/bool, enum subclasses, and literals."""
    if annotation in _PRIMITIVES or (isinstance(annotation, type) and issubclass(annotation, Enum)):
        return True
    return get_origin(annotation) is Literal and all(
        isinstance(v, _PRIMITIVES + (str,)) for v in get_args(annotation)
    )


def build_response_slots(state_type: type[BaseModel]) -> dict:
    """Return the null-seeded response slots for one review artifact.

    Every leaf primitive field of state_type becomes a slot starting null,
    whether it has a default or not: fields with defaults may be left empty,
    required-without-default blocks completion at validate_completion. A
    nested BaseModel field is a ConfigError; flat models only until a concrete
    workflow needs more.
    """
    slots: dict = {}
    for name, fi in state_type.model_fields.items():
        if isinstance(fi.annotation, type) and issubclass(fi.annotation, BaseModel):
            raise ConfigError(
                f"state_type field {name!r} is a nested model; "
                "review artifacts cover flat models only"
            )
        if not _is_slot_leaf(fi.annotation):
            raise ConfigError(f"state_type field {name!r} is not a primitive slot type")
        slots[name] = None
    return slots


def apply_prefill(slots: dict, context_dump: dict, prefill: dict[str, str]) -> dict:
    """Seed slots from the context dump in place and return them.

    Keys are state field names, values are dotted path strings into the edge
    input's dump. Unknown paths are ConfigError (compile-time validation also
    checks them); a wrong-typed seed surfaces at completion instead.
    """
    for slot, path in prefill.items():
        value: Any = context_dump
        for segment in path.split("."):
            if not isinstance(value, dict) or segment not in value:
                raise ConfigError(f"prefill path {path!r} resolves to nothing in the input")
            value = value[segment]
        if slot not in slots:
            raise ConfigError(f"prefill targets unknown state field {slot!r}")
        slots[slot] = value
    return slots


def validate_completion(state_type: type[BaseModel], response: dict) -> BaseModel:
    """Validate a submitted response against the state model.

    Required-without-default fields must be present even when prefill filled
    them: no code path completes an artifact without a submission event.
    Raises DataError naming the offending fields on failure.
    """
    try:
        return state_type.model_validate(response)
    except ValidationError as exc:
        raise DataError(
            f"review response does not match {format_validation_error(state_type, exc)}"
        ) from None
