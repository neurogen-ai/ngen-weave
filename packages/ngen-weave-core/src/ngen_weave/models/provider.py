"""Completion boundary between ngweave and model SDKs: adapters translate, never extend."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Completion:
    """One model reply with token and cost accounting attached."""

    text: str
    tokens_in_context: int
    tokens_total: int
    cost_usd: float


class CompletionProvider(Protocol):
    """Anything that can complete a message list under a named variant."""

    async def complete(self, messages: list[dict], *, variant: str) -> Completion: ...
