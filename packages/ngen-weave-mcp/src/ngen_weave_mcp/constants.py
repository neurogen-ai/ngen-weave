"""MCP-package tunables: blocking-poll cadence, tool timeout, and HTTP bind address."""

# Engine-specified status poll cadence while a run-blocking tool call waits.
POLL_INTERVAL_S = 0.25

# Default per-tool wall-clock budget for workflow runs dispatched over MCP.
DEFAULT_TOOL_TIMEOUT_S = 3600.0

# Local-only bind address by design; TLS/auth are out of scope for this server.
MCP_HTTP_HOST = "127.0.0.1"
MCP_HTTP_PORT = 8000
