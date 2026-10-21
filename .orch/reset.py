"""Reset the orchestrator's history: generated documents, run logs, the task list in TASKS.md, and caches.

Usage (via the launcher at the project root: orch-reset.bat on Windows, orch-reset.sh on macOS/Linux):
    orch-reset           list what will be deleted and ask first     (PowerShell: .\\orch-reset, macOS/Linux: ./orch-reset.sh)
    orch-reset --yes     delete without asking
Deletes permanently: doc/*.md (subfolders such as doc/refs/ are kept), report/*.md, .orch/runs/*,
.orch/worktrees/*, the Python caches in .orch/ and its packages, and the task list of TASKS.md (from the first task
line on; the header comment stays). A Notion task list ([tasks] source = "notion") is only pointed out, never
touched - its rows are the user's data, and TASKS.md is then just their cache, rebuilt by the next run or preview.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Before the imports below, which need tomllib (3.11+): macOS's own /usr/bin/python3 is older
if sys.version_info < (3, 11):
    sys.exit(f"error: LpOrch needs Python 3.11 or newer; {sys.executable} is {sys.version.split()[0]}")

from core.config import SOURCE_NOTION, LoadConfig
from core.paths import DOC_DIR_NAME, ORCH_DIR, PROJECT_ROOT, REPORT_DIR_NAME, RUNS_DIR, TASKS_FILE, WORKTREES_DIR
from core.taskfile import TASK_PATTERN, ReadLines, TaskListStart, TaskStatus, WriteLines
from core.tasks import STATUS_RUNNING

DOC_DIR = PROJECT_ROOT / DOC_DIR_NAME
REPORT_DIR = PROJECT_ROOT / REPORT_DIR_NAME  # progress reports (reporting/progress.py)


def RunInProgress(lines: list[str]) -> str | None:
    """Why resetting now would break a running orchestrator, or None."""
    # Matched like ParseTasks does, so "-[~][doc]x" counts too
    matches = [TASK_PATTERN.match(line.rstrip("\r\n")) for line in lines]
    if any(match and TaskStatus(match) == STATUS_RUNNING for match in matches):
        return "TASKS.md has a running task [~]"
    unfinished = [path.name for path in RunDirs() if not (path / "summary.md").is_file()]
    return f"run {unfinished[-1]} has no summary.md yet" if unfinished else None


def RunDirs() -> list[Path]:
    return sorted(path for path in RUNS_DIR.iterdir() if path.is_dir()) if RUNS_DIR.is_dir() else []


def CacheDirs() -> list[Path]:
    """Python caches of .orch/ itself and of each of its packages (core/, agents/, ...)."""
    return [ORCH_DIR / "__pycache__", *ORCH_DIR.glob("*/__pycache__")]


def Documents(folder: Path) -> list[Path]:
    """The folder's own .md files; subfolders are left alone."""
    return list(folder.glob("*.md")) if folder.is_dir() else []


def Plan(lines: list[str]) -> dict[str, int]:
    tasks = [line for line in lines[TaskListStart(lines):] if TASK_PATTERN.match(line.rstrip("\r\n"))]
    return {
        "documents in doc/": len(Documents(DOC_DIR)),
        "progress reports in report/": len(Documents(REPORT_DIR)),
        "run logs in .orch/runs/": len(RunDirs()),
        "tasks in TASKS.md": len(tasks),
    }


def NotionNotice() -> str | None:
    """A reset only deletes local files, so a Notion task list is left exactly as it is."""
    config, _ = LoadConfig(PROJECT_ROOT)  # settings problems are the run's business, not the reset's
    if config.taskSource != SOURCE_NOTION:
        return None
    return ('note: the tasks come from a Notion database ([tasks] source = "notion" in orch.toml);'
            " its rows keep their status and results - delete them in Notion if you want them gone."
            " TASKS.md only caches them and gets them back on the next run or preview")


def Reset(lines: list[str]) -> None:
    for document in Documents(DOC_DIR) + Documents(REPORT_DIR):
        document.unlink()
    for path in RunDirs():
        shutil.rmtree(path)
    for folder in (WORKTREES_DIR, *CacheDirs()):  # leftovers of an aborted run, and Python caches
        shutil.rmtree(folder, ignore_errors=True)
    WriteLines(TASKS_FILE, lines[:TaskListStart(lines)])


def Main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="orch-reset", description="Delete docs, run logs and the task list.")
    parser.add_argument("--yes", action="store_true", help="delete without asking")
    args = parser.parse_args(argv)
    lines = ReadLines(TASKS_FILE) if TASKS_FILE.is_file() else []
    busy = RunInProgress(lines)
    if busy:
        print(f"error: an orchestrator run seems to be in progress ({busy}); reset after it finishes", file=sys.stderr)
        return 2
    plan = Plan(lines)
    notice = NotionNotice()
    if not any(plan.values()):
        print("Nothing to reset.")
        if notice:
            print(notice)
        return 0
    print("This permanently deletes:")
    for label, count in plan.items():
        print(f"  {count:>3} {label}")
    print(
        "  (kept: .orch code and prompts, .claude/, CLAUDE.md, orch.toml, the TASKS.md header,"
        " folders inside doc/ and report/)"
    )
    if notice:
        print(notice)
    if not args.yes and input("Delete? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return 1
    Reset(lines)
    print("Reset done.")
    return 0


if __name__ == "__main__":
    sys.exit(Main())
