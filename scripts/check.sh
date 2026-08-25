#!/usr/bin/env bash
# Run gating checks locally, same set CI runs.
# Usage:
#   scripts/check.sh                    # all checks, keeps going past failures
#   scripts/check.sh lint test          # subset of checks
# Exits nonzero if any selected check failed.
set -uo pipefail
cd "$(dirname "$0")"

all=(deps complexity-check lint format-check test build)

stages=("$@")
if [ ${#stages[@]} -eq 0 ]; then
  stages=("${all[@]}")
else
  for stage in "${stages[@]}"; do
    if [[ ! " ${all[*]} " == *" $stage "* ]]; then
      echo "unknown check: $stage" >&2
      echo "valid checks: ${all[*]}" >&2
      exit 1
    fi
  done
fi

failed=()
for stage in "${stages[@]}"; do
  echo "== $stage =="
  case "$stage" in
    deps)             ./ci/deps.sh ;;
    complexity-check) ./ci/complexity-check.sh ;;
    lint)             ./ci/lint.sh ;;
    format-check)     ./util/format-check.sh ;;
    test)             ./ci/test.sh ;;
    build)            ./ci/build.sh ;;
  esac
  rc=$?
  if [ $rc -ne 0 ]; then
    echo "-- $stage failed (rc=$rc)" >&2
    failed+=("$stage")
  fi
done

echo
if [ ${#failed[@]} -gt 0 ]; then
  echo "FAILED: ${failed[*]}" >&2
  exit 1
fi
echo "all checks passed"
