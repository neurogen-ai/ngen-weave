"""PiRpcAgentExecutor behavior: session pump, repair loop, error taxonomy."""

import sys

import pytest
from pydantic import BaseModel

from ngen_weave.agent.pi_rpc import DEFAULT_RPC_TIMEOUT_S, PiRpcAgentExecutor
from ngen_weave.errors import AgentReplyError, InfraError
from ngen_weave.workflow import RunContext

# A fake pi: speaks the docs/rpc.md JSONL protocol from a scripted reply list.
# Env: FAKE_PI_REPLY_1..N are the per-round last-assistant texts (1-indexed),
# FAKE_PI_REJECT_PROMPT makes the prompt command fail, FAKE_PI_SLEEP delays the
# first turn_end. One turn_end record is CRLF-terminated on purpose.
FAKE_PI = r"""
import json, os, sys, time

replies, i = [], 1
while f"FAKE_PI_REPLY_{i}" in os.environ:
    replies.append(os.environ[f"FAKE_PI_REPLY_{i}"])
    i += 1
sleep = float(os.environ.get("FAKE_PI_SLEEP", "0"))
round_no = 0

def usage(n):
    return {"input": 10 * n, "output": 5 * n, "cacheRead": 0, "cacheWrite": 0,
            "totalTokens": 15 * n, "cost": {"total": 0.01 * n}}

def emit(obj, crlf=False):
    sys.stdout.write(json.dumps(obj) + ("\r\n" if crlf else "\n"))
    sys.stdout.flush()

def last_reply():
    return replies[min(round_no, len(replies)) - 1]

for line in sys.stdin:
    cmd = json.loads(line)
    t, rid = cmd.get("type"), cmd.get("id")
    if t == "prompt":
        if os.environ.get("FAKE_PI_REJECT_PROMPT"):
            emit({"id": rid, "type": "response", "command": "prompt",
                  "success": False, "error": "no"})
            continue
        round_no += 1
        emit({"id": rid, "type": "response", "command": "prompt", "success": True})
        emit({"type": "agent_start"})
        emit({"type": "turn_start"})
        if sleep:
            time.sleep(sleep)
        emit({"type": "turn_end", "message": {"role": "assistant", "usage": usage(round_no)},
              "toolResults": []}, crlf=True)
        emit({"type": "agent_settled"})
    elif t == "follow_up":
        round_no += 1
        emit({"id": rid, "type": "response", "command": "follow_up", "success": True})
        emit({"type": "turn_start"})
        emit({"type": "turn_end", "message": {"role": "assistant", "usage": usage(round_no)},
              "toolResults": []})
        emit({"type": "agent_settled"})
    elif t == "get_last_assistant_text":
        emit({"id": rid, "type": "response", "command": "get_last_assistant_text",
              "success": True, "data": {"text": last_reply()}})
    elif t == "abort":
        emit({"id": rid, "type": "response", "command": "abort", "success": True})
"""


class _In(BaseModel):
    text: str


class _Out(BaseModel):
    text: str


def make_env(tmp_path, **extra):
    """Write the fake pi and build executor + ctx with a fresh env."""
    script = tmp_path / "fake_pi.py"
    script.write_text(FAKE_PI)
    env = dict(extra)

    def emit(kind, payload):
        env.setdefault("emitted", []).append((kind, payload))

    timeout_s = extra.pop("timeout_s", 10.0)
    executor = PiRpcAgentExecutor(
        binary=sys.executable, extra_args=(str(script),), timeout_s=timeout_s
    )
    ctx = RunContext(run_id="r", node_path="test.node", emit=emit, provider=None)
    return executor, ctx, env


def apply_env(monkeypatch, **extra):
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


async def test_first_round_success_and_provenance(tmp_path, monkeypatch):
    apply_env(
        monkeypatch,
        FAKE_PI_REPLY_1='```json\n{"text": "hi"}\n```',
    )
    executor, ctx, env = make_env(tmp_path)
    out = await executor.execute("do it", _Out, None, None, ctx)
    assert out == _Out(text="hi")
    assert env["emitted"] and all(kind == "model_call" for kind, _ in env["emitted"])
    payload = env["emitted"][0][1]
    assert payload["variant"] is None
    assert payload["tokens_total"] == 15  # totalTokens, not 30 (double-counted)
    assert payload["cost_usd"] == 0.01
    assert isinstance(payload["duration_ms"], int) and payload["duration_ms"] >= 0


async def test_repair_loop_recovers_on_follow_up(tmp_path, monkeypatch):
    apply_env(monkeypatch, FAKE_PI_REPLY_1="not json at all", FAKE_PI_REPLY_2='{"text": "fixed"}')
    executor, ctx, env = make_env(tmp_path)
    out = await executor.execute("do it", _Out, None, None, ctx)
    assert out == _Out(text="fixed")
    kinds = [kind for kind, _ in env["emitted"]]
    assert kinds == ["model_call", "model_call"]


async def test_exhaustion_raises_agent_reply_error(tmp_path, monkeypatch):
    apply_env(monkeypatch, FAKE_PI_REPLY_1="still not json")
    executor, ctx, _ = make_env(tmp_path)
    with pytest.raises(AgentReplyError, match="no validated final answer after"):
        await executor.execute("do it", _Out, None, None, ctx)


async def test_rejected_prompt_is_infra_error(tmp_path, monkeypatch):
    apply_env(monkeypatch, FAKE_PI_REJECT_PROMPT="1")
    executor, ctx, _ = make_env(tmp_path)
    with pytest.raises(InfraError, match="pi rejected prompt"):
        await executor.execute("do it", _Out, None, None, ctx)


async def test_dead_process_is_infra_error(tmp_path):
    executor = PiRpcAgentExecutor(binary="/bin/false", timeout_s=10.0)
    ctx = RunContext(run_id="r", node_path="test.node", emit=lambda *a: None, provider=None)
    with pytest.raises(InfraError, match="exited before settling"):
        await executor.execute("do it", _Out, None, None, ctx)


async def test_session_timeout_is_infra_error(tmp_path, monkeypatch):
    apply_env(monkeypatch, FAKE_PI_SLEEP="5", FAKE_PI_REPLY_1='{"text": "late"}')
    executor, ctx, _ = make_env(tmp_path, timeout_s=0.5)
    with pytest.raises(InfraError, match="timeout"):
        await executor.execute("do it", _Out, None, None, ctx)


def test_command_shape():
    executor = PiRpcAgentExecutor(
        binary="pi", session_dir="/tmp/s", model="anthropic/claude-sonnet", extra_args=("--flag",)
    )
    assert executor._command() == [
        "pi",
        "--flag",
        "--mode",
        "rpc",
        "--session-dir",
        "/tmp/s",
        "--model",
        "anthropic/claude-sonnet",
    ]
    assert PiRpcAgentExecutor()._command() == ["pi", "--mode", "rpc", "--no-session"]
    assert DEFAULT_RPC_TIMEOUT_S == 600.0
