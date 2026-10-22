"""One task's own pipeline: wait for upstream -> plan -> work -> review/revise/verify -> merge.

Standard tasks: one agent, one fresh review, revision, and a verify only when the review found CRITICAL issues.
"mode: full" tasks: planner-split parallel parts, ring review, every revision verified, merge + integration review.
Every task runs this concurrently; a task with "after:" waits for those tasks and builds on their results - and
stays pending while one of them has a "todo:" left for a person.
"""
from __future__ import annotations

import asyncio
import traceback
from datetime import datetime
from typing import Callable

from core.context import AddRefResults, RefSummary
from core.runcontext import RunContext
from core.tasks import HasCritical, OpenTodos, Part, Task
from pipeline.merge import MergeTask
from pipeline.phases import PartsNeedingRevision, ReviseParts, RunWork
from pipeline.planning import PlanTask
from pipeline.review import ReviewTask, VerifyParts
from reporting.report import UpstreamHistory


async def RunTask(task: Task, ctx: RunContext, maxRounds: int, onFinished: Callable[[Task], None]) -> None:
    try:
        problem = await WaitForUpstream(task, ctx)
        if problem:
            task.error, task.blocked = problem, True
        else:
            task.startedAt = datetime.now()
            ctx.log.info("[%s] task start (%s)", task.id, task.title)
            await RunTaskPhases(task, ctx, maxRounds)
            task.resultRun = ctx.runId  # recorded with the result: later tasks read this run's reviews
    except asyncio.CancelledError:  # the run was aborted: the task stays unfinished, so it is reported and reset as pending
        task.doneEvent.set()
        raise
    except Exception as error:  # one task's crash must not stop the others
        task.error = f"{type(error).__name__}: {error}"
        ctx.log.debug("[%s] crashed:\n%s", task.id, traceback.format_exc())
    task.finished = True
    task.finishedAt = datetime.now()
    task.doneEvent.set()  # release downstream tasks even if this one crashed
    outcome = "BLOCKED" if task.blocked else "FAILED"
    ctx.log.info("[%s] %s", task.id, f"done -> {task.result}" if task.ok else f"{outcome} - {task.error}")
    onFinished(task)  # mark TASKS.md right away instead of waiting for the slowest task


async def WaitForUpstream(task: Task, ctx: RunContext) -> str | None:
    running = [up.id for up in task.upstream if not up.doneEvent.is_set()]  # earlier runs' tasks are already done
    if running:
        ctx.log.info("[%s] waiting for %s", task.id, ", ".join(running))
    for up in task.upstream:
        await up.doneEvent.wait()
    failed = [up.id for up in task.upstream if not up.ok]
    if failed:
        return f"blocked: {', '.join(failed)} did not succeed; left pending"
    # A to-do for a person under an upstream task (e.g. a code result this run could not apply) is a premise of
    # this task: running it now would build on what is not there yet
    waiting = [f"{up.id} ({OpenTodos(up)[0]})" for up in task.upstream if OpenTodos(up)]
    return f"blocked: waiting for a person's to-do under {'; '.join(waiting)}; left pending" if waiting else None


async def RunTaskPhases(task: Task, ctx: RunContext, maxRounds: int) -> None:
    AddRefResults(task, ctx.projectDir)  # "ref: Txx" results exist only now that those tasks are done
    task.history = UpstreamHistory(task, ctx.runDir.parent)
    if task.refFiles or task.refUrls:  # show which base materials were recognized and how agents get them
        count = f"{len(task.refFiles)} file(s), {len(task.refUrls)} URL(s)"
        ctx.log.info("[%s] ref recognized: %s - %s", task.id, count, RefSummary(task, ctx.projectDir))
    if task.history:
        sources = [f"{up.id} (run {up.resultRun})" for up in task.upstream if up.resultRun]
        ctx.log.info("[%s] history recognized: %s - %d chars of reviews and responses", task.id, ", ".join(sources), len(task.history))
    await PlanTask(task, ctx)
    await RunWork(task, ctx)
    failed = [part for part in task.parts if not part.ok]
    if failed:  # a missing part would make the merged result incomplete
        task.error = "; ".join(f"{part.id}: {part.error}" for part in failed)
        return
    if maxRounds > 0:
        await ReviewParts(task, ctx, maxRounds)
    await MergeTask(task, ctx, maxRounds)


async def ReviewParts(task: Task, ctx: RunContext, maxRounds: int) -> None:
    """Review every part, revise what the review asks for, and verify the revisions that need it."""
    await ReviewTask(task, ctx, 1)  # a lone part gets a fresh reviewer; split parts review each other in a ring
    for roundNo in range(2, maxRounds + 2):
        parts = PartsNeedingRevision(task)
        if not parts:
            return
        revised = await ReviseParts(parts, ctx)
        await VerifyParts([part for part in revised if NeedsVerify(part)], ctx, roundNo)


def NeedsVerify(part: Part) -> bool:
    # Standard mode trusts WARNING-only fixes; failed checks are re-run without an agent anyway
    return part.task.full or HasCritical(part.reviews[-1])
