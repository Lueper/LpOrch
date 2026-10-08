"""Review phases: ring review of a task's parts, verify-only re-review after every revision,
and review of the integrated result of a multi-part task.

Every revision is followed by a verification, so no result leaves the pipeline "revised, not re-reviewed".
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable

from phases import Clip, PartList
from report import CheckText, FindingLines, RenderReview
from runner import AgentResult, AgentSpec, LoadPrompt, LoadSkill, RunAgent, RunContext, WriteText
from context import TaskContext
from tasks import AssignReviewers, Part, Task, TaskNeedsRevision

# Optional project skills added to the code review rubric; missing files are skipped
CODE_RUBRIC = ["develop/reviewer.md", "common/conventions.md", "common/security.md", "common/performance.md"]
FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["CRITICAL", "WARNING", "INFO"]},
        "location": {"type": "string"},
        "issue": {"type": "string"},
        "suggestion": {"type": "string"},
    },
    "required": ["severity", "location", "issue", "suggestion"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "revise"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": FINDING_SCHEMA},
    },
    "required": ["verdict", "summary", "findings"],
}
REVIEW_FORMAT = {"type": "json_schema", "schema": REVIEW_SCHEMA}
VERIFY_RULES = (
    "For every CRITICAL and WARNING finding above, and every failed check, decide whether the current output "
    "resolves it, or the author rejected it with a convincing reason. Report as findings only (a) items still "
    "unresolved, keeping their severity, and (b) new CRITICAL problems the revision introduced. "
    "Do not raise new WARNING or INFO items. Verdict approve when nothing is left."
)


# ---------- shared ----------

def ReviewerSystemPrompt(task: Task) -> str:
    parts = [LoadPrompt("reviewer")]
    if task.kind in ("code", "sys"):
        parts += [LoadSkill(path) for path in CODE_RUBRIC]
    return "\n\n".join(part for part in parts if part)


def ReviewerSpec(task: Task, cwd: Path, addDirs: list[Path], resume: str | None = None) -> AgentSpec:
    return AgentSpec(
        role="reviewer",
        cwd=cwd,
        systemAppend=ReviewerSystemPrompt(task),
        resume=resume,
        fork=resume is not None,  # fork so the peer's own session stays clean for its revision
        addDirs=[*addDirs, *task.refDirs],  # refs outside the project stay readable for fidelity checks
        outputFormat=REVIEW_FORMAT,
    )


def ParseReview(result: AgentResult) -> dict:
    if not result.ok:
        return {"verdict": "error", "summary": result.error, "findings": []}
    review = result.structured
    if review is None:
        try:
            review = json.loads(result.text)
        except json.JSONDecodeError:
            review = None
    if not isinstance(review, dict) or review.get("verdict") not in ("approve", "revise"):
        return {"verdict": "error", "summary": "reviewer returned no valid structured result", "findings": []}
    return {"findings": [], **review}


def RecordReview(subject: Part | Task, ctx: RunContext, result: AgentResult, roundNo: int, reviewer: str, mode: str) -> dict:
    subject.costUsd += result.costUsd  # review cost is charged to the reviewed part or task
    review = {**ParseReview(result), "round": roundNo, "reviewer": reviewer, "mode": mode}
    subject.reviews.append(review)
    directory = ctx.TaskDir(subject.id)
    WriteText(directory / f"review.r{roundNo}.json", json.dumps(review, ensure_ascii=False, indent=2))
    WriteText(directory / f"review.r{roundNo}.md", RenderReview(subject, review))
    return review


def VerifyPrompt(subject: str, task: Task, previous: dict, response: str, checks: str, output: str) -> str:
    lines = [
        f"Verify the revision of {subject}.",
        *TaskContext(task),
        "",
        f"The previous review (round {previous['round']}) asked for these changes:",
        FindingLines(previous) or "- (no review findings; the revision addressed failed automated checks)",
        "",
        "The author's response:",
        response or "(none given)",
        "",
        VERIFY_RULES,
    ]
    if checks:
        lines += ["", checks]
    lines += ["", "=== CURRENT OUTPUT ===", output]
    return "\n".join(lines)


# ---------- parts ----------

def PartDirs(part: Part, ctx: RunContext) -> list[Path]:
    return [ctx.TaskDir(part.id), *([part.worktree] if part.worktree else [])]


async def ReviewTask(task: Task, ctx: RunContext, roundNo: int) -> None:
    parts = [part for part in task.parts if part.ok]
    peers = {part.id: part for part in parts}
    assignment = AssignReviewers(parts)  # ring: .1 reviews .2, .2 reviews .3, ...; one part gets a fresh reviewer
    await asyncio.gather(*(RunReview(part, peers.get(assignment[part.id]), ctx, roundNo) for part in parts))


async def RunReview(part: Part, peer: Part | None, ctx: RunContext, roundNo: int) -> None:
    cwd = peer.workCwd if peer else part.workCwd  # resume must run in the session's own project dir
    spec = ReviewerSpec(part.task, cwd, PartDirs(part, ctx), resume=peer.sessionId if peer else None)
    reviewer = peer.id if peer else "fresh-reviewer"
    result = await RunAgent(f"{part.id} review r{roundNo} by {reviewer}", ReviewPrompt(part, peer, ctx), spec, ctx)
    RecordReview(part, ctx, result, roundNo, reviewer, "review")


def ReviewPrompt(part: Part, peer: Part | None, ctx: RunContext) -> str:
    task = part.task
    lines = [f"Review part {part.id} ({part.title}).", *TaskContext(task, embedRefs=True), "This part's brief:", part.brief]
    if len(task.parts) > 1:
        lines += [
            "The task is split into these parts, which will be merged into one result:",
            *PartList(task),
            "Check that this part covers its brief, stays out of the other parts' scope, "
            "and fits the whole: no gaps, overlaps, contradictions, or terminology drift.",
        ]
    if peer:
        lines.append(f"Earlier in this session you wrote part {peer.id} ({peer.title}); compare this part against it.")
    if part.worktree:
        lines.append(f"The full workspace after the change is at: {part.worktree}")
    if CheckText(part):
        lines += ["", CheckText(part)]
    lines += ["", "=== OUTPUT UNDER REVIEW ===", OutputForReview(part, ctx)]
    return "\n".join(lines)


def OutputForReview(part: Part, ctx: RunContext) -> str:
    partDir = ctx.TaskDir(part.id)
    if part.kind == "doc":
        return Clip(partDir / "output.md")
    return f"Worker summary:\n{Clip(partDir / 'notes.md')}\n\nDiff against base:\n{Clip(partDir / 'output.diff')}"


async def VerifyParts(parts: list[Part], ctx: RunContext, roundNo: int) -> None:
    await asyncio.gather(*(RunVerify(part, ctx, roundNo) for part in parts))


async def RunVerify(part: Part, ctx: RunContext, roundNo: int) -> None:
    # A fresh session: the check is independent of whoever asked for the changes
    spec = ReviewerSpec(part.task, part.workCwd, PartDirs(part, ctx))
    response = part.responses[-1] if part.responses else ""
    subject = f"part {part.id} ({part.title})"
    prompt = VerifyPrompt(subject, part.task, part.reviews[-1], response, CheckText(part), OutputForReview(part, ctx))
    result = await RunAgent(f"{part.id} verify r{roundNo}", prompt, spec, ctx)
    RecordReview(part, ctx, result, roundNo, "fresh-verifier", "verify")


# ---------- integrated result ----------

async def IntegrationLoop(
    task: Task,
    ctx: RunContext,
    maxRounds: int,
    cwd: Path,
    addDirs: list[Path],
    snapshot: Callable[[], Awaitable[str]],
    fix: Callable[[dict], Awaitable[bool]],
) -> None:
    """Review the integrated result as a whole; while it needs changes and rounds remain: fix, then verify."""
    spec = ReviewerSpec(task, cwd, addDirs)
    result = await RunAgent(f"{task.id} integration review", IntegrationPrompt(task, ctx, await snapshot()), spec, ctx)
    RecordReview(task, ctx, result, 1, "integration-reviewer", "integration")
    for roundNo in range(2, maxRounds + 2):
        if not TaskNeedsRevision(task) or not await fix(task.reviews[-1]):
            return
        task.revisions += 1
        response = task.responses[-1] if task.responses else ""
        subject = f"the integrated result of task {task.id}"
        prompt = VerifyPrompt(subject, task, task.reviews[-1], response, CheckText(task), await snapshot())
        result = await RunAgent(f"{task.id} integration verify r{roundNo}", prompt, spec, ctx)
        RecordReview(task, ctx, result, roundNo, "fresh-verifier", "verify")


def IntegrationPrompt(task: Task, ctx: RunContext, output: str) -> str:
    lines = [
        f"Review the integrated result of task {task.id}, combined from {len(task.parts)} parts written in parallel.",
        *TaskContext(task),
        "",
        f"Parts (each part's own output is under {ctx.runDir}/<part id>/):",
        *PartList(task),
        "",
        "Each part was already reviewed on its own; judge the whole. Does it fulfil the task? Was any part's "
        "substance lost or distorted when combining (compare with the part outputs)? Are there contradictions, "
        "duplication, gaps, or drift in terminology, interfaces, structure or language between parts?",
    ]
    if CheckText(task):
        lines += ["", CheckText(task)]
    lines += ["", "=== INTEGRATED RESULT ===", output]
    return "\n".join(lines)
