"""Friendly rendering of pydantic validation failures for people."""

from __future__ import annotations

from pydantic import BaseModel, ValidationError


def format_validation_error(model_type: type[BaseModel], exc: ValidationError) -> str:
    """Render exc as one `- field: message` line per violation.

    Field paths join nested locations with dots; the model name heads the
    block. This is the single formatting point for user-facing schema
    reports, whether the payload came from a person or a model.
    """
    lines = [f"{model_type.__name__}:"]
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)
