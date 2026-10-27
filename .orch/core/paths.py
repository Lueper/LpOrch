"""Where LpOrch lives and what it writes: the project layout, the names of generated files, and the record of the
folder under src/ each code goal got. SDK-free."""
from __future__ import annotations

import json
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
WORKTREES_DIR = ORCH_DIR / "worktrees"  # sandboxes, base copies and test copies while a run lasts
DOC_DIR_NAME = "doc"  # doc deliverables
REPORT_DIR_NAME = "report"  # progress reports, kept apart from the doc deliverables
SRC_DIR_NAME = "src"  # code deliverables: one folder per origin goal, named by an agent (pipeline/phases.CodeDir)
# .orch/code_folders.json: which folder under src/ each goal got, so its later code tasks work in the same one
CODE_FOLDERS_FILE_NAME = "code_folders.json"
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


def CodeOrigin(task: Task) -> Task:
    """The goal whose folder under src/ a code task works in: the task a person wrote that its follow-up chain
    started from ("ref: Txx (origin)"), or the task itself without one."""
    return next((up for up in task.upstream if up.id == task.origin), task)


def CodeFoldersFile(projectDir: Path) -> Path:
    return projectDir / ".orch" / CODE_FOLDERS_FILE_NAME


def ReadCodeFolders(projectDir: Path) -> dict[str, dict]:
    """The code folder record: origin key (OriginKey) -> {"folder": name under src/, "title": the origin's title}.
    A missing or unreadable file is an empty record: the goals' folders are then named again."""
    try:
        data = json.loads(CodeFoldersFile(projectDir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: entry for key, entry in data.items() if isinstance(entry, dict) and entry.get("folder")}


def OriginKey(origin: Task) -> str:
    """A goal's key in the record: its Notion page id, stable whatever happens to the list; else its task ID, which a
    reset task list hands out again - the recorded title tells the two goals apart (RecordedCodeFolder)."""
    return origin.sourceId or origin.id


def SameTitle(first: str, second: str) -> bool:
    return " ".join(first.split()).lower() == " ".join(second.split()).lower()


def RecordedCodeFolder(origin: Task, projectDir: Path) -> str | None:
    """The folder name under src/ recorded for the goal, or None while it has none."""
    entry = ReadCodeFolders(projectDir).get(OriginKey(origin))
    if entry is None or not (origin.sourceId or SameTitle(str(entry.get("title", "")), origin.title)):
        return None
    return str(entry["folder"])


def RecordCodeFolder(origin: Task, projectDir: Path, name: str) -> None:
    folders = ReadCodeFolders(projectDir)
    folders[OriginKey(origin)] = {"folder": name, "title": origin.title}
    CodeFoldersFile(projectDir).write_text(json.dumps(folders, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def DocPath(task: Task, ctx: RunContext, title: str, dirName: str = DOC_DIR_NAME) -> Path:
    """<dirName>/[Txx]_<YYMMDD>_<title>.md (doc/ by default); never overwrites - adds _2, _3, ... when the name is taken."""
    stem = f"[{task.id}]_{ctx.runId[2:8]}_{title}"  # runId is YYYYMMDD-HHMMSS
    docDir = ctx.projectDir / dirName  # progress reports go to their own folder (REPORT_DIR_NAME)
    path, counter = docDir / f"{stem}.md", 2
    while path.exists():
        path, counter = docDir / f"{stem}_{counter}.md", counter + 1
    return path
