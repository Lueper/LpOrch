"""Korean goal report per origin: once the follow-up chain of a task a person wrote ("ref: T01 (origin)") has ended
or waits on a person's to-do, one report says what the chain achieved against that task's goal, what is left, and
what a person has to do - instead of the user piecing it together from each task's progress report.

Written to report/[Txx]_<YYMMDD>_목표_보고_<title>.md under the origin's ID, by the provider's small_model
(orch.toml, the reporter role) with prompts/goal.md, after the run's progress reports. [tasks] goal_reports = false
turns it off. When the model gives no report, the recorded facts are written as they are instead.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from agents.runner import AgentSpec, LoadPrompt, RunAgent, WriteText
from core.paths import REPORT_DIR_NAME, DocPath, SanitizeTitle
from core.runcontext import RunContext
from core.tasks import (
    STATUS_DONE, STATUS_FAILED, STATUS_PENDING, STATUS_RUNNING, FollowUp, OpenTodos, Task, TodoBlockers,
)

GOAL_TITLE = "목표 보고"  # in the file name, before the origin's title: report/[T01]_<YYMMDD>_목표_보고_<title>.md
MAX_SECTION_CHARS = 1_500  # per section quoted from a chain task's progress report
REPORT_SECTIONS = ("## 개요", "## 남은 이슈 및 후속 조치")  # what the goal report needs of each progress report
NOT_ADDED_NOTE = "follow-up task(s) not added"  # orchestrate.AddFollowUpTasks' note when a chain limit stopped one
STATUS_NAMES = {STATUS_DONE: "done", STATUS_PENDING: "pending", STATUS_RUNNING: "running", STATUS_FAILED: "failed"}
# A follow-up added this run: (its task ID, the task as proposed, the task that proposed it)
Added = tuple[str, FollowUp, Task]


@dataclass
class GoalChain:
    """An origin and the follow-ups that grew from it, as they stand when the run ends."""

    origin: Task
    tasks: list[Task]  # the chain's tasks already in the task list, in ID order
    added: list[Added]  # follow-ups this run added to it (pending, not yet run)
    waiting: list[str]  # "Txx: <to-do>" a person must do before the chain can go on; empty when it simply ended


@dataclass
class GoalReport:
    originId: str
    path: str  # relative to the project
    costUsd: float


def StalledChains(ran: list[Task], allTasks: list[Task], added: list[Added]) -> list[GoalChain]:
    """The chains this run moved that cannot go on by themselves now: every task left is done or failed, or waits on
    a person's to-do. A chain with something still runnable - a pending task, a follow-up just added - goes on in a
    later run and gets its report then; a task without follow-ups is no chain."""
    byId = {task.id: task for task in allTasks}
    chains = []
    for originId in dict.fromkeys(task.origin or task.id for task in ran):
        origin = byId.get(originId)
        if origin is None:
            continue
        tasks = [task for task in allTasks if task.origin == originId]
        new = [item for item in added if (item[2].origin or item[2].id) == originId]
        if not tasks and not new:
            continue
        waiting = [f"{origin.id}: {item}" for item in OpenTodos(origin)]
        for task in tasks:
            waiting += [f"{task.id}: {item}" for item in OpenTodos(task)] if task.status == STATUS_DONE else []
        runnable = [task for task in tasks if task.status == STATUS_PENDING and not TodoBlockers(task)]
        runnable += [item for item in new if not OpenTodos(item[2]) and not OpenTodos(origin)]
        if not runnable:
            chains.append(GoalChain(origin, tasks, new, list(dict.fromkeys(waiting))))
    return chains


async def WriteGoalReports(chains: list[GoalChain], ctx: RunContext) -> list[GoalReport]:
    reports = await asyncio.gather(*(WriteGoalReport(chain, ctx) for chain in chains))
    return [report for report in reports if report]


async def WriteGoalReport(chain: GoalChain, ctx: RunContext) -> GoalReport | None:
    origin = chain.origin
    try:
        facts = GoalFacts(chain, ctx)
        spec = AgentSpec(role="reporter", cwd=ctx.runDir, systemAppend=LoadPrompt("goal"))  # small_model
        # Not "T01 ...": a label that starts with a task ID is counted as that task's call (benchmark.py, the timeline
        # of its progress report), while this cost goes to the run's total only
        result = await RunAgent(f"goal {origin.id}", facts, spec, ctx)
        report = result.text if result.ok and result.text.strip() else RawGoalReport(origin, facts, result.error or "empty reply")
        path = DocPath(origin, ctx, SanitizeTitle(f"{GOAL_TITLE} {origin.title}"), REPORT_DIR_NAME)
        path.parent.mkdir(exist_ok=True)
        WriteText(path, report.rstrip() + "\n")
        relative = path.relative_to(ctx.projectDir).as_posix()
        ctx.log.info("[%s] goal report: %s", origin.id, relative)
        return GoalReport(origin.id, relative, result.costUsd)
    except Exception as error:  # a report must never cost the run its results or its summary
        ctx.log.warning("[%s] goal report not written: %s: %s", origin.id, type(error).__name__, error)
        return None


# ---------- facts ----------

def GoalFacts(chain: GoalChain, ctx: RunContext) -> str:
    """Everything the task list and the progress reports say about the chain, as the goal reporter's prompt."""
    origin = chain.origin
    lines = [
        f"Origin (the goal): {origin.id} ({origin.kind}) {origin.title}",
        *(["Details:", origin.detailText] if origin.details else []),
        f"Origin result: {origin.resultRef or '-'}",
        f"Run: {ctx.runId}",
        "State: " + ("waiting for a person's to-do" if chain.waiting else "ended - nothing left that can run"),
        "",
        "Tasks of the chain (origin first, in ID order):",
        *(TaskLine(task) for task in [origin, *chain.tasks]),
    ]
    if chain.added:
        lines += ["", "Follow-ups added to the task list in this run (pending; a later run executes them):"]
        lines += [f"- {taskId} [{item.kind}] {item.title} (proposed by {parent.id})" for taskId, item, parent in chain.added]
    notAdded = [note for task in [origin, *chain.tasks] for note in task.notes if note.startswith(NOT_ADDED_NOTE)]
    if notAdded:
        lines += ["", "Follow-ups proposed but not added (a chain limit of orch.toml stopped them):", *(f"- {note}" for note in notAdded)]
    lines += ["", "To-dos for a person still open (todo: lines in the task list):", *(f"- {item}" for item in chain.waiting or ["(none)"])]
    lines += ["", "Progress reports of the chain (overview and remaining issues of each):"]
    for task in [origin, *chain.tasks]:
        lines += ReportExcerpt(task, ctx.projectDir)
    return "\n".join(lines)


def TaskLine(task: Task) -> str:
    status = STATUS_NAMES.get(task.status, task.status)
    parts = [f"- {task.id} [{task.kind}] {task.title} - status {status}", f"result {task.resultRef or '-'}"]
    if task.todoOpen:
        parts.append(f"open to-dos: {'; '.join(task.todoOpen)}")
    if task.todoDone:
        parts.append(f"to-dos done: {'; '.join(task.todoDone)}")
    if task.error:
        parts.append(f"error: {task.error}")
    return "; ".join(parts)


def ReportExcerpt(task: Task, projectDir: Path) -> list[str]:
    """The overview and remaining-issues sections of the task's newest progress report."""
    path = LatestReport(task, projectDir)
    if path is None:
        return [f"=== {task.id}: no progress report found ==="]
    text = path.read_text(encoding="utf-8", errors="replace")
    sections = [Section(text, heading) for heading in REPORT_SECTIONS]
    return [f"=== {task.id} {path.relative_to(projectDir).as_posix()} ===", *(section for section in sections if section)]


def LatestReport(task: Task, projectDir: Path) -> Path | None:
    folder = projectDir / REPORT_DIR_NAME
    if not folder.is_dir():
        return None
    goal = SanitizeTitle(GOAL_TITLE)
    found = [path for path in folder.iterdir() if path.name.startswith(f"[{task.id}]_") and goal not in path.name]
    return max(found, key=lambda path: path.stat().st_mtime, default=None)


def Section(text: str, heading: str) -> str:
    """One "## " section of a report, heading included, cut to MAX_SECTION_CHARS."""
    match = re.search(rf"^{re.escape(heading)}\s*$(?P<body>.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    body = match["body"].strip()
    body = body if len(body) <= MAX_SECTION_CHARS else f"{body[:MAX_SECTION_CHARS]}\n[truncated]"
    return f"{heading}\n{body}"


def RawGoalReport(origin: Task, facts: str, reason: str) -> str:
    """Fallback when the model gives no report: the recorded facts as they are."""
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", facts)), default=0) + 1)
    return "\n".join([
        f"# [{origin.id}] {origin.title} — 목표 종합 보고",
        "",
        f"> 자동 요약 실패({reason.replace(chr(10), ' ')})로 기록 원문을 그대로 남김.",
        "",
        f"{fence}text",
        facts,
        fence,
    ])
