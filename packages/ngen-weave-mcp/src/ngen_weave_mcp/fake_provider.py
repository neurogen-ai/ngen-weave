"""Test-only canned-reply provider behind NGEN_WEAVE_FAKE_PROVIDER=1."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ngen_weave.models.provider import Completion

FAKE_PROVIDER_ENV = "NGEN_WEAVE_FAKE_PROVIDER"
FAKE_REPLIES_ENV = "NGEN_WEAVE_FAKE_REPLIES"


class FakeReplyProvider:
    """Sequential canned replies; NOT a product feature, e2e suites only.

    Each model call returns the next JSON reply in the list (the last one
    repeats once exhausted) and keeps deterministic token/cost accounting.
    Replies come from `NGEN_WEAVE_FAKE_REPLIES` (a path to a JSON array of
    strings); without it, a single "ok" reply is used. Activated only when
    `NGEN_WEAVE_FAKE_PROVIDER=1` is set, which stdio.py reads at startup so
    subprocess-driven tests run without real models.
    """

    def __init__(self, replies: list[str]) -> None:
        self.replies = replies or ["ok"]
        self.calls: int = 0

    async def complete(self, messages: list[dict], *, variant: str | None = None) -> Completion:
        """Return the next canned Completion."""
        text = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return Completion(
            text=text,
            tokens_in_context=100 + self.calls - 1,
            tokens_total=100 + self.calls - 1 + len(text),
            cost_usd=(100 + self.calls - 1 + len(text)) / 1000,
        )


def fake_provider_from_env() -> FakeReplyProvider | None:
    """Build the env-hooked provider when NGEN_WEAVE_FAKE_PROVIDER=1, else None.

    Raises:
        ValueError: The replies file is unreadable or is not a JSON array of
            strings.
    """
    if os.environ.get(FAKE_PROVIDER_ENV) != "1":
        return None
    replies = ["ok"]
    raw_path = os.environ.get(FAKE_REPLIES_ENV)
    if raw_path:
        parsed = json.loads(Path(raw_path).read_text())
        if not isinstance(parsed, list) or not all(isinstance(r, str) for r in parsed):
            raise ValueError(f"{raw_path}: must be a JSON array of strings")
        replies = parsed
    return FakeReplyProvider(replies)
