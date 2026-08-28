"""Shared test doubles for model-boundary and e2e suites."""

from __future__ import annotations

import dataclasses
from typing import Any

from ngen_weave.engine.runner import Engine, _DriveState
from ngen_weave.engine.state import RunFile
from ngen_weave.models.provider import Completion
from ngen_weave.provenance import RunMetadata


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


class PerRunBudgetEngine(Engine):
    """Engine double resolving the run budget per workflow class path.

    Real budgets live on engine-level RunSettings, so one engine cannot
    natively express two concurrent runs whose configs differ in budget.
    The double resolves WHICH budget applies per workflow config and
    delegates every enforcement mechanism to Engine._at_boundary, so
    tests can drive one engine with distinct per-workflow budgets
    without touching production budget logic.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.per_workflow_budgets: dict[str, Any] = {}

    def _at_boundary(
        self,
        state: _DriveState,
        run_file: RunFile,
        node_path: str,
        metadata: RunMetadata | None,
    ) -> bool:
        budget = self.per_workflow_budgets.get(run_file.workflow, self._settings.budget)
        saved = self._settings
        self._settings = dataclasses.replace(saved, budget=budget)
        try:
            return super()._at_boundary(state, run_file, node_path, metadata)
        finally:
            self._settings = saved
