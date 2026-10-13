---
name: orch
description: Run the agent orchestrator on TASKS.md (parallel work, cross review, merge) and report the outcome
disable-model-invocation: true
argument-hint: "[preview]"
allowed-tools: Bash(python .orch/orchestrate.py:*), Read, Glob
---

# Run the Agent Orchestrator

## Steps
1. If `$ARGUMENTS` is `preview`, run `python .orch/orchestrate.py preview` from the project root, show the output, and stop.
2. Otherwise run `python .orch/orchestrate.py` from the project root with the Bash tool and `run_in_background: true`. A run can take many minutes. Never start a second run while one is in progress.
3. When the background command finishes, read the newest `.orch/runs/*/summary.md` and report:
   - each task's status, number of parts, review result (including integration review and automated checks), and cost
   - where each task's result went: one document per doc task under `doc/` (`[Txx]_YYMMDD_title.md`), code changes staged in the working tree (`git diff --cached`), and system files applied by `[sys]` tasks (originals backed up in `.orch/runs/<run-id>/<Txx>/backup/`, full diff in `sys.diff`)
   - each code task's Korean progress report (`report/[Txx]_YYMMDD_<title>.md`, listed under "Progress reports" in `summary.md`; doc and sys tasks get none)
   - failed tasks with their reason (they are marked `[!]` in `TASKS.md`; set them back to `[ ]` to retry) and blocked tasks (left `[ ]` because an `after:` task did not succeed)
4. Do not commit, and do not edit the files the run produced.
