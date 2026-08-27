"""Shared test doubles for model-boundary and e2e suites."""

from __future__ import annotations

from ngen_weave.models.provider import Completion


class FakeProvider:
    """Canned-reply CompletionProvider with deterministic accounting.

    Each call returns the next reply in the list (the last one repeats once
    exhausted). Token counts derive mechanically from call order and reply
    length: tokens_in_context = 100 + call index; tokens_total adds the reply
    length; cost is tokens_total / 1000. Every (messages, variant) pair is
    recorded in `calls`, so suites assert which variant actually ran.
    """

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies) if replies is not None else ["ok"]
        self.calls: list[tuple[list[dict], str | None]] = []

    async def complete(self, messages: list[dict], *, variant: str | None = None) -> Completion:
        """Return the next canned Completion and record the call."""
        index = min(len(self.calls), len(self.replies) - 1)
        self.calls.append((messages, variant))
        text = self.replies[index]
        tokens_in_context = 100 + len(self.calls) - 1
        tokens_total = tokens_in_context + len(text)
        return Completion(
            text=text,
            tokens_in_context=tokens_in_context,
            tokens_total=tokens_total,
            cost_usd=tokens_total / 1000,
        )
