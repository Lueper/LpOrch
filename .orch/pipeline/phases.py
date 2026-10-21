"""Per-task phases over its parts: workspaces, parallel work, revision and automated checks."""
from __future__ import annotations

import asyncio
from pathlib import Path

from agents.runner import AgentSpec, LoadPrompt, LoadSkill, RunAgent, WriteText
from core.context import TaskContext
from core.runcontext import RunContext
from core.taskfile import FOLLOWUP_MARKER, SplitFollowUps
from core.tasks import MAX_FOLLOWUPS, MAX_WRITTEN_TASKS, HasCritical, Part, PartNeedsRevision, Task, TaskId
from reporting.report import CheckText, FindingLines
from workspace import gitops, sandbox

MAX_EMBED_CHARS = 60_000  # cap inlined outputs; agents can still Read the full file
MAX_UNREAD_SHOWN = 3  # unreadable follow-up lines quoted in a note; the rest are counted
MAX_COPY_ERROR_CHARS = 500  # shutil.Error lists every file it could not copy
# A revision of a worker session past this many tokens of context starts a fresh session when the review found no
# CRITICAL issue: every request of a resumed session re-reads its whole context, so fixing a few WARNINGs on top
# of a long session costs more than a fresh worker reading the files it needs. CRITICAL fixes keep the context.
FRESH_FIX_CONTEXT_TOKENS = 60_000
RESPONSE_MARKER = "=== REVIEW RESPONSE ==="  # splits a revision's output from the author's answer to the review
NOTES_MARKER = "=== NOTES FOR THE REPORT ==="  # splits a document from notes that belong in the progress report
NOTES_DELIVERY = (
    f"Keep notes out of the document: assumptions you made (or, when merging, conflicts between parts and how you "
    f"resolved them) go after the document, below a line `{NOTES_MARKER}`, as bullets - all of them, each time. "
    "They go to the task's progress report. Omit that line when there is nothing to note."
)
DELIVERY = {
    "doc": f"Your final message must be the complete Markdown content for your part. {NOTES_DELIVERY}",
    "code": "Edit the files in this worktree, then end with a short summary: files changed, rationale, test results, assumptions.",
    "sys": "Edit the system files in this sandbox, then end with a short summary: files changed, rationale, check results, assumptions.",
    "test": (
        "Do the task in this disposable copy of the project: change files and run commands as it needs. Nothing you "
        "change here is kept, so your final message is the only record - end with the test report: what you ran or "
        "tried, the outcome of each step (quote the relevant output), your conclusion, the files you changed in the "
        "copy, and your assumptions."
    ),
}
# A revision answers with its whole output again; code and sys revise their files, so DELIVERY says enough for them
REVISED_OUTPUT = {
    "doc": f"Reply with the complete revised Markdown content, not a diff or a list of changes. {NOTES_DELIVERY}",
    "test": "Redo or extend the test where the review asks for it, then reply with the complete test report again "
            "- every step, not only the new ones.",
    "task": "Reply with the complete revised task list in the same form - the new tasks keep the IDs you were given, "
            f"in list order - not a list of changes. {NOTES_DELIVERY}",
}
RESPONSE_DELIVERY = (
    f"After that, add a line `{RESPONSE_MARKER}` followed by one line per CRITICAL/WARNING finding and failed check: "
    "how you fixed it, or why you reject it. Nothing about the review may appear above that line."
)
# Follow-up tasks: the agent names them, the orchestrator writes them into the task list when the run ends.
# Added to a prompt only while [tasks] follow_ups is on, so the setting really decides whether agents are asked
# (the role prompts in prompts/ are static and cannot be gated).
FOLLOWUP_DELIVERY = "\n".join([
    "Follow-up tasks: finish the work asked of you first. If it uncovers something that needs a task of its own - "
    "outside this task's scope, blocked on a decision someone else has to make, or a problem you must not fix here -"
    f" do not do it: name it below a line `{FOLLOWUP_MARKER}` at the very end of your message.",
    "- One per line in the task list's own syntax: `- [doc] <title>`, `- [code] <title>`, `- [sys] <title>`,"
    " `- [test] <title>` (`- [code:refactorer] <title>` picks a role skill). Indent detail lines under one to configure it:"
    " `ref: <path>`, `verify: <command>`, `lang: en`, `mode: full`.",
    f"- The orchestrator appends them to the task list as new pending tasks ({MAX_FOLLOWUPS} per task at most) and"
    " a later run picks them up, so write titles that stand on their own without your session. Keep them few and"
    " concrete; do not restate what you already did or what this task's own result covers.",
    "- Omit the line when nothing is needed. Never edit the task list yourself, and never rely on a follow-up being"
    " done: your own result must be complete without it.",
])


def TaskListDelivery(firstNumber: int) -> str:
    """What a [task] worker's final message must be: the tasks to add, numbered from the list's next free ID."""
    first, second = TaskId(firstNumber), TaskId(firstNumber + 1)
    return "\n".join([
        "Your final message is the list of tasks to add to the task list - nothing else above the notes line.",
        "- One task per line in the task list's own syntax: `- [<kind>] <title>` with kind doc, code, sys, test or"
        " task (`- [code:refactorer] <title>` picks a role skill). Indent detail lines under a task: its instructions,"
        " `after: Txx`, `ref: <path or Txx>`, `lang: en`, `verify: <command>`, `mode: full` (not for test or task).",
        f"- The new tasks get the IDs {first}, {second}, ... in the order you list them. A task that builds on an"
        f" earlier one of your list names that ID with `after:` (e.g. `after: {first}`). Refer only to existing tasks"
        " and to earlier tasks of your list.",
        f"- At most {MAX_WRITTEN_TASKS} tasks. Each title with its details must stand on its own: whoever runs it later"
        " sees nothing of this session. Do not repeat a task that is already in the task list.",
        "- The orchestrator adds them as pending tasks when the run ends, and a later run executes them.",
        f"Assumptions you made go after the list, below a line `{NOTES_MARKER}`, as bullets; they go to the task's"
        " progress report. Omit that line when there is nothing to note.",
    ])


async def GitCall(ctx: RunContext, function, *args):
    """Run a blocking git helper off the event loop, one at a time: all tasks share one repository."""
    async with ctx.gitLock:
        return await asyncio.to_thread(function, *args)


# ---------- workspaces ----------

async def RunWork(task: Task, ctx: RunContext) -> None:
    problem = await asyncio.to_thread(PrepareSystemBase, task, ctx) if task.kind == "sys" else None
    for part in task.parts:
        part.error = problem or part.error
    for part in task.parts:
        if part.ok:
            await PrepareWorkspace(part, ctx)
    await asyncio.gather(*(RunWorker(part, ctx) for part in task.parts if part.ok))


def PrepareSystemBase(task: Task, ctx: RunContext) -> str | None:
    """A sys task starts from the run's snapshot plus every in-run sys task it builds on."""
    sources = [up for up in task.upstream if up.kind == "sys" and up.workspace]
    if not sources:
        task.baseDir = ctx.systemBase
        return None
    task.baseDir = sandbox.CopyTree(ctx.systemBase, ctx.worktreeDir / f"{task.id}-base")
    for up in sources:  # snapshot -> upstream workspace holds all of the upstream chain's changes
        _, conflicts = sandbox.ApplyChanges(ctx.systemBase, task.baseDir, up.workspace)
        if conflicts:
            return f"cannot combine the changes of upstream {up.id}: {', '.join(conflicts)}"
    return None


async def PrepareWorkspace(part: Part, ctx: RunContext) -> None:
    if part.kind in ("doc", "task"):  # read-only workers: they look at the project as it is
        part.workCwd = ctx.projectDir
        return
    if part.kind == "test":  # deleted with the run's other workspaces when the run ends
        copy = ctx.worktreeDir / part.id
        try:
            part.copyStamps = await asyncio.to_thread(sandbox.CopyProject, ctx.projectDir, copy)
        except OSError as error:  # shutil.Error included: a locked or unreadable file
            part.error = f"cannot copy the project for the test: {str(error)[:MAX_COPY_ERROR_CHARS]}"
            return
        part.worktree = part.workCwd = copy
        return
    if part.kind == "sys":
        sandboxDir = await asyncio.to_thread(sandbox.CopyTree, part.task.baseDir, ctx.worktreeDir / part.id)
        part.worktree = part.workCwd = sandboxDir
        return
    if ctx.repoRoot is None:
        part.error = "code tasks need the project to be a git repository with at least one commit"
        return
    branch = f"orch/{ctx.runId}/{part.id}"
    worktree = ctx.worktreeDir / part.id
    try:
        await GitCall(ctx, gitops.CreateWorktree, ctx.repoRoot, worktree, branch, ctx.baseCommit)
    except gitops.GitError as error:
        part.error = str(error)
        return
    part.branch, part.worktree, part.workCwd = branch, worktree, worktree


# ---------- work ----------

async def RunWorker(part: Part, ctx: RunContext) -> None:
    spec = AgentSpec(
        role=f"{part.kind}-worker", cwd=part.workCwd, systemAppend=WorkerSystemPrompt(part.task), addDirs=part.task.refDirs
    )
    prompt = WorkerPrompt(part, ctx.config.followUps, ctx.nextTaskNumber)
    result = await RunAgent(f"{part.id} work", prompt, spec, ctx)
    part.costUsd += result.costUsd
    if not result.ok:
        part.error = result.error
        await ParkPartialEdits(part, ctx, "failed work")
        return
    part.sessionId = result.sessionId
    part.contextTokens = result.tokens.peakContext if result.tokens else 0
    text = TakeFollowUps(part, result.text, ctx)
    if not await SaveOutput(part, text, ctx, f"orch({part.id}): {part.title[:60]}"):
        part.error = "worker finished without changing any files"
        return
    await RunChecks(part, part.task, part.worktree, ctx)


async def SaveOutput(part: Part, text: str, ctx: RunContext, commitMessage: str) -> bool:
    """Persist the latest output (archiving the previous round); True when files changed."""
    partDir = ctx.TaskDir(part.id)
    if part.kind in ("doc", "task"):  # task: output.md is the task list it wrote
        document, notes = SplitNotes(text)
        ArchiveAndWrite(partDir / "output.md", document, part.revisions)
        if notes:  # the author's assumptions: for the progress report, never in the document
            ArchiveAndWrite(partDir / "notes.md", notes, part.revisions)
        return True
    if part.kind == "test":  # the report is the result; a test that changed nothing is still a test
        changes = await asyncio.to_thread(sandbox.ChangedFiles, part.worktree, part.copyStamps)
        ArchiveAndWrite(partDir / "notes.md", text, part.revisions)
        ArchiveAndWrite(partDir / "changes.md", ChangeList(changes), part.revisions)
        return True
    if part.kind == "sys":
        changed = bool(await asyncio.to_thread(sandbox.Changes, part.task.baseDir, part.worktree))
        diff = await asyncio.to_thread(sandbox.Diff, part.task.baseDir, part.worktree)
        stray = await asyncio.to_thread(sandbox.OutOfScope, part.worktree)
        if stray:
            part.notes.append(f"ignored files outside the system scope: {', '.join(stray[:5])}")
    else:
        changed = await GitCall(ctx, gitops.CommitAll, part.worktree, commitMessage)
        diff = await GitCall(ctx, gitops.DiffAgainstBase, part.worktree, ctx.baseCommit)
    ArchiveAndWrite(partDir / "output.diff", diff, part.revisions)
    ArchiveAndWrite(partDir / "notes.md", text, part.revisions)
    return changed


def ChangeList(changes: list[tuple[str, str]]) -> str:
    """A [test] copy's changed files, one "M path" line each (A added, M modified, D deleted)."""
    lines = [f"{status} {name}" for status, name in changes] or ["(no files changed)"]
    return "Files changed in the test's disposable copy of the project (discarded when the run ends):\n" + "\n".join(lines)


def ArchiveAndWrite(path: Path, text: str, roundNo: int) -> None:
    if path.exists():  # keep earlier rounds as output.r0.md, output.r1.md, ...
        path.replace(path.with_name(f"{path.stem}.r{roundNo}{path.suffix}"))
    WriteText(path, text)


async def ParkPartialEdits(part: Part, ctx: RunContext, reason: str, checkpoint: Path | None = None) -> None:
    if part.kind == "code" and part.worktree:
        await GitCall(ctx, gitops.StashChanges, part.worktree, f"orch: {reason} of {part.id}")
    if part.kind == "sys" and checkpoint:  # sandboxes are applied as they are: roll the failed attempt back
        await asyncio.to_thread(sandbox.Restore, part.worktree, checkpoint)


async def RunChecks(subject, task: Task, workdir: Path | None, ctx: RunContext) -> None:
    """Run the automated checks of a code/sys workspace and record them on the part or task."""
    if task.kind not in ("code", "sys") or workdir is None:  # a test runs its own commands and reports them
        return
    outcome = await asyncio.to_thread(sandbox.RunChecks, task.kind, workdir, task.verify)
    if outcome is None:
        return
    subject.checkOk, subject.checkOutput = outcome
    ArchiveAndWrite(ctx.TaskDir(subject.id) / "checks.md", subject.checkOutput, subject.revisions)
    ctx.log.info("[%s] automated checks %s", subject.id, "passed" if subject.checkOk else "FAILED")


def WorkerSystemPrompt(task: Task) -> str:
    parts = [LoadPrompt("worker")]
    if task.role:
        skill = LoadSkill(f"develop/{task.role}.md", task.title)
        parts.append(f"## Role Skill: {task.role}\n\n{skill}" if skill else "")
    return "\n\n".join(part for part in parts if part)


def PartList(task: Task, exclude: Part | None = None) -> list[str]:
    return [f"- {part.id} {part.title}: {part.brief}" for part in task.parts if part is not exclude]


def WorkerPrompt(part: Part, withFollowUps: bool = True, firstNumber: int = 1) -> str:
    task = part.task
    lines = TaskContext(task, embedRefs=True, withHistory=True)  # inline small refs and how upstream was reviewed
    if len(task.parts) == 1:
        lines += ["", f"Approach: {part.brief}"]
    else:
        lines += [
            "",
            f"This task is split among {len(task.parts)} agents working in parallel. You are {part.id}.",
            f"Your part: {part.title}",
            part.brief,
            "",
            "Other parts (other agents cover these - do not duplicate them):",
            *PartList(task, exclude=part),
        ]
    lines += ["", TaskListDelivery(firstNumber) if part.kind == "task" else DELIVERY[part.kind]]
    # [tasks] follow_ups = false: nobody writes them anywhere, so they are not asked for;
    # a [task] worker's whole result is new tasks already
    if withFollowUps and part.kind != "task":
        lines += ["", FOLLOWUP_DELIVERY]
    return "\n".join(lines)


def Clip(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if len(text) <= MAX_EMBED_CHARS:
        return text
    return f"{text[:MAX_EMBED_CHARS]}\n\n[truncated - read the full file at {path}]"


def SplitResponse(text: str) -> tuple[str, str]:
    """(output, the author's response to the review) from a revision's final message."""
    output, marker, response = text.rpartition(RESPONSE_MARKER)
    return (output.rstrip() + "\n", response.strip()) if marker else (text, "")


def SplitNotes(text: str) -> tuple[str, str]:
    """(document, notes for the progress report) from a doc agent's output."""
    document, marker, notes = text.partition(NOTES_MARKER)
    return (document.rstrip() + "\n", notes.strip()) if marker else (text, "")


def TakeFollowUps(subject: Part | Task, text: str, ctx: RunContext, replace: bool = False) -> str:
    """Record the follow-up tasks an agent asked for and return its message without them.

    Taken out before the notes and the review response are split off, so the block works wherever the agent put
    it, and so a proposed task never ends up inside a deliverable - with [tasks] follow_ups = false it is only
    stripped. A revision that lists follow-ups replaces the earlier list: the reviewer may have asked for one to
    be dropped, and a silent revision keeps what there was.
    """
    rest, items, unread = SplitFollowUps(text, subject.id)
    if not ctx.config.followUps:
        if items:
            ctx.log.info("[%s] %d proposed follow-up task(s) ignored ([tasks] follow_ups = false)", subject.id, len(items))
        return rest
    subject.followUps = items if items and replace else subject.followUps + items
    if unread:  # a list item meant as a task that nobody can read is worth seeing in summary.md
        more = f" (+{len(unread) - MAX_UNREAD_SHOWN} more)" if len(unread) > MAX_UNREAD_SHOWN else ""
        subject.notes.append(f"follow-up line(s) not read as a task: {'; '.join(unread[:MAX_UNREAD_SHOWN])}{more}")
    return rest


def FollowUpText(subject: Part | Task) -> str:
    """The follow-up tasks an agent asked for, as the reviewer sees them: they go into the task list verbatim."""
    if not subject.followUps:
        return ""
    lines = []
    for item in subject.followUps:
        role = f":{item.role}" if item.role else ""
        lines += [f"- [{item.kind}{role}] {item.title}", *(f"    {detail}" for detail in item.details)]
    return "\n".join([
        "=== FOLLOW-UP TASKS THE AUTHOR ASKED FOR (appended to the task list, not part of the result) ===",
        *lines,
    ])


def RevisionRequest(intro: str, review: dict, checks: str, kind: str) -> str:
    findings = FindingLines(review) or "- (none listed)"
    lines = [intro, f"Summary: {review.get('summary', '')}", "", "Findings:", findings]
    if checks:
        lines += ["", checks]
    lines += [
        "",
        "Fix every CRITICAL and WARNING finding you agree with, and make failed checks pass. "
        "If you reject a finding, say which one and why.",
        REVISED_OUTPUT.get(kind) or DELIVERY[kind],
        RESPONSE_DELIVERY,
    ]
    return "\n".join(lines)


# ---------- revision ----------

def PartsNeedingRevision(task: Task) -> list[Part]:
    return [part for part in task.parts if PartNeedsRevision(part)]


async def ReviseParts(parts: list[Part], ctx: RunContext) -> list[Part]:
    """Revise the parts in parallel; returns the ones whose revision succeeded."""
    revised = await asyncio.gather(*(RunRevision(part, ctx) for part in parts))
    return [part for part, ok in zip(parts, revised) if ok]


async def RunRevision(part: Part, ctx: RunContext) -> bool:
    review = part.reviews[-1]
    fresh = FreshFix(part, review)
    spec = AgentSpec(
        role=f"{part.kind}-worker",
        cwd=part.workCwd,
        systemAppend=WorkerSystemPrompt(part.task),
        resume=None if fresh else part.sessionId,  # resumed: the original worker revises with its full context
        addDirs=[*part.task.refDirs, ctx.TaskDir(part.id)] if fresh else part.task.refDirs,  # fresh: reads the diff
    )
    checkpoint = await asyncio.to_thread(sandbox.Checkpoint, part.worktree) if part.kind == "sys" else None
    checks = CheckText(part) if part.checkOk is False else ""
    if fresh:
        ctx.log.info("[%s] fix r%d in a fresh session (the worker session reached %d tokens of context)",
                     part.id, review["round"], part.contextTokens)
        intro = f"{FreshFixContext(part, ctx)}\n\nA reviewer ({review['reviewer']}) reviewed that output."
    else:
        intro = f"A reviewer ({review['reviewer']}) reviewed your output for part {part.id}."
    # "fix", not "revise": next to "review" in the log the two read alike
    result = await RunAgent(f"{part.id} fix r{review['round']}", RevisionRequest(intro, review, checks, part.kind), spec, ctx)
    part.costUsd += result.costUsd
    if not result.ok:  # keep the last good output; the part itself still counts as done
        part.notes.append(f"revision r{review['round']} failed: {result.error}")
        await ParkPartialEdits(part, ctx, "failed revision", checkpoint)
        return False
    part.sessionId = result.sessionId or part.sessionId
    part.contextTokens = result.tokens.peakContext if result.tokens else 0
    output, response = SplitResponse(TakeFollowUps(part, result.text, ctx, replace=True))
    part.responses.append(response)
    WriteText(ctx.TaskDir(part.id) / f"response.r{review['round']}.md", response)
    await SaveOutput(part, output, ctx, f"orch({part.id}): address review round {review['round']}")
    await RunChecks(part, part.task, part.worktree, ctx)  # before counting the revision: archives keep round numbers
    part.revisions += 1
    return True


def FreshFix(part: Part, review: dict) -> bool:
    """Whether a revision starts a fresh worker session instead of resuming the one that wrote the output:
    always without a saved session, and for a long session when the review found nothing CRITICAL."""
    return not part.sessionId or (part.contextTokens >= FRESH_FIX_CONTEXT_TOKENS and not HasCritical(review))


def FreshFixContext(part: Part, ctx: RunContext) -> str:
    """What a fresh worker needs to revise another session's output: the task, its part, and that output -
    in full for a document or task list (the reply replaces it), as a summary plus the diff's location for a
    workspace, whose files the worker reads as it needs."""
    partDir = ctx.TaskDir(part.id)
    lines = [*TaskContext(part.task, embedRefs=True), "", f"Your part: {part.title}", part.brief, ""]
    if part.kind in ("doc", "task"):
        lines += ["An earlier session wrote the current version below; you continue from it without that session.",
                  "=== CURRENT VERSION ===", Clip(partDir / "output.md")]
        if (partDir / "notes.md").is_file():
            lines += ["=== ITS NOTES FOR THE REPORT ===", Clip(partDir / "notes.md")]
    elif part.kind == "test":
        lines += ["An earlier session ran the test in this folder - its changes are still here - and reported:",
                  "=== TEST REPORT SO FAR ===", Clip(partDir / "notes.md")]
    else:
        lines += [f"An earlier session made the change; it is already in this workspace ({part.workCwd}). Its summary:",
                  Clip(partDir / "notes.md"), "",
                  f"Its diff against the base is {partDir / 'output.diff'}: read only the parts you need."]
    return "\n".join(lines)
