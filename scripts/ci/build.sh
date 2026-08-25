#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
uv build --package ngen-weave-core
uv build --package ngen-weave-cli
