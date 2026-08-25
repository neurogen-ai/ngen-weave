#!/usr/bin/env bash
# Gating complexity check: fails if any function exceeds the ceiling.
# Usage: scripts/complexity-check.sh                  # uses max-complexity from ruff.toml
#        CX_THRESHOLD=10 scripts/complexity-check.sh # pretend the ceiling is 10
set -euo pipefail
cd "$(dirname "$0")/../.."

threshold="${CX_THRESHOLD:-}"
if [ -n "$threshold" ]; then
  echo "complexity ceiling $threshold"
  exec uv run ruff check --select C901 --config "lint.mccabe.max-complexity = $threshold" .
else
  echo "complexity ceiling from ruff.toml"
  exec uv run ruff check --select C901 .
fi
