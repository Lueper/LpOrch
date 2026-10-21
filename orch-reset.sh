#!/bin/sh
# Reset orchestrator history: doc/*.md, report/*.md, .orch/runs, .orch/worktrees, the TASKS.md task list and caches.
# Lists what will be deleted and asks first; "./orch-reset.sh --yes" skips the question.
# Windows uses orch-reset.bat. Python: the project's .venv when there is one, else python3 on PATH (3.11+ either way).
root="$(cd "$(dirname "$0")" && pwd)"
python="$root/.venv/bin/python"
[ -x "$python" ] || python=python3
exec "$python" "$root/.orch/reset.py" "$@"
