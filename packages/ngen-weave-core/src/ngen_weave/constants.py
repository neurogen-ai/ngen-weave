"""Library-wide tunable constants shared across ngen-weave modules."""

from pathlib import Path

# How much of the last model reply rides an exhausted AgentReplyError message.
REPLY_EXCERPT_CHARS = 500

# Sentinel for a budget dimension meaning "no cap"; accepted in run.budget
# (normalizing to None there) and honored directly by runtime enforcement.
BUDGET_UNLIMITED = -1

# Per-project state directory (runs, artifacts) rooted at the working directory.
NGEN_WEAVE_DIR = Path(".ngen-weave")

# Filename of the optional project manifest at a project root.
MANIFEST_NAME = "ngen-weave.json"

# Provider turns before agent exhaustion; one turn yields exactly one action.
MAX_TURNS = 3

# User-role message nudging the model to emit one valid JSON action after a bad reply.
REPAIR_NUDGE = (
    "Reply again with exactly one JSON object: either "
    '{"tool_call": {"name": <tool>, "args": {...}}} '
    'or {"output": {...}}.'
)
