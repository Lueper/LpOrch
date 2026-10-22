"""Merge one task's parts into its own result, review the integrated result, and deliver it:
doc/[Txx]_<YYMMDD>_<title>.md, staged code changes, system files applied when the run ends, a test's report only,
or the tasks a [task] task wrote, added to the task list when the run ends.
Code results are applied through the run's integration branch, one task after another, so two tasks that change
the same file are merged rather than the second being refused (ApplyCode).
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
    items, todos, unread = ParseFollowUps((ctx.TaskDir(part.id) / "output.md").read_text(encoding="utf-8"), task.id)
    task.todoAsked += todos  # a step only a person can take is written under this task, not added as a task
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
        await GitCall(ctx, gitops.CreateWorktree, ctx.repoRoot, worktree, branch, task.gitBase)
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
        diff = lambda: GitCall(ctx, gitops.DiffAgainstBase, worktree, task.gitBase)
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
        situation = (
            f"Merging branch {part.branch} (part {part.id}: {part.title}) into the integration branch of task "
            f"{part.task.id} ({part.task.title}) stopped on conflicts. The integration branch already holds the "
            "task's other parts."
        )
        if conflicts and await ResolveConflicts(part.task, f"{part.id} resolve conflicts", situation, worktree, conflicts, ctx):
            part.merged = True
            part.notes.append(f"merged after resolving conflicts in {', '.join(conflicts)}")
            return
        part.notes.append(f"unresolved conflict in {', '.join(conflicts)}" if conflicts else f"git merge failed: {failure}")
    except gitops.GitError as error:
        part.notes.append(f"merge failed: {error}")
    await GitCall(ctx, gitops.AbortMerge, worktree)


async def ResolveConflicts(
    task: Task, label: str, situation: str, worktree: Path, conflicts: list[str], ctx: RunContext
) -> bool:
    """Let a code-merger agent resolve a merge that stopped on conflicts, and commit it; False when it could not."""
    spec = AgentSpec(role="code-merger", cwd=worktree, systemAppend=LoadPrompt("merger"))
    prompt = situation + "\nConflicted files:\n" + "\n".join(f"- {name}" for name in conflicts)
    result = await RunAgent(label, prompt, spec, ctx)
    task.costUsd += result.costUsd
    if not result.ok:
        return False
    if await GitCall(ctx, gitops.HasConflictMarkers, worktree, conflicts):
        ctx.log.warning("[%s] conflict markers remain after resolution attempt", label)
        return False
    await GitCall(ctx, gitops.FinishMerge, worktree)
    return True


async def ApplyCode(task: Task, branch: str, ctx: RunContext) -> None:
    """Bring the task's result into the project as staged, uncommitted changes - unless it is blocked.

    The result is merged into the run's integration branch first, which holds every code result this run applied
    before it: two tasks that change the same file are combined by a 3-way merge (a code-merger agent resolves
    conflicts, and the task's verify: must still pass), and the working tree then gets what the merge added. Only
    files a person changed since the run last wrote them keep the result out: then the patch is kept in the run
    folder and applying it becomes a to-do for a person ("todo:" under the task), which holds back the tasks that
    build on it.
    """
    task.resultRef = f"branch {branch}"
    if Blocked(task, f"branch {branch} not applied"):
        return
    async with ctx.applyLock:  # one result at a time: each merge builds on the one before
        try:
            problem = await IntegrateAndApply(task, branch, ctx)
        except gitops.GitError as error:
            problem = str(error)
    if problem in (None, gitops.NO_CHANGES):
        task.result = ("nothing new to apply: the working tree holds this change already" if problem
                       else "staged in the working tree (review with git diff --cached, then commit)")
        task.resultRef = f"staged in the project working tree (from branch {branch})"
        return
    ctx.log.warning("[%s] result not applied to the project: %s", task.id, problem)
    patch = ctx.TaskDir(task.id) / "apply.patch"
    try:
        WriteText(patch, await GitCall(ctx, gitops.DiffPatch, ctx.repoRoot, task.gitBase, branch))
        how = f"git apply --3way --index \"{patch.relative_to(ctx.projectDir).as_posix()}\""
    except (gitops.GitError, OSError, ValueError):  # the branch stays: a person can still merge it by hand
        how = f"git diff {task.gitBase} {branch} | git apply --3way --index"
    task.result = f"branch {branch} - NOT applied ({problem}); apply with: {how}"
    task.todoAsked.append(f"apply {task.id}'s code result, which was not applied ({problem}): {how}")


async def IntegrateAndApply(task: Task, branch: str, ctx: RunContext) -> str | None:
    """Merge the branch into the run's integration branch and stage what that adds in the working tree; returns why
    it was not applied (gitops.NO_CHANGES when there was nothing to add). Called under ctx.applyLock."""
    previous = ctx.appliedCommit or ctx.baseCommit
    worktree = ctx.applyWorktree
    if worktree is None:
        worktree = ctx.worktreeDir / "_applied"
        await GitCall(ctx, gitops.CreateWorktree, ctx.repoRoot, worktree, f"orch/{ctx.runId}/applied", previous)
        ctx.applyWorktree = worktree
    problem = await MergeIntoApplied(task, branch, worktree, ctx)
    if problem:
        return problem
    tip = await GitCall(ctx, gitops.RevParse, worktree)
    problem = await GitCall(ctx, gitops.ApplyToWorkingTree, ctx.repoRoot, previous, tip)
    if problem in (None, gitops.NO_CHANGES):
        ctx.appliedCommit = tip
    else:  # the working tree did not take it: neither may the tasks that start after this one
        await GitCall(ctx, gitops.ResetHard, worktree, previous)
    return problem


async def MergeIntoApplied(task: Task, branch: str, worktree: Path, ctx: RunContext) -> str | None:
    """Merge a task's branch into the integration branch; returns why it could not be merged."""
    previous = await GitCall(ctx, gitops.RevParse, worktree)
    failure = await GitCall(ctx, gitops.StartMerge, worktree, branch)
    if failure is None:
        return None
    conflicts = await GitCall(ctx, gitops.ConflictedFiles, worktree)
    situation = (
        f"Applying the result of task {task.id} ({task.title}): merging its branch {branch} into this run's"
        " integration branch, which already holds the code results of other tasks applied before it, stopped on"
        " conflicts. Keep the intent of both sides."
    )
    if not conflicts or not await ResolveConflicts(task, f"{task.id} apply merge", situation, worktree, conflicts, ctx):
        await GitCall(ctx, gitops.AbortMerge, worktree)
        listed = ", ".join(conflicts)
        return f"conflicts with code results applied earlier in this run: {listed}" if conflicts else f"git merge failed: {failure}"
    checks = await asyncio.to_thread(sandbox.RunChecks, "code", worktree, task.verify)
    if checks and not checks[0]:  # the resolved merge must pass what the task's own result passed
        WriteText(ctx.TaskDir(task.id) / "apply-checks.md", checks[1])
        await GitCall(ctx, gitops.ResetHard, worktree, previous)
        return f"verify failed after merging with code results applied earlier in this run ({', '.join(conflicts)})"
    task.notes.append(f"merged with code results applied earlier in this run after resolving conflicts in {', '.join(conflicts)}")
    return None


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
