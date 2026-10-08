"""Per-task phases over its parts: workspaces, parallel work, revision and automated checks."""
from __future__ import annotations

import asyncio
from pathlib import Path

import gitops
import sandbox
from report import CheckText, FindingLines
from runner import AgentSpec, LoadPrompt, LoadSkill, RunAgent, RunContext, WriteText
from context import TaskContext
from tasks import Part, PartNeedsRevision, Task

MAX_EMBED_CHARS = 60_000  # cap inlined outputs; agents can still Read the full file
RESPONSE_MARKER = "=== REVIEW RESPONSE ==="  # splits a revision's output from the author's answer to the review
DELIVERY = {
    "doc": "Your final message must be the complete Markdown content for your part and nothing else.",
    "code": "Edit the files in this worktree, then end with a short summary: files changed, rationale, test results, assumptions.",
    "sys": "Edit the system files in this sandbox, then end with a short summary: files changed, rationale, check results, assumptions.",
}
RESPONSE_DELIVERY = (
    f"After that, add a line `{RESPONSE_MARKER}` followed by one line per CRITICAL/WARNING finding and failed check: "
    "how you fixed it, or why you reject it. Nothing about the review may appear above that line."
)


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
    if part.kind == "doc":
        part.workCwd = ctx.projectDir
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
    result = await RunAgent(f"{part.id} work", WorkerPrompt(part), spec, ctx)
    part.costUsd += result.costUsd
    if not result.ok:
        part.error = result.error
        await ParkPartialEdits(part, ctx, "failed work")
        return
    part.sessionId = result.sessionId
    if not await SaveOutput(part, result.text, ctx, f"orch({part.id}): {part.title[:60]}"):
        part.error = "worker finished without changing any files"
        return
    await RunChecks(part, part.task, part.worktree, ctx)


async def SaveOutput(part: Part, text: str, ctx: RunContext, commitMessage: str) -> bool:
    """Persist the latest output (archiving the previous round); True when files changed."""
    partDir = ctx.TaskDir(part.id)
    if part.kind == "doc":
        ArchiveAndWrite(partDir / "output.md", text, part.revisions)
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
    if task.kind == "doc" or workdir is None:
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


def WorkerPrompt(part: Part) -> str:
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
    lines += ["", DELIVERY[part.kind]]
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


def RevisionRequest(intro: str, review: dict, checks: str, kind: str) -> str:
    findings = FindingLines(review) or "- (none listed)"
    lines = [intro, f"Summary: {review.get('summary', '')}", "", "Findings:", findings]
    if checks:
        lines += ["", checks]
    lines += [
        "",
        "Fix every CRITICAL and WARNING finding you agree with, and make failed checks pass. "
        "If you reject a finding, say which one and why.",
        DELIVERY[kind] if kind != "doc" else "Reply with the complete revised Markdown content, not a diff or a list of changes.",
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
    spec = AgentSpec(
        role=f"{part.kind}-worker",
        cwd=part.workCwd,
        systemAppend=WorkerSystemPrompt(part.task),
        resume=part.sessionId,  # the original worker revises with its full context
        addDirs=part.task.refDirs,
    )
    checkpoint = await asyncio.to_thread(sandbox.Checkpoint, part.worktree) if part.kind == "sys" else None
    intro = f"A reviewer ({review['reviewer']}) reviewed your output for part {part.id}."
    checks = CheckText(part) if part.checkOk is False else ""
    result = await RunAgent(f"{part.id} revise r{review['round']}", RevisionRequest(intro, review, checks, part.kind), spec, ctx)
    part.costUsd += result.costUsd
    if not result.ok:  # keep the last good output; the part itself still counts as done
        part.notes.append(f"revision r{review['round']} failed: {result.error}")
        await ParkPartialEdits(part, ctx, "failed revision", checkpoint)
        return False
    part.sessionId = result.sessionId or part.sessionId
    output, response = SplitResponse(result.text)
    part.responses.append(response)
    WriteText(ctx.TaskDir(part.id) / f"response.r{review['round']}.md", response)
    await SaveOutput(part, output, ctx, f"orch({part.id}): address review round {review['round']}")
    await RunChecks(part, part.task, part.worktree, ctx)  # before counting the revision: archives keep round numbers
    part.revisions += 1
    return True
