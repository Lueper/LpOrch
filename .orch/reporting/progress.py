"""Korean progress report per task (code, doc and sys): what the run recorded about a task - timeline, worker notes
and assumptions, reviews, the author's responses, checks, changed files or the document's outline - summarized by
the provider's small_model (orch.toml) once every task is final.

Written to report/[Txx]_<YYMMDD>_<title>.md, apart from the doc deliverables, which hold their content only: a doc
task's assumptions, merge notes and unresolved findings land here. For every other kind the report is the result
document as well: the task list's result line and "ref: Txx" point at it (orchestrate.RunPipeline).
A sys task's report comes after the [sys] applies,
so it says whether its changes reached the project. Tasks blocked by "after:" get none (they did not run).
When the model gives no report, the recorded facts are written as they are instead.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

from agents.runner import AgentSpec, LoadPrompt, RunAgent, WriteText
from core.context import TaskContext
from core.paths import REPORT_DIR_NAME, DocPath, SanitizeTitle
from core.runcontext import RunContext
from core.tasks import Part, Task
from reporting.report import ROUND_NUMBER, CheckState, ReviewState, TaskStatus

MAX_ARTIFACT_CHARS = 8_000  # per artifact; the full files stay in the run folder
MAX_OUTLINE_HEADINGS = 60  # a doc task's report gets the document's headings, not its text
HEADING = re.compile(r"^#{1,6} \S")
# Agent start/finish, checks and outcomes; per-tool progress lines ("[label]   Tool ...") are left out
LOG_EVENT = re.compile(r"^\S+ (?:INFO|WARNING|ERROR) \[(?P<label>[^\]]+)\] (?! )")
# Order within a round: the review, the author's response to it, then the version that revision produced
KIND_ORDER = ("plan", "review", "response", "notes", "checks", "output", "sys")
CURRENT = 10**6  # files without ".rN." hold the final version: after every archived round


async def WriteProgressReports(tasks: list[Task], ctx: RunContext) -> None:
    """One report per task that ran, sys tasks included; called once every task, [sys] applies included, is final."""
    reported = [task for task in tasks if not task.blocked]
    await asyncio.gather(*(WriteProgressReport(task, ctx) for task in reported))


async def WriteProgressReport(task: Task, ctx: RunContext) -> None:
    try:
        facts = FactSheet(task, ctx)
        spec = AgentSpec(role="reporter", cwd=ctx.runDir, systemAppend=LoadPrompt("reporter"))  # small_model
        result = await RunAgent(f"{task.id} report", facts, spec, ctx)
        task.costUsd += result.costUsd
        report = result.text if result.ok and result.text.strip() else RawReport(task, facts, result.error or "empty reply")
        path = DocPath(task, ctx, SanitizeTitle(task.title), REPORT_DIR_NAME)  # the folder says what it is
        path.parent.mkdir(exist_ok=True)
        WriteText(path, report.rstrip() + "\n")
        task.reportRef = path.relative_to(ctx.projectDir).as_posix()
        ctx.log.info("[%s] progress report: %s", task.id, task.reportRef)
    except Exception as error:  # a report must never cost the run its results or its summary
        ctx.log.warning("[%s] progress report not written: %s: %s", task.id, type(error).__name__, error)


# ---------- facts ----------

def FactSheet(task: Task, ctx: RunContext) -> str:
    """Everything the run recorded about the task, as the reporter's prompt."""
    lines = [
        *TaskContext(task),
        "",
        "Outcome:",
        f"- Run: {ctx.runId}",
        f"- Mode: {task.mode}",
        f"- Status: {TaskStatus(task)}",
        f"- Result: {task.result or '-'}",
        f"- Result location: {task.resultRef or '-'}",
        f"- Error: {task.error or '-'}",
        f"- Notes: {'; '.join(task.notes) or '-'}",
        f"- To-dos for a person under the task (todo: lines): {'; '.join(task.todoOpen) or '-'}",
        f"- Total cost (USD, before this report): {task.totalCostUsd:.3f}",
    ]
    if task.planNote:
        lines.append(f"- Plan rationale: {task.planNote}")
    lines += [PartFacts(part) for part in task.parts]
    if task.reviews or task.checkOk is not None:  # multi-part tasks: the merged result is reviewed and checked too
        lines.append(f"- Integrated result: review {ReviewState(task)}, revisions {task.revisions}, checks {CheckState(task)}")
    lines += ["", "Timeline (run log):", *Timeline(task, ctx)]
    if task.kind == "doc":
        lines += ["", "Document outline (headings of the result; the text itself is not included):", *Outline(task, ctx)]
    lines += ["", "Artifacts:", *Artifacts(task, ctx)]
    return "\n".join(lines)


def Outline(task: Task, ctx: RunContext) -> list[str]:
    """The headings of a doc task's result: what it covers, without quoting it."""
    if not task.resultRef:
        return ["(no document written)"]
    try:
        text = ReadText(ctx.projectDir / task.resultRef)
    except OSError:
        return ["(document unreadable)"]
    headings, fenced = [], False
    for line in text.splitlines():
        fenced ^= line.lstrip().startswith("```")  # "# comment" lines inside code blocks are not headings
        if not fenced and HEADING.match(line):
            headings.append(line.rstrip())
    more = [f"(+{len(headings) - MAX_OUTLINE_HEADINGS} more headings)"] if len(headings) > MAX_OUTLINE_HEADINGS else []
    return headings[:MAX_OUTLINE_HEADINGS] + more or ["(no headings)"]


def PartFacts(part: Part) -> str:
    outcome = f"error: {part.error}" if part.error else f"notes: {'; '.join(part.notes) or '-'}"
    return (
        f"- Part {part.id} ({part.title}): review {ReviewState(part)}, revisions {part.revisions}, "
        f"checks {CheckState(part)}, cost {part.costUsd:.3f}, {outcome}"
    )


def Timeline(task: Task, ctx: RunContext) -> list[str]:
    """The task's lines of run.log: when each agent started and finished, with its cost, checks and the outcome."""
    try:
        lines = ReadText(ctx.runDir / "run.log").splitlines()
    except OSError:
        return ["(run log unreadable)"]
    return [line for line in lines if IsTaskEvent(task, line)]


def IsTaskEvent(task: Task, line: str) -> bool:
    match = LOG_EVENT.match(line)
    if not match:
        return False
    return match["label"] == task.id or match["label"].startswith((f"{task.id}.", f"{task.id} "))  # never T010


def Artifacts(task: Task, ctx: RunContext) -> list[str]:
    """The text artifacts of the task's part folders and its own folder, oldest round first."""
    folders = [ctx.runDir / part.id for part in task.parts] + [ctx.runDir / task.id]
    # A sys task's sys.diff (a code task's code.diff) is its merged change, the one applied to the project; the parts'
    # final output.diff would list the same files again
    merged = task.kind in ("code", "sys") and (ctx.runDir / task.id / f"{task.kind}.diff").is_file()
    blocks = []
    for folder in (path for path in folders if path.is_dir()):
        for path in sorted((path for path in folder.iterdir() if path.is_file()), key=ArtifactOrder):
            if merged and folder.name != task.id and path.name == "output.diff":
                continue
            text = ArtifactText(path)
            if text:
                blocks += [f"=== {folder.name}/{path.name} ===", text]
    return blocks or ["(none recorded)"]


def ArtifactOrder(path: Path) -> tuple[int, int, str]:
    match = ROUND_NUMBER.search(path.name)
    stem = path.name.split(".")[0]
    kind = KIND_ORDER.index(stem) if stem in KIND_ORDER else len(KIND_ORDER)
    return (int(match[1]) if match else CURRENT, kind, path.name)


def ArtifactText(path: Path) -> str:
    """What the reporter needs from one artifact: the final diff as changed-file counts; drafts and duplicates skipped."""
    if path.suffix == ".diff":
        return "" if ROUND_NUMBER.search(path.name) else DiffStat(ReadText(path))
    if path.suffix not in (".md", ".json") or path.name.startswith(("output", "merged")):
        return ""  # document drafts: the report gets the final document's outline instead
    if path.name.startswith("review.") and path.suffix == ".json":
        return ""  # same content as review.rN.md
    return Head(ReadText(path), MAX_ARTIFACT_CHARS)


def DiffStat(diff: str) -> str:
    """Changed files of a unified diff (git or difflib) with their added/removed line counts."""
    counts: dict[str, list[int]] = {}
    current = None
    lines = diff.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            old, new = line[4:].rstrip(), lines[index + 1][4:].rstrip()
            current = (old if new == "/dev/null" else new).split("/", 1)[-1]  # drop the a/ or b/ prefix
            counts.setdefault(current, [0, 0])
        elif line.startswith("+++ ") and index > 0 and lines[index - 1].startswith("--- "):
            continue  # second header line, handled with the first
        elif current and line.startswith("+"):
            counts[current][0] += 1
        elif current and line.startswith("-"):
            counts[current][1] += 1
    if not counts:
        return ""
    files = [f"- {name} (+{added} -{removed})" for name, (added, removed) in counts.items()]
    return "Changed files in the final version (+added -removed lines):\n" + "\n".join(files)


def Head(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}\n[truncated - {len(text)} chars in total]"


def ReadText(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def RawReport(task: Task, facts: str, reason: str) -> str:
    """Fallback when the model gives no report: the recorded facts as they are, so the task is still documented."""
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", facts)), default=0) + 1)  # outlasts fences inside
    return "\n".join([
        f"# [{task.id}] {task.title} — 진행 보고",
        "",
        f"> 자동 요약 실패({reason.replace(chr(10), ' ')})로 run 기록 원문을 그대로 남김.",
        "",
        f"{fence}text",
        facts,
        fence,
    ])
