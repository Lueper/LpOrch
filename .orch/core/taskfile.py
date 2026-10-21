"""TASKS.md: parsing task lines and their settings, linking "after:" dependencies, and writing status, results
and the follow-up tasks agents ask for.

The file backend of core/tasksource.py, and the cache of its Notion backend: the database's rows are written here
(ReplaceTaskList) and the run reads and writes them like any other task line, so a task is configured and proposed
the same way in both.
SDK-free so `preview` works without claude_agent_sdk.
"""
from __future__ import annotations

import re
from pathlib import Path

from core.tasks import (
    MODE_FULL, MODE_STANDARD, SINGLE_PART_KINDS, STATUS_PENDING, TASK_KINDS, FollowUp, Task, TaskFileError,
)

KINDS = "|".join(TASK_KINDS)  # "doc|code|sys|test|task", for the patterns below
# "- [ ] [doc] title" / "- [x] [code:refactorer] title" / "- [ ] [sys] title"
# Spaces between the parts are optional, "[]" or no checkbox at all is pending, and case is ignored:
# "-[doc]title", "- [][Doc] title" and "- [ ] [doc] title" are the same task (UpdateTaskStatus writes the last form)
TASK_PATTERN = re.compile(
    rf"^-\s*(?:\[\s*(?P<status>[x~!])?\s*\]\s*)?\[(?P<kind>{KINDS})(?::(?P<role>[\w-]+))?\]\s*(?P<title>\S.*?)\s*$",
    re.IGNORECASE,
)
# Lines that look like a task but are not one ("- [ ] [docs] x", "* [ ] [doc] x", "- [ ] [doc]" with no title):
# reported by UnreadTaskLines instead of being left out silently
NEAR_TASK_PATTERN = re.compile(rf"^\s*(?:[-*+]\s*\[[^\]]{{0,3}}\]|(?:[-*+]\s*)?\[\s*(?:{KINDS}))", re.IGNORECASE)
COMMENT_MARKER = re.compile(r"<!--|-->")  # the header's examples sit inside <!-- -->
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
# An agent lists the follow-up tasks it asks for below this line in its final message (pipeline/phases.py)
FOLLOWUP_MARKER = "=== FOLLOW-UP TASKS ==="
SECTION_MARKER = re.compile(r"^\s*===\s.+\s===\s*$")  # the "=== X ===" lines an agent's final message is split at
# A follow-up task line, read more leniently than a file's own: any bullet, a pasted checkbox and no bullet all count
FOLLOWUP_PATTERN = re.compile(
    rf"^(?:[-*+]\s*)?(?:\[\s*[x~!]?\s*\]\s*)?\[(?P<kind>{KINDS})(?::(?P<role>[\w-]+))?\]\s*(?P<title>\S.*?)\s*$",
    re.IGNORECASE,
)
# A list item in a follow-up block that FOLLOWUP_PATTERN did not read ("1. [code] x", "- **[code] x**", "- x"):
# reported instead of being dropped in silence, like UnreadTaskLines does for the file
UNREAD_FOLLOWUP_PATTERN = re.compile(r"^(?:[-*+•]|\d+[.)])\s*\S")
NEW_FILE_HEADER = ["# Tasks\n", "\n"]  # a task list cached into a TASKS.md that does not exist yet


def ReadLines(path: Path) -> list[str]:
    # newline="" keeps the file's own line endings when we write it back
    with open(path, encoding="utf-8-sig", newline="") as file:
        return file.read().splitlines(keepends=True)


def WriteLines(path: Path, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as file:
        file.writelines(lines)


def TaskListStart(lines: list[str]) -> int:
    """Index of the first task line; everything before it (header comment, title) is the file's header."""
    return next((index for index, line in enumerate(lines) if TASK_PATTERN.match(line.rstrip("\r\n"))), len(lines))


def ReplaceTaskList(path: Path, taskLines: list[str]) -> None:
    """Keep the file's header and replace everything from its first task line on with taskLines (no line endings).

    How a Notion task list is cached in TASKS.md (core/tasksource.py). The file is created when missing and left
    untouched when it already holds exactly these lines.
    """
    lines = ReadLines(path) if path.is_file() else []
    newline = next((LineEnding(line) for line in lines if LineEnding(line)), "\n")
    header = lines[:TaskListStart(lines)] if path.is_file() else list(NEW_FILE_HEADER)
    if header and not LineEnding(header[-1]):  # the header may end the file unterminated
        header[-1] += newline
    updated = header + [line + newline for line in taskLines]
    if updated != lines:
        WriteLines(path, updated)


# ---------- parsing ----------

def TaskStatus(match: re.Match) -> str:
    """A task line's status marker; "[ ]", "[]" and a missing checkbox are all pending, "[X]" is done."""
    return (match["status"] or STATUS_PENDING).lower()


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
                kind=match["kind"].lower(),  # "[Doc]" is a doc task
                role=match["role"],
                title=match["title"],
                lineNo=lineNo,
                status=TaskStatus(match),
            )
            tasks.append(current)
            continue
        detail = DETAIL_PATTERN.match(text)
        if current and detail:
            AddDetail(current, detail["text"])
            continue
        current = None
    return tasks


def UnreadTaskLines(path: Path) -> list[str]:
    """Warnings for lines that look like a task but are not read as one; ParseTasks skips them without a word.
    Lines inside <!-- --> comments and detail lines of a task are not checked."""
    warnings = []
    inComment = inTask = False
    for lineNo, line in enumerate(ReadLines(path), start=1):
        text = line.rstrip("\r\n")
        markers = COMMENT_MARKER.findall(text)
        commented = inComment or bool(markers)
        inComment = markers[-1] == "<!--" if markers else inComment
        if TASK_PATTERN.match(text):
            inTask = True
            continue
        if inTask and DETAIL_PATTERN.match(text):  # same rule as ParseTasks: a detail line of the task above
            continue
        inTask = False
        if not commented and NEAR_TASK_PATTERN.match(text):
            warnings.append(
                f"{path.name} line {lineNo} is not read as a task (expected: - [ ] [{KINDS}] <title>): {text.strip()}"
            )
    return warnings


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


# ---------- follow-up tasks ----------

def SplitFollowUps(text: str, origin: str = "") -> tuple[str, list[FollowUp], list[str]]:
    """(the message without its follow-up block, the follow-up tasks listed in it, the lines not read as one).

    The block runs from FOLLOWUP_MARKER to the next "=== ... ===" section or the end of the message, so an agent
    may put it anywhere after its output - before or after the notes and review-response sections, which are
    split off the returned text afterwards.
    """
    lines = text.splitlines(keepends=True)
    start = next((index for index, line in enumerate(lines) if line.strip() == FOLLOWUP_MARKER), None)
    if start is None:
        return text, [], []
    end = next((index for index in range(start + 1, len(lines)) if SECTION_MARKER.match(lines[index])), len(lines))
    items, unread = ParseFollowUps("".join(lines[start + 1:end]), origin)
    return "".join(lines[:start] + lines[end:]), items, unread


def ParseFollowUps(block: str, origin: str = "") -> tuple[list[FollowUp], list[str]]:
    """(the follow-up tasks of a block, the list items in it that are not readable as one).

    A task line follows the task list's own syntax ("- [code] title" plus indented detail lines); prose and
    empty lines are left out, while a list item that was meant as a task but cannot be read is reported.
    """
    items: list[FollowUp] = []
    unread: list[str] = []
    current: FollowUp | None = None
    for line in block.splitlines():
        text = line.rstrip()
        detail = DETAIL_PATTERN.match(text)
        if current and detail:  # same rule as ParseTasks: an indented line configures the task above it
            current.details.append(detail["text"])
            continue
        match = FOLLOWUP_PATTERN.match(text.strip())
        current = FollowUp(match["kind"].lower(), match["role"], match["title"], origin=origin) if match else None
        if current:
            items.append(current)
        elif UNREAD_FOLLOWUP_PATTERN.match(text.strip()):
            unread.append(text.strip())
    return items, unread


def AppendTask(path: Path, followUp: FollowUp) -> str:
    """Add a follow-up task as a new pending task at the end of the file, detail lines and all.

    Appending keeps the existing task IDs stable, which is what "after:" and "ref: Txx" rely on - the same rule
    the file's header asks of the user.
    """
    if not path.is_file():
        raise TaskFileError(f"Task file not found: {path}")
    lines = ReadLines(path)
    newline = next((LineEnding(line) for line in lines if LineEnding(line)), "\n")
    if lines and not LineEnding(lines[-1]):  # the file may end unterminated
        lines[-1] += newline
    role = f":{followUp.role}" if followUp.role else ""
    added = [f"- [{STATUS_PENDING}] [{followUp.kind}{role}] {followUp.title}{newline}"]
    added += [f"  {detail}{newline}" for detail in followUp.details]
    WriteLines(path, lines + added)
    return f"{path.name} line {len(lines) + 1}"


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
        if task.status == STATUS_PENDING and task.full and task.kind in SINGLE_PART_KINDS:
            errors.append(f"{task.id} mode: {MODE_FULL} is not available for {task.kind} tasks (they run as one agent)")
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
        if match and match["title"] == task.title and TaskStatus(match) == task.status:
            return lineNo
    return None


def LocateTask(lines: list[str], path: Path, task: Task) -> int:
    lineNo = FindTaskLine(lines, task)
    if lineNo is None:
        raise TaskFileError(f"{task.id} '{task.title}' is no longer in {path}; not updated")
    return lineNo


def StandardTaskLine(match: re.Match, status: str) -> str:
    role = f":{match['role']}" if match["role"] else ""
    return f"- [{status}] [{match['kind'].lower()}{role}] {match['title']}"


def UpdateTaskStatus(path: Path, task: Task, status: str) -> None:
    lines = ReadLines(path)
    lineNo = LocateTask(lines, path, task)
    # The whole line is rewritten in the standard form, so "-[doc]title" gets its checkbox and spacing;
    # kind and role come from the line itself, keeping edits made mid-run
    match = TASK_PATTERN.match(lines[lineNo].rstrip("\r\n"))
    lines[lineNo] = StandardTaskLine(match, status) + LineEnding(lines[lineNo])
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
