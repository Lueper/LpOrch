"""TASKS.md: parsing task lines and their settings, linking "after:" dependencies, and writing status and results.

SDK-free so `preview` works without claude_agent_sdk installed.
"""
from __future__ import annotations

import re
from pathlib import Path

from tasks import MODE_FULL, MODE_STANDARD, STATUS_PENDING, Task, TaskFileError

# "- [ ] [doc] title" / "- [x] [code:refactorer] title" / "- [ ] [sys] title"
TASK_PATTERN = re.compile(
    r"^- \[(?P<status>[ x~!])\] \[(?P<kind>doc|code|sys)(?::(?P<role>[\w-]+))?\] (?P<title>.+?)\s*$"
)
# Indented lines right below a task carry its detailed instructions
DETAIL_PATTERN = re.compile(r"^(?:\s{2,}|\t)(?P<text>\S.*?)\s*$")
# Detail lines that configure the task instead of describing it
# An optional leading arrow marks lines the orchestrator wrote ("→ result: ...")
DIRECTIVE_PATTERN = re.compile(
    r"^(?:(?:→|->)\s*)?(?P<key>after|ref|lang|verify|mode|result)\s*:\s*(?P<value>.*)$", re.IGNORECASE
)
RESULT_ARROW = "→"
RESULT_RUN_PATTERN = re.compile(r"^(?P<ref>.*?)\s*\(run (?P<run>\d{8}-\d{6})\)$")  # "doc/x.md (run 20261008-101500)"
TASK_ID_PATTERN = re.compile(r"T(\d+)", re.IGNORECASE)
TASK_REF_PATTERN = re.compile(r"^T(\d+)$", re.IGNORECASE)  # "ref: T03" = T03's result


def ReadLines(path: Path) -> list[str]:
    # newline="" keeps the file's own line endings when we write it back
    with open(path, encoding="utf-8-sig", newline="") as file:
        return file.read().splitlines(keepends=True)


def WriteLines(path: Path, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as file:
        file.writelines(lines)


# ---------- parsing ----------

def ParseTasks(path: Path) -> list[Task]:
    if not path.is_file():
        raise TaskFileError(f"Task file not found: {path}")
    tasks: list[Task] = []
    current: Task | None = None
    for lineNo, line in enumerate(ReadLines(path)):
        text = line.rstrip("\r\n")
        match = TASK_PATTERN.match(text)
        if match:
            current = Task(
                id=f"T{len(tasks) + 1:02d}",
                kind=match["kind"],
                role=match["role"],
                title=match["title"],
                lineNo=lineNo,
                status=match["status"],
            )
            tasks.append(current)
            continue
        detail = DETAIL_PATTERN.match(text)
        if current and detail:
            AddDetail(current, detail["text"])
            continue
        current = None
    return tasks


def AddDetail(task: Task, text: str) -> None:
    directive = DIRECTIVE_PATTERN.match(text)
    if not directive:
        task.details.append(text)
        return
    key, value = directive["key"].lower(), directive["value"].strip()
    if key == "after":
        task.after += [f"T{int(number):02d}" for number in TASK_ID_PATTERN.findall(value)]
    elif key == "ref" and TASK_REF_PATTERN.match(value):  # a task ID: build on its result, waiting if it runs now
        taskId = f"T{int(TASK_REF_PATTERN.match(value)[1]):02d}"
        task.refTaskIds.append(taskId)
        task.after.append(taskId)
    elif key == "ref":
        task.refs += [value] if value else []
    elif key == "lang":
        task.lang = value or None
    elif key == "verify":
        task.verify = value or None
    elif key == "mode":
        task.mode = value.lower() or MODE_STANDARD
    else:
        withRun = RESULT_RUN_PATTERN.match(value)
        task.resultRef, task.resultRun = (withRun["ref"], withRun["run"]) if withRun else (value, "")


# ---------- dependencies ----------

def LinkDependencies(tasks: list[Task]) -> list[str]:
    """Resolve every task's "after:" IDs to tasks and check its settings; returns errors that stop pending tasks."""
    byId = {task.id: task for task in tasks}
    errors = []
    for task in tasks:
        task.upstream = [byId[dep] for dep in dict.fromkeys(task.after) if dep in byId and dep != task.id]
        unknown = [dep for dep in task.after if dep not in byId]
        if task.status == STATUS_PENDING and unknown:
            errors.append(f"{task.id} after: unknown task {', '.join(unknown)}")
        if task.status == STATUS_PENDING and task.id in task.after:
            errors.append(f"{task.id} after: a task cannot wait for itself")
        if task.status == STATUS_PENDING and task.mode not in (MODE_STANDARD, MODE_FULL):
            errors.append(f"{task.id} mode: unknown mode '{task.mode}' (use: {MODE_FULL})")
    cycle = FindCycle(tasks)
    if cycle:
        errors.append(f"dependency cycle: {' -> '.join(cycle)}")
    return errors


def FindCycle(tasks: list[Task]) -> list[str] | None:
    """A loop of pending tasks waiting on each other, or None."""
    visiting: list[Task] = []
    cleared: set[str] = set()

    def Visit(task: Task) -> list[str] | None:
        if task.id in cleared:
            return None
        if task in visiting:
            return [item.id for item in visiting[visiting.index(task):]] + [task.id]
        visiting.append(task)
        for up in task.upstream:
            cycle = Visit(up) if up.status == STATUS_PENDING else None
            if cycle:
                return cycle
        visiting.pop()
        cleared.add(task.id)
        return None

    pending = [task for task in tasks if task.status == STATUS_PENDING]
    return next((cycle for cycle in map(Visit, pending) if cycle), None)


# ---------- writing back ----------

def FindTaskLine(lines: list[str], task: Task) -> int | None:
    # Try the original line first, then scan: the user may have edited the file mid-run
    for lineNo in [task.lineNo, *range(len(lines))]:
        if lineNo >= len(lines):
            continue
        match = TASK_PATTERN.match(lines[lineNo].rstrip("\r\n"))
        if match and match["title"] == task.title and match["status"] == task.status:
            return lineNo
    return None


def LocateTask(lines: list[str], path: Path, task: Task) -> int:
    lineNo = FindTaskLine(lines, task)
    if lineNo is None:
        raise TaskFileError(f"{task.id} '{task.title}' is no longer in {path}; not updated")
    return lineNo


def UpdateTaskStatus(path: Path, task: Task, status: str) -> None:
    lines = ReadLines(path)
    lineNo = LocateTask(lines, path, task)
    lines[lineNo] = lines[lineNo].replace(f"- [{task.status}]", f"- [{status}]", 1)
    WriteLines(path, lines)
    task.status = status
    task.lineNo = lineNo


def LineEnding(line: str) -> str:
    return line[len(line.rstrip("\r\n")):]


def IsResultLine(line: str) -> bool:
    detail = DETAIL_PATTERN.match(line.rstrip("\r\n"))
    directive = DIRECTIVE_PATTERN.match(detail["text"]) if detail else None
    return bool(directive) and directive["key"].lower() == "result"


def RecordResult(path: Path, task: Task) -> None:
    """Write "→ result: <resultRef>" on its own line after the task's other lines, replacing an earlier one,
    so later runs can build on it with "after:"."""
    lines = ReadLines(path)
    lineNo = LocateTask(lines, path, task)
    newline = next((LineEnding(line) for line in lines if LineEnding(line)), "\n")
    end = lineNo + 1
    while end < len(lines) and DETAIL_PATTERN.match(lines[end].rstrip("\r\n")):
        end += 1
    kept = [lines[lineNo], *(line for line in lines[lineNo + 1:end] if not IsResultLine(line))]
    kept = [line if LineEnding(line) else line + newline for line in kept]  # the block may end the file unterminated
    value = re.sub(r"[\r\n]+", " ", task.resultRef)
    run = f" (run {task.resultRun})" if task.resultRun else ""  # later tasks find its reviews in that run
    lines[lineNo:end] = [*kept, f"  {RESULT_ARROW} result: {value}{run}{newline}"]
    WriteLines(path, lines)
