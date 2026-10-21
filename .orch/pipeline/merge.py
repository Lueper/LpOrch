"""Merge one task's parts into its own result, review the integrated result, and deliver it:
doc/[Txx]_<YYMMDD>_<title>.md, staged code changes, system files applied when the run ends, a test's report only,
or the tasks a [task] task wrote, added to the task list when the run ends.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from agents.runner import AgentSpec, LoadPrompt, RunAgent, WriteText
from core.paths import DocPath, SanitizeTitle
from core.runcontext import RunContext
from core.taskfile import ParseFollowUps
from core.tasks import MAX_WRITTEN_TASKS, SYS_APPLIED_PREFIX, Part, Task
from pipeline.phases import (
    FOLLOWUP_DELIVERY, MAX_UNREAD_SHOWN, ArchiveAndWrite, Clip, FollowUpText, GitCall, RevisionRequest, RunChecks,
    SplitNotes, SplitResponse, TakeFollowUps, WorkerSystemPrompt,
)
from pipeline.review import IntegrationLoop
from reporting.report import BlockingIssues, OpenIssues, ReviewState
from workspace import gitops, sandbox

TITLE_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
NAMER_INPUT_CHARS = 3000  # the head of the document is enough to name it


async def MergeTask(task: Task, ctx: RunContext, maxRounds: int) -> None:
    merge = {
        "doc": MergeDocTask, "code": MergeCodeTask, "sys": MergeSysTask, "test": MergeTestTask, "task": MergeTaskList,
    }[task.kind]
    await merge(task, ctx, maxRounds)


def Blocked(task: Task, what: str) -> bool:
    """Fail the task when unresolved CRITICAL findings or failed checks remain."""
    issues = BlockingIssues(task)
    if issues:
        task.error = f"{what}: {'; '.join(issues)}"
    return bool(issues)


# ---------- documents ----------

async def MergeDocTask(task: Task, ctx: RunContext, maxRounds: int) -> None:
    content, source, session = await DocContent(task, ctx)
    if session and maxRounds > 0:  # several parts merged by an agent: review the whole
        content = await ReviewMergedDoc(task, ctx, maxRounds, content, session)
    # The document holds only its content: unresolved findings go to the progress report and summary.md
    path = DocPath(task, ctx, await KoreanTitle(task, content, ctx))
    path.parent.mkdir(exist_ok=True)
    WriteText(path, content.rstrip() + "\n")
    task.resultRef = path.relative_to(ctx.projectDir).as_posix()
    task.result = f"{task.resultRef} ({source})"
    Blocked(task, f"written to {task.resultRef} for inspection, but unresolved")


async def DocContent(task: Task, ctx: RunContext) -> tuple[str, str, str | None]:
    """(document, how it was built, merger session); one part is used as-is, several are merged by an agent."""
    if len(task.parts) == 1:
        return (ctx.TaskDir(task.parts[0].id) / "output.md").read_text(encoding="utf-8"), "1 part", None
    spec = AgentSpec(role="doc-merger", cwd=ctx.runDir, systemAppend=LoadPrompt("merger"))
    delivery = f"\n\n{FOLLOWUP_DELIVERY}" if ctx.config.followUps else ""  # a gap no part covers can become a task
    prompt = (
        f"Assemble the parts of task {task.id} ({task.title}) into one document. "
        "Parts are listed in the planned order.\n\n" + "\n\n".join(PartBlock(part, ctx) for part in task.parts)
        + delivery
    )
    result = await RunAgent(f"{task.id} merge", prompt, spec, ctx)
    task.costUsd += result.costUsd
    if result.ok:
        document, notes = SplitNotes(TakeFollowUps(task, result.text, ctx))
        if notes:  # how the merger resolved conflicts between parts: for the progress report
            WriteText(ctx.TaskDir(task.id) / "notes.md", notes)
        return document, f"merged {len(task.parts)} parts", result.sessionId
    # Fallback: concatenate so the task still yields a usable document
    task.notes.append(f"merge agent failed ({result.error}); parts concatenated")
    return "\n\n".join(PartBlock(part, ctx) for part in task.parts), f"{len(task.parts)} parts concatenated", None


def PartBlock(part: Part, ctx: RunContext) -> str:
    header = f"### {part.id}: {part.title} [{ReviewState(part)}]"
    issues = OpenIssues(part)
    content = Clip(ctx.TaskDir(part.id) / "output.md")
    return "\n\n".join(item for item in (header, f"Brief: {part.brief}", issues, content) if item)


async def ReviewMergedDoc(task: Task, ctx: RunContext, maxRounds: int, content: str, session: str) -> str:
    """Integration review of the merged document; the merger's own session fixes what it finds."""
    merged = ctx.TaskDir(task.id) / "merged.md"
    WriteText(merged, content)
    state = {"session": session, "rounds": 0}

    async def Snapshot() -> str:  # with the merger's own follow-up proposals: they are reviewed like the document
        return "\n\n".join(item for item in (Clip(merged), FollowUpText(task)) if item)

    async def Fix(review: dict) -> bool:
        spec = AgentSpec(role="doc-merger", cwd=ctx.runDir, systemAppend=LoadPrompt("merger"), resume=state["session"])
        request = RevisionRequest(f"A reviewer checked the merged document of task {task.id}.", review, "", "doc")
        result = await RunAgent(f"{task.id} merge fix", request, spec, ctx)
        task.costUsd += result.costUsd
        if not result.ok:
            task.notes.append(f"merge fix failed: {result.error}")
            return False
        output, response = SplitResponse(TakeFollowUps(task, result.text, ctx, replace=True))
        output, notes = SplitNotes(output)
        state["session"], state["rounds"] = result.sessionId or state["session"], state["rounds"] + 1
        task.responses.append(response)
        merged.replace(merged.with_name(f"merged.r{state['rounds'] - 1}.md"))
        WriteText(merged, output)
        if notes:
            ArchiveAndWrite(ctx.TaskDir(task.id) / "notes.md", notes, state["rounds"] - 1)
        return True

    await IntegrationLoop(task, ctx, maxRounds, ctx.projectDir, [ctx.runDir], Snapshot, Fix)
    return merged.read_text(encoding="utf-8")


async def KoreanTitle(task: Task, content: str, ctx: RunContext) -> str:
    """Short Korean title for the file name; falls back to the task's own title."""
    spec = AgentSpec(  # a one-shot chore: runs on the provider's small_model (orch.toml)
        role="namer",
        cwd=ctx.runDir,
        systemAppend=LoadPrompt("namer"),
        schema=TITLE_SCHEMA,
    )
    prompt = f"Task: {task.title}\n\nDocument (beginning):\n{content[:NAMER_INPUT_CHARS]}"
    result = await RunAgent(f"{task.id} title", prompt, spec, ctx)  # the Korean title in the doc's file name
    task.costUsd += result.costUsd
    data = result.structured if result.ok and isinstance(result.structured, dict) else {}
    return SanitizeTitle(str(data.get("title", ""))) or SanitizeTitle(task.title) or ctx.runId


# ---------- test and task (one part each: SINGLE_PART_KINDS) ----------

async def MergeTestTask(task: Task, ctx: RunContext, maxRounds: int) -> None:
    """A test leaves only its report: its copy of the project goes with the run's other workspaces."""
    part = task.parts[0]
    part.merged = True
    # Until the progress report exists (orchestrate.RunPipeline points here at it), a later task of this run that
    # builds on the test with "ref: Txx" reads the worker's own report
    task.resultRef = (ctx.TaskDir(part.id) / "notes.md").relative_to(ctx.projectDir).as_posix()
    task.result = "report only (the test's copy of the project is deleted when the run ends)"
    Blocked(task, "test unresolved")


async def MergeTaskList(task: Task, ctx: RunContext, maxRounds: int) -> None:
    """Read the tasks a [task] worker wrote; orchestrate.AddNewTasks adds them to the task list when the run ends."""
    part = task.parts[0]
    part.merged = True
    items, unread = ParseFollowUps((ctx.TaskDir(part.id) / "output.md").read_text(encoding="utf-8"), task.id)
    if unread:  # a line meant as a task that cannot be read is worth seeing in summary.md and the report
        more = f" (+{len(unread) - MAX_UNREAD_SHOWN} more)" if len(unread) > MAX_UNREAD_SHOWN else ""
        task.notes.append(f"line(s) not read as a task: {'; '.join(unread[:MAX_UNREAD_SHOWN])}{more}")
    if len(items) > MAX_WRITTEN_TASKS:
        task.notes.append(f"{len(items)} tasks written; only the first {MAX_WRITTEN_TASKS} are added")
        items = items[:MAX_WRITTEN_TASKS]
    if not items:
        task.error = "no task line in the result (expected: - [kind] <title>)"
        return
    if Blocked(task, "tasks not added"):
        return
    task.writtenTasks = items
    task.result = f"{len(items)} task(s) to add to the task list when the run ends"


# ---------- workspaces (code and sys) ----------

async def ReviewWorkspace(task: Task, ctx: RunContext, maxRounds: int, workdir: Path, diff) -> None:
    """Check and review the integrated workspace; a fresh worker fixes what the review finds."""
    await RunChecks(task, task, workdir, ctx)
    if maxRounds == 0:
        return

    async def Snapshot() -> str:  # with the integration fixer's follow-up proposals, so those are reviewed too
        return "\n\n".join(item for item in (f"Diff against base:\n{await diff()}", FollowUpText(task)) if item)

    async def Fix(review: dict) -> bool:
        spec = AgentSpec(role=f"{task.kind}-worker", cwd=workdir, systemAppend=WorkerSystemPrompt(task), addDirs=task.refDirs)
        checkpoint = await asyncio.to_thread(sandbox.Checkpoint, workdir) if task.kind == "sys" else None
        intro = f"You are in the integration workspace of task {task.id}, which combines all of its parts. A reviewer checked it."
        checks = f"Automated checks FAILED:\n{task.checkOutput}" if task.checkOk is False else ""
        result = await RunAgent(f"{task.id} integration fix", RevisionRequest(intro, review, checks, task.kind), spec, ctx)
        task.costUsd += result.costUsd
        if not result.ok:
            task.notes.append(f"integration fix failed: {result.error}")
            if checkpoint:
                await asyncio.to_thread(sandbox.Restore, workdir, checkpoint)
            return False
        task.responses.append(SplitResponse(TakeFollowUps(task, result.text, ctx, replace=True))[1])
        if task.kind == "code":
            await GitCall(ctx, gitops.CommitAll, workdir, f"orch({task.id}): address integration review")
        await RunChecks(task, task, workdir, ctx)
        return True

    await IntegrationLoop(task, ctx, maxRounds, workdir, [workdir, ctx.runDir], Snapshot, Fix)


# ---------- code ----------

async def MergeCodeTask(task: Task, ctx: RunContext, maxRounds: int) -> None:
    if len(task.parts) == 1:  # nothing to integrate: the part's branch is the task's result
        task.parts[0].merged = True
        await ApplyCode(task, task.parts[0].branch, ctx)
        return
    branch = f"orch/{ctx.runId}/{task.id}"
    worktree = ctx.worktreeDir / f"{task.id}-merge"
    try:
        await GitCall(ctx, gitops.CreateWorktree, ctx.repoRoot, worktree, branch, ctx.baseCommit)
    except gitops.GitError as error:
        task.error = f"integration branch setup failed: {error}"
        return
    try:
        for part in task.parts:
            await MergePart(part, worktree, ctx)
        unmerged = [part for part in task.parts if not part.merged]
        if unmerged:
            task.error = "; ".join(f"{part.id} not merged: {'; '.join(part.notes[-1:])}" for part in unmerged)
            return
        diff = lambda: GitCall(ctx, gitops.DiffAgainstBase, worktree, ctx.baseCommit)
        await ReviewWorkspace(task, ctx, maxRounds, worktree, diff)
    finally:
        await GitCall(ctx, gitops.RemoveWorktree, ctx.repoRoot, worktree)
    await ApplyCode(task, branch, ctx)


async def MergePart(part: Part, worktree: Path, ctx: RunContext) -> None:
    try:
        failure = await GitCall(ctx, gitops.StartMerge, worktree, part.branch)
        if failure is None:
            part.merged = True
            return
        conflicts = await GitCall(ctx, gitops.ConflictedFiles, worktree)
        if conflicts and await ResolveConflicts(part, worktree, conflicts, ctx):
            part.merged = True
            part.notes.append(f"merged after resolving conflicts in {', '.join(conflicts)}")
            return
        part.notes.append(f"unresolved conflict in {', '.join(conflicts)}" if conflicts else f"git merge failed: {failure}")
    except gitops.GitError as error:
        part.notes.append(f"merge failed: {error}")
    await GitCall(ctx, gitops.AbortMerge, worktree)


async def ResolveConflicts(part: Part, worktree: Path, conflicts: list[str], ctx: RunContext) -> bool:
    spec = AgentSpec(role="code-merger", cwd=worktree, systemAppend=LoadPrompt("merger"))
    prompt = (
        f"Merging branch {part.branch} (part {part.id}: {part.title}) into the integration branch of task "
        f"{part.task.id} ({part.task.title}) stopped on conflicts. The integration branch already holds the "
        "task's other parts.\nConflicted files:\n" + "\n".join(f"- {name}" for name in conflicts)
    )
    result = await RunAgent(f"{part.id} resolve conflicts", prompt, spec, ctx)
    part.task.costUsd += result.costUsd
    if not result.ok:
        return False
    if await GitCall(ctx, gitops.HasConflictMarkers, worktree, conflicts):
        ctx.log.warning("[%s] conflict markers remain after resolution attempt", part.id)
        return False
    await GitCall(ctx, gitops.FinishMerge, worktree)
    return True


async def ApplyCode(task: Task, branch: str, ctx: RunContext) -> None:
    """Bring the task's result into the project as staged, uncommitted changes - unless it is blocked."""
    task.resultRef = f"branch {branch}"
    if Blocked(task, f"branch {branch} not applied"):
        return
    try:
        problem = await GitCall(ctx, gitops.ApplyToWorkingTree, ctx.repoRoot, ctx.baseCommit, branch)
    except gitops.GitError as error:
        problem = str(error)
    if problem is None:
        task.result = "staged in the working tree (review with git diff --cached, then commit)"
        task.resultRef = f"staged in the project working tree (from branch {branch})"
        return
    ctx.log.warning("[%s] result not applied to the project: %s", task.id, problem)
    task.result = f"branch {branch} - NOT applied ({problem}); apply with: git merge --squash {branch}"


# ---------- sys ----------

async def MergeSysTask(task: Task, ctx: RunContext, maxRounds: int) -> None:
    """Combine the parts' sandboxes into one workspace; it is applied after every task of the run is done."""
    if len(task.parts) == 1:
        task.parts[0].merged = True
        workspace = task.parts[0].worktree
    else:
        workspace = ctx.worktreeDir / f"{task.id}-merge"
        problem = await asyncio.to_thread(CombineSandboxes, task, workspace)
        if problem:
            task.error = problem
            return
        diff = lambda: asyncio.to_thread(sandbox.Diff, task.baseDir, workspace)
        await ReviewWorkspace(task, ctx, maxRounds, workspace, diff)
    WriteText(ctx.TaskDir(task.id) / "sys.diff", await asyncio.to_thread(sandbox.Diff, task.baseDir, workspace))
    if Blocked(task, "not applied"):
        return
    task.workspace = workspace
    files = [name for _, name in await asyncio.to_thread(sandbox.Changes, task.baseDir, workspace)]
    task.resultRef = f"{workspace} (changes to {', '.join(files)}; applied to the project when run {ctx.runId} ends)"
    task.result = "waiting to apply when the run ends"


def CombineSandboxes(task: Task, workspace: Path) -> str | None:
    sandbox.CopyTree(task.baseDir, workspace)
    for part in task.parts:
        _, conflicts = sandbox.ApplyChanges(task.baseDir, workspace, part.worktree)
        if conflicts:  # the planner splits sys tasks along disjoint files, so this means overlapping parts
            return f"{part.id} overlaps earlier parts in {', '.join(conflicts)}; nothing applied"
        part.merged = True
    return None


def ApplySystemTasks(tasks: list[Task], ctx: RunContext, onApplied) -> None:
    """Apply finished sys tasks once every task is done: running agents read these files (prompts, skills)."""
    ready = [task for task in tasks if task.kind == "sys" and task.ok and task.workspace]
    for task in sorted(ready, key=SysDepth):  # upstream sys tasks first
        ApplySystemTask(task, ctx)
        onApplied(task)


def SysDepth(task: Task) -> int:
    return 1 + max((SysDepth(up) for up in task.upstream if up.kind == "sys" and up.workspace), default=0)


def ApplySystemTask(task: Task, ctx: RunContext) -> None:
    notApplied = [up.id for up in task.upstream if up.kind == "sys" and up.workspace and not up.ok]
    if notApplied:
        task.error = f"not applied: upstream {', '.join(notApplied)} was not applied"
        return
    taskDir = ctx.TaskDir(task.id)
    applied, conflicts = sandbox.ApplyChanges(task.baseDir, ctx.projectDir, task.workspace, taskDir / "backup")
    if conflicts:
        changed = [name for _, name in sandbox.Changes(task.baseDir, task.workspace)]
        sandbox.SaveChangedFiles(task.workspace, changed, taskDir / "changes")
        task.error = f"not applied: changed meanwhile in the project ({', '.join(conflicts)}); new versions kept in {taskDir / 'changes'}"
        return
    # The report's "Result location"; the report then becomes the result (orchestrate.RunPipeline), unless it fails
    task.resultRef = SYS_APPLIED_PREFIX + ", ".join(applied)
    task.result = f"applied {len(applied)} file(s): {', '.join(applied)}; originals backed up in {taskDir / 'backup'}"
    ctx.log.info("[%s] applied to the project: %s", task.id, ", ".join(applied))
