"""What agents get to see about a task: resolved "ref:" materials, upstream results, and the prompt context.

SDK-free so `preview` can resolve and list refs without claude_agent_sdk installed.
"""
from __future__ import annotations

import glob
import re
from pathlib import Path

from tasks import STATUS_DONE, SYS_APPLIED_PREFIX, Task

URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)
GLOB_CHARS = re.compile(r"[*?\[]")
HOME_PREFIX = re.compile(r"^~[\\/]")  # "~\doc\x.md" in TASKS.md means the project root, not the user's home
REF_EMBED_CHARS = 30_000  # ref text inlined into a worker/review prompt; larger files are listed for the agent to Read


# ---------- refs ----------

def ResolveRefs(task: Task, projectDir: Path) -> list[str]:
    """Expand "ref:" entries into files and URLs; returns errors for entries that match nothing."""
    errors = []
    for entry in task.refs:
        if URL_PATTERN.match(entry):
            task.refUrls.append(entry)
            continue
        files = RefFiles(entry, projectDir)
        if not files:
            errors.append(f"{task.id} ref: nothing found at '{entry}'")
        task.refFiles += [path for path in files if path not in task.refFiles]
    task.refDirs = sorted({path.parent for path in task.refFiles if not path.is_relative_to(projectDir.resolve())})
    for up in RefTasks(task):  # tasks of this run are checked once they finish (AddRefResults)
        if up.status == STATUS_DONE and not ResultFiles(up, projectDir):
            errors.append(f"{task.id} ref: {up.id} has no result file to build on ({up.resultRef or 'none recorded'})")
    return errors


def RefFiles(entry: str, projectDir: Path) -> list[Path]:
    """Files for one "ref:" entry: a file, a folder (all files inside) or a glob; relative to the project."""
    text = HOME_PREFIX.sub("", entry.strip().strip('"'))
    path = Path(text) if Path(text).is_absolute() else projectDir / text
    if path.is_file():  # literal first: names like "[T08]_x.md" are not glob patterns
        return [path.resolve()]
    if path.is_dir():
        return sorted(item.resolve() for item in path.rglob("*") if item.is_file())
    if GLOB_CHARS.search(text):
        return sorted(Path(item).resolve() for item in glob.glob(str(path), recursive=True) if Path(item).is_file())
    return []


def RefLabel(path: Path, projectDir: Path) -> str:
    root = projectDir.resolve()
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


# ---------- upstream results ----------

def RefTasks(task: Task) -> list[Task]:
    return [up for up in task.upstream if up.id in task.refTaskIds]


def ResultFiles(task: Task, projectDir: Path) -> list[Path]:
    """Files a task's result points at: its document, or the system files a sys task applied."""
    names = task.resultRef.removeprefix(SYS_APPLIED_PREFIX).split(", ") if task.resultRef.startswith(SYS_APPLIED_PREFIX) else [task.resultRef]
    paths = [projectDir / name for name in names if name]
    return [path.resolve() for path in paths if path.is_file()]


def AddRefResults(task: Task, projectDir: Path) -> None:
    """Add the result files of "ref: Txx" tasks to the base materials; called once those tasks are done."""
    for up in RefTasks(task):
        task.refFiles += [path for path in ResultFiles(up, projectDir) if path not in task.refFiles]


def NewestChange(task: Task, projectDir: Path) -> float | None:
    times = [path.stat().st_mtime for path in ResultFiles(task, projectDir)]
    return max(times) if times else None


def StaleNotices(tasks: list[Task], projectDir: Path) -> list[str]:
    """Done tasks whose upstream result changed after they were made: they were built on an older version."""
    notices = []
    for task in (task for task in tasks if task.status == STATUS_DONE):
        builtAt = NewestChange(task, projectDir)
        newer = [up.id for up in task.upstream if builtAt and (NewestChange(up, projectDir) or 0) > builtAt]
        if newer:
            notices.append(f"{task.id} was built on an older result of {', '.join(newer)}; set it back to [ ] to rebuild it")
    return notices


# ---------- prompt context ----------

def ReadRefText(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # binary (images, PDFs) or unreadable: the agent opens it with Read
        return None


def RefPlan(task: Task, embed: bool) -> list[tuple[Path, str | None]]:
    """Each ref file with its text when it is inlined into a prompt, or None when the agent reads it itself."""
    budget, plan = (REF_EMBED_CHARS if embed else 0), []
    for path in task.refFiles:
        text = ReadRefText(path) if embed else None
        fits = text is not None and len(text) <= budget
        budget -= len(text) if fits else 0
        plan.append((path, text if fits else None))
    return plan


def RefSection(task: Task, embed: bool) -> list[str]:
    """Base materials for the prompt; with embed, small text files are inlined so the agent need not look for them."""
    if not (task.refFiles or task.refUrls):
        return []
    plan = RefPlan(task, embed)
    lines = ["Base materials (ref) - the authoritative inputs for this task. Base the work on them; do not look for other versions:"]
    lines += [f"- {path} ({'included below' if text is not None else 'read it first'})" for path, text in plan]
    lines += [f"- {url} (fetch it)" for url in task.refUrls]
    for path, text in plan:
        lines += [f"=== REF {path.name} ===", text.rstrip(), "=== END REF ==="] if text is not None else []
    return lines


def RefSummary(task: Task, projectDir: Path) -> str:
    """Every ref and how the worker gets it: inlined into its prompt, read by itself, or fetched."""
    def Size(path: Path) -> str:
        try:
            return f"{path.stat().st_size / 1024:.1f} KB"
        except OSError:
            return "missing now"

    files = [f"{RefLabel(path, projectDir)} ({'inline' if text is not None else 'read'}, {Size(path)})" for path, text in RefPlan(task, True)]
    return ", ".join(files + [f"{url} (url)" for url in task.refUrls])


def TaskContext(task: Task, embedRefs: bool = False, withHistory: bool = False) -> list[str]:
    """The task as every agent sees it: title, details, output language, required check, upstream results, refs;
    with withHistory, also the upstream tasks' review findings and author responses."""
    lines = [f"Task {task.id} ({task.kind}): {task.title}"]
    if task.details:
        lines += ["Details:", task.detailText]
    if task.outputLang:
        lines.append(f"Output language: {task.outputLang} (keep established technical terms in English)")
    if task.verify:
        lines.append(f"Required check: `{task.verify}` must pass; the orchestrator runs it after every change")
    if task.upstream:
        lines.append("Builds on these earlier tasks - read their results and treat them as given inputs:")
        lines += [f"- {up.id} ({up.kind}) {up.title}: {up.resultRef or 'location not recorded'}" for up in task.upstream]
    if withHistory and task.history:
        lines += [
            "How those earlier results were reviewed (findings and the author's responses) - keep what was approved "
            "and do not reintroduce problems that were fixed or rejected for good reasons:",
            task.history,
        ]
    return lines + RefSection(task, embedRefs)
