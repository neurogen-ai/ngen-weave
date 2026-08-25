#!/usr/bin/env bash
# Run every gating check, same set CI runs.
# Usage: scripts/main.sh            # all stages
#        scripts/main.sh lint test  # subset
set -euo pipefail
cd "$(dirname "$0")/.."

stages=("$@")
if [ ${#stages[@]} -eq 0 ]; then
  stages=(deps lint test build)
fi

for stage in "${stages[@]}"; do
  case "$stage" in
    deps)
      echo "== deps =="
      uv sync --locked
      ;;
    lint)   ./scripts/lint.sh ;;
    fmt)    ./scripts/fmt.sh ;;
    test)   ./scripts/test.sh ;;
    build)  ./scripts/build.sh ;;
    cx)     ./scripts/complexity.sh ;;
    *)
      echo "unknown stage: $stage" >&2
      echo "valid stages: deps lint fmt test build cx" >&2
      exit 1
      ;;
  esac
done

echo "all checks passed"
