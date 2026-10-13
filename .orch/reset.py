"""Reset the orchestrator's history: generated documents, run logs, the task list in TASKS.md, and caches.

Usage (via the launcher at the project root):
    orch-reset           list what will be deleted and ask first     (PowerShell: .\\orch-reset)
    orch-reset --yes     delete without asking
Deletes permanently: doc/*.md (subfolders such as doc/refs/ are kept), report/*.md, .orch/runs/*,
.orch/worktrees/*, .orch/__pycache__, and the task list of TASKS.md (from the first task line on; the header
comment stays).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from taskfile import TASK_PATTERN, ReadLines, WriteLines

ORCH_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = ORCH_DIR.parent
TASKS_FILE = PROJECT_ROOT / "TASKS.md"
DOC_DIR = PROJECT_ROOT / "doc"
REPORT_DIR = PROJECT_ROOT / "report"  # progress reports of code tasks (progress.py)
RUNS_DIR = ORCH_DIR / "runs"
WORKSPACE_DIR = ORCH_DIR / "worktrees"
CACHE_DIR = ORCH_DIR / "__pycache__"


def TaskListStart(lines: list[str]) -> int:
    """Index of the first task line; everything before it (header comment, title) is kept."""
    return next((index for index, line in enumerate(lines) if TASK_PATTERN.match(line.rstrip("\r\n"))), len(lines))


def RunInProgress(lines: list[str]) -> str | None:
    """Why resetting now would break a running orchestrator, or None."""
    if any(line.startswith("- [~]") for line in lines):
        return "TASKS.md has a running task [~]"
    unfinished = [path.name for path in RunDirs() if not (path / "summary.md").is_file()]
    return f"run {unfinished[-1]} has no summary.md yet" if unfinished else None


def RunDirs() -> list[Path]:
    return sorted(path for path in RUNS_DIR.iterdir() if path.is_dir()) if RUNS_DIR.is_dir() else []


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


def Reset(lines: list[str]) -> None:
    for document in Documents(DOC_DIR) + Documents(REPORT_DIR):
        document.unlink()
    for path in RunDirs():
        shutil.rmtree(path)
    for folder in (WORKSPACE_DIR, CACHE_DIR):  # leftovers of an aborted run, and Python caches
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
    if not any(plan.values()):
        print("Nothing to reset.")
        return 0
    print("This permanently deletes:")
    for label, count in plan.items():
        print(f"  {count:>3} {label}")
    print("  (kept: .orch code and prompts, .claude/, CLAUDE.md, the TASKS.md header, folders inside doc/ and report/)")
    if not args.yes and input("Delete? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Cancelled.")
        return 1
    Reset(lines)
    print("Reset done.")
    return 0


if __name__ == "__main__":
    sys.exit(Main())
