"""Where LpOrch lives and what it writes: the project layout and the names of generated files. SDK-free."""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # type-only: keeps this module a leaf that every other module can import
    from core.runcontext import RunContext
    from core.tasks import Task

ORCH_DIR = Path(__file__).resolve().parent.parent  # .orch/: the orchestrator's code, prompts and run artifacts
PROJECT_ROOT = ORCH_DIR.parent  # the folder that contains .orch/; every run works on it
TASKS_FILE = PROJECT_ROOT / "TASKS.md"
PROMPT_DIR = ORCH_DIR / "prompts"
SKILL_DIR = PROJECT_ROOT / ".claude" / "skills"
RUNS_DIR = ORCH_DIR / "runs"  # one folder of logs, reviews and summary.md per run
WORKTREES_DIR = ORCH_DIR / "worktrees"  # git worktrees and [sys] sandboxes while a run lasts
DOC_DIR_NAME = "doc"  # doc deliverables
REPORT_DIR_NAME = "report"  # progress reports, kept apart from the doc deliverables
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
MAX_TITLE_CHARS = 40
# Files the file manager leaves in any folder (Finder, Explorer), lower case: never task material or system files
OS_METADATA_FILES = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
APPLE_DOUBLE_PREFIX = "._"  # macOS writes "._<name>" next to each file on FAT, exFAT and SMB volumes


def IsOsMetadata(name: str) -> bool:
    return name.lower() in OS_METADATA_FILES or name.startswith(APPLE_DOUBLE_PREFIX)


def SanitizeTitle(title: str) -> str:
    # Windows-safe file name: drop reserved characters, spaces -> "_", bounded length
    cleaned = INVALID_FILENAME_CHARS.sub("", title)
    cleaned = re.sub(r"\s+", "_", cleaned.strip()).strip("._")
    return cleaned[:MAX_TITLE_CHARS].rstrip("._")


def DocPath(task: Task, ctx: RunContext, title: str, dirName: str = DOC_DIR_NAME) -> Path:
    """<dirName>/[Txx]_<YYMMDD>_<title>.md (doc/ by default); never overwrites - adds _2, _3, ... when the name is taken."""
    stem = f"[{task.id}]_{ctx.runId[2:8]}_{title}"  # runId is YYYYMMDD-HHMMSS
    docDir = ctx.projectDir / dirName  # progress reports go to their own folder (REPORT_DIR_NAME)
    path, counter = docDir / f"{stem}.md", 2
    while path.exists():
        path, counter = docDir / f"{stem}_{counter}.md", counter + 1
    return path
