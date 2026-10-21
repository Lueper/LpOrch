#!/bin/sh
# Agent orchestrator launcher for macOS/Linux - "./orch.sh" runs TASKS.md, "./orch.sh preview" shows the plan only.
# Windows uses orch.bat. Python: the project's .venv when there is one, else python3 on PATH (3.11+ either way).
root="$(cd "$(dirname "$0")" && pwd)"
python="$root/.venv/bin/python"
[ -x "$python" ] || python=python3
exec "$python" "$root/.orch/orchestrate.py" "$@"
