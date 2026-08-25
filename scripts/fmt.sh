#!/usr/bin/env bash
# Format check. Advisory until `uv run ruff format .` has been run once;
# add this to main.sh afterwards.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run ruff format --check .
