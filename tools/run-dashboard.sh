#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ ! -x .venv-linux/bin/python ]]; then
    echo 'Run: UV_PROJECT_ENVIRONMENT=.venv-linux uv sync --extra dashboard' >&2
    exit 1
fi
exec .venv-linux/bin/python dashboard.py "$@"
