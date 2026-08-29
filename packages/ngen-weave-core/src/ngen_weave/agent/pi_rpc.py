"""Pi RPC agent executor: one activation = one headless pi subprocess.

An alternative to HarnessAgentExecutor for nodes whose loop should be pi's own
agent loop (its bash/read/grep tools, retries, compaction) instead of the
text-only JSON-envelope loop over CompletionProvider. The prompt is delivered
to `pi --mode rpc --no-session` over stdin JSONL; the final answer is the last
assistant text, validated against output_type exactly as the harness would
validate its {"output": {...}} envelope.

SAFETY: pi's own tools bypass the PermissionGate entirely — allowed_tools,
max_calls, and budget_usd are NOT enforced while pi runs. Only use this
executor on activations that are trusted with pi's full tool set (e.g. run it
in a container or a scratch checkout).

Dialogs: extension UI dialog methods (select/confirm/input/editor) emit an
extension_ui_request and block until the client answers. A headless RPC agent
has no human, so the event pump auto-responds: select picks "Allow" (or the
first option), confirm confirms, input/editor cancel. Pass
no_extensions=True to skip extension discovery entirely so packages whose
adapters emit dialogs (e.g. MCP adapters with approval gates) never load.

Metadata: each pi turn_end event maps to one model_call provenance record
(variant None) carrying tokens_total, cost_usd, and duration_ms (wall time
from turn_start), so budgets and observers still see per-turn token/cost
traffic; per-activation gate ceilings are the only accounting lost.

Wire handling follows docs/rpc.md: strict JSONL with LF framing (a trailing
\r is stripped, never split on U+2028/U+2029), id-correlated responses, and
agent_settled as the "turns are done" signal. Validation failures send a
follow_up carrying the pydantic hint — the same repair-nudge policy as the
harness, bounded by MAX_TURNS — then AgentReplyError. Spawn failures, rejected
prompts, dead processes, and timeouts raise InfraError (retryable, like any
provider outage).

Configuration rides the executor factory hatch on AgentNode:

    from functools import partial
    executor = partial(PiRpcAgentExecutor, binary="pi", cwd="/repo", model="...")
"""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, ValidationError

from ngen_weave.constants import MAX_TURNS, REPAIR_NUDGE, REPLY_EXCERPT_CHARS
from ngen_weave.errors import AgentReplyError, InfraError
from ngen_weave.schema_errors import format_validation_error

# Generous ceiling per execute() call: pi runs its own multi-turn loop, so this
# bounds the whole activation (prompt plus any follow_up rounds), not one turn.
DEFAULT_RPC_TIMEOUT_S = 600.0

# Dialog methods block until answered; fire-and-forget methods (notify,
# setStatus, setWidget, setTitle, set_editor_text) do not expect a response.
_DIALOG_METHODS = ("select", "confirm", "input", "editor")


class PiRpcAgentExecutor:
    """Runs one activation as a pi RPC session instead of a provider loop."""

    def __init__(
        self,
        provider=None,
        *,
        binary: str = "pi",
        cwd: str | None = None,
        session_dir: str | None = None,
        model: str | None = None,
        extra_args: tuple[str, ...] = (),
        no_extensions: bool = False,
        timeout_s: float = DEFAULT_RPC_TIMEOUT_S,
    ) -> None:
        """Store launch configuration; provider is accepted and ignored.

        The provider parameter exists only so the executor fits the AgentNode
        factory hatch, which always calls factory(ctx.provider).
        no_extensions launches pi with --no-extensions: extension discovery
        (user packages, including MCP adapters that gate tool calls behind
        approval dialogs) is disabled, so the session runs on pi's own tools
        only. Explicit -e extensions would still load; dialogs from any that
        slip through are auto-answered by the event pump regardless.
        """
        self._binary = binary
        self._cwd = cwd
        self._session_dir = session_dir
        self._model = model
        self._extra_args = tuple(extra_args)
        self._no_extensions = no_extensions
        self._timeout_s = timeout_s

    async def execute(
        self,
        prompt: str,
        output_type: type[BaseModel],
        permissions,
        gate,
        ctx,
    ) -> BaseModel:
        """Drive pi to completion and return the validated final answer.

        Args:
            prompt: The user-facing instruction; the output contract is
                appended, mirroring the harness's system message.
            output_type: Pydantic model the final JSON must validate against.
            permissions / gate: Mirrored signature only; pi bypasses the gate,
                so neither is consulted here (see class docstring).
            ctx: This activation's RunContext; carries emit() for model_call.

        Returns:
            The validated output_type instance parsed from pi's last reply.

        Raises:
            AgentReplyError: MAX_TURNS rounds elapsed without a validated answer.
            InfraError: pi failed to start, rejected the prompt, died, or the
                session exceeded timeout_s.
        """
        session = _RpcSession(self._command(), self._cwd, self._timeout_s)
        async with session:
            prompt_msg = self._message(prompt, output_type)
            response = await session.request(
                {"id": "prompt-1", "type": "prompt", "message": prompt_msg}
            )
            if not response.get("success"):
                raise InfraError(
                    f"{ctx.node_path}: pi rejected prompt: {response.get('error')}"
                )
            for round_no in range(MAX_TURNS):
                await session.wait_settled(ctx)
                text = await self._last_text(session, ctx)
                parsed = self._parse_final(output_type, text)
                if isinstance(parsed, BaseModel):
                    return parsed
                hint = parsed
                if round_no < MAX_TURNS - 1:
                    response = await session.request(
                        {
                            "id": f"follow-up-{round_no}",
                            "type": "follow_up",
                            "message": f"The output fields are wrong:\n{hint}\n{REPAIR_NUDGE}",
                        }
                    )
                    if not response.get("success"):
                        raise InfraError(
                            f"{ctx.node_path}: pi rejected follow_up: {response.get('error')}"
                        )
            raise AgentReplyError(
                f"{ctx.node_path}: no validated final answer after {MAX_TURNS} pi rounds\n"
                f"last reply: {text[:REPLY_EXCERPT_CHARS]!r}"
            )

    def _command(self) -> list[str]:
        """Build the pi RPC argv for this executor's configuration."""
        argv = [self._binary, *self._extra_args, "--mode", "rpc"]
        if self._no_extensions:
            argv.append("--no-extensions")
        if self._session_dir is not None:
            argv += ["--session-dir", str(self._session_dir)]
        else:
            argv.append("--no-session")
        if self._model is not None:
            argv += ["--model", self._model]
        return argv

    @staticmethod
    def _message(prompt: str, output_type: type[BaseModel]) -> str:
        """Assemble the prompt message with the output contract appended."""
        schema = json.dumps(output_type.model_json_schema(), indent=2)
        return (
            f"{prompt}\n\n"
            "Work autonomously with your tools until you can answer. Then reply with "
            "ONLY one JSON object (optionally inside a code fence) matching this "
            f"schema:\n{schema}\nNo text before or after the JSON."
        )

    @staticmethod
    async def _last_text(session: _RpcSession, ctx) -> str:
        """Fetch the last assistant text from the settled session."""
        response = await session.request({"id": "last-text", "type": "get_last_assistant_text"})
        if not response.get("success"):
            raise InfraError(
                f"{ctx.node_path}: get_last_assistant_text failed: {response.get('error')}"
            )
        return response.get("data", {}).get("text") or ""

    @staticmethod
    def _parse_final(output_type: type[BaseModel], text: str) -> BaseModel | str:
        """Parse one reply into a validated output or a repair hint string.

        Mirrors engine.runner.parse_output's parsing (fence stripped, JSON
        first, direct validation fallback) but returns the formatted hint
        instead of raising, so the caller can spend it on a follow_up round.
        """
        from ngen_weave.engine.runner import _strip_code_fence

        candidate = _strip_code_fence(text)
        try:
            try:
                return output_type.model_validate_json(candidate)
            except ValidationError:
                return output_type.model_validate(candidate)
        except ValidationError as exc:
            return format_validation_error(output_type, exc)


class _RpcSession:
    """One pi RPC subprocess: JSONL requests in, events and responses out.

    LF framing only per docs/rpc.md; each readline is bounded by the session
    deadline so a wedged pi cannot hang the activation forever.
    """

    def __init__(self, argv: list[str], cwd: str | None, timeout_s: float) -> None:
        self._argv = argv
        self._cwd = cwd
        self._timeout_s = timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self._started: float = 0.0
        self._id_seq = 0

    async def __aenter__(self) -> _RpcSession:
        self._started = asyncio.get_running_loop().time()
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            cwd=self._cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # pi emits single-line JSONL events; 64KB default truncates large tool results
            limit=16 * 1024 * 1024,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.close()

    async def request(self, payload: dict) -> dict:
        """Send one command and return its id-matched response, skipping events."""
        assert self._proc is not None and self._proc.stdin is not None
        assert payload.get("id"), "RPC commands need an id for response correlation"
        self._proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self._proc.stdin.drain()
        while True:
            event = await self._readline()
            if event.get("type") == "response" and event.get("id") == payload["id"]:
                return event

    async def wait_settled(self, ctx) -> None:
        """Pump events until agent_settled, emitting model_call per turn_end."""
        assert self._proc is not None
        turn_started: float | None = None
        while True:
            event = await self._readline()
            kind = event.get("type")
            if kind == "turn_start":
                turn_started = asyncio.get_running_loop().time()
            elif kind == "turn_end":
                duration_ms = (
                    int((asyncio.get_running_loop().time() - turn_started) * 1000)
                    if turn_started is not None
                    else None
                )
                self._emit_model_call(event, ctx, duration_ms)
            elif kind == "agent_settled":
                return

    async def _readline(self) -> dict:
        """Read and parse one JSONL record; EOF and deadline both raise InfraError."""
        assert self._proc is not None and self._proc.stdout is not None
        remaining = self._timeout_s - (asyncio.get_running_loop().time() - self._started)
        if remaining <= 0:
            raise InfraError(
                f"pi RPC session exceeded {self._timeout_s}s timeout: {' '.join(self._argv)}"
            )
        try:
            line = await asyncio.wait_for(self._proc.stdout.readline(), timeout=remaining)
        except TimeoutError as exc:
            raise InfraError(
                f"pi RPC session exceeded {self._timeout_s}s timeout: {' '.join(self._argv)}"
            ) from exc
        if not line:
            tail = await self._stderr_tail()
            raise InfraError(
                f"pi RPC process exited before settling: {' '.join(self._argv)}\n{tail}"
            )
        try:
            event = json.loads(line.decode().rstrip("\r\n"))
        except json.JSONDecodeError as exc:
            raise InfraError(f"pi emitted unparseable JSONL: {exc}: {line[:200]!r}") from exc
        if (
            event.get("type") == "extension_ui_request"
            and event.get("method") in _DIALOG_METHODS
        ):
            # A dialog blocks pi until answered; a headless agent must never
            # wedge here, so answer on its behalf and keep pumping for the
            # real event. Fire-and-forget requests fall through and are
            # discarded like any other event.
            await self._respond_dialog(event)
            return await self._readline()
        return event

    async def _respond_dialog(self, event: dict) -> None:
        """Answer one extension UI dialog automatically (no human present).

        select picks "Allow" when offered (the MCP-adapter approval shape)
        and otherwise the first option; confirm confirms; input/editor cancel
        rather than inject fabricated text. Mirrors docs/rpc.md: the response
        id must match the request id.
        """
        assert self._proc is not None and self._proc.stdin is not None
        method = event["method"]
        if method == "select":
            options = event.get("options") or []
            value = "Allow" if "Allow" in options else (options[0] if options else None)
            payload = (
                {"value": value}
                if value is not None
                else {"cancelled": True}
            )
        elif method == "confirm":
            payload = {"confirmed": True}
        else:
            payload = {"cancelled": True}
        response = {"type": "extension_ui_response", "id": event["id"], **payload}
        self._proc.stdin.write((json.dumps(response) + "\n").encode())
        await self._proc.stdin.drain()

    @staticmethod
    def _emit_model_call(event: dict, ctx, duration_ms: int | None) -> None:
        """Map one turn_end event onto the harness's model_call payload shape.

        totalTokens is authoritative when present; summing every numeric key
        would double-count it against its input/output/cache components.
        """
        usage = (event.get("message") or {}).get("usage") or {}
        total = usage.get("totalTokens")
        if isinstance(total, int | float):
            tokens = int(total)
        else:
            tokens = sum(
                value
                for key, value in usage.items()
                if key in ("input", "output", "cacheRead", "cacheWrite")
                and isinstance(value, int | float)
            )
        cost = (usage.get("cost") or {}).get("total")
        ctx.emit(
            "model_call",
            {
                "variant": None,
                "tokens_total": tokens,
                "cost_usd": float(cost) if cost is not None else 0.0,
                "duration_ms": duration_ms,
            },
        )

    async def _stderr_tail(self) -> str:
        """Best-effort tail of stderr for a dead process's error message."""
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            data = await asyncio.wait_for(self._proc.stderr.read(2000), timeout=2.0)
        except TimeoutError:
            return ""
        return data.decode(errors="replace")[-2000:]

    async def close(self) -> None:
        """Close stdin and reap the process; kill if it will not die."""
        if self._proc is None or self._proc.returncode is not None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except TimeoutError:
            self._proc.kill()
            await self._proc.wait()
