#!/usr/bin/env bash
# Advisory: rank functions over the cyclomatic complexity ceiling. Never fails.
# Usage: scripts/complexity.sh                        # uses max-complexity from ruff.toml
#        CX_THRESHOLD=10 scripts/complexity.sh       # pretend the ceiling is 10
set -euo pipefail
cd "$(dirname "$0")/../.."

threshold="${CX_THRESHOLD:-}"
if [ -n "$threshold" ]; then
  echo "complexity above $threshold:"
  cfg=(--config "lint.mccabe.max-complexity = $threshold")
else
  echo "complexity above ruff.toml limit:"
  cfg=()
fi

out=$(uv run ruff check --select C90 "${cfg[@]}" --output-format concise . 2>/dev/null \
  | grep C901 \
  | sed -E 's/.* \(([0-9]+) > [0-9]+\)/\1 &/' \
  | sort -rn) || true

if [ -n "$out" ]; then
  echo "$out"
else
  echo "no violations"
fi
