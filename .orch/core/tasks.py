"""The Task/Part model: task settings, runtime state, and the review rules that decide revisions.

A Task is one line of TASKS.md (parsed by taskfile.py); the planner splits it into Parts, one per parallel agent.
Kept free of claude_agent_sdk imports so `preview` works without the SDK installed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

STATUS_PENDING = " "
STATUS_RUNNING = "~"
STATUS_DONE = "x"
STATUS_FAILED = "!"
# standard: one agent, one fresh review, revision, verify only after CRITICAL findings
# full: planner splits into 1..MAX_PARTS parallel parts, ring review, every revision verified, merge + integration review
MODE_STANDARD = "standard"
MODE_FULL = "full"
MAX_PARTS = 4  # parallel agents per "mode: full" task; the planner picks 1..MAX_PARTS
DEFAULT_DOC_LANG = "Korean"  # doc deliverables; "lang:" overrides per task
LANG_NAMES = {"ko": "Korean", "kr": "Korean", "en": "English", "ja": "Japanese"}
SYS_APPLIED_PREFIX = "applied to the project: "  # a sys task's result: the files it changed


class TaskFileError(Exception):
    """TASKS.md is missing or a task line can no longer be located."""


@dataclass(eq=False)  # identity equality: Task and Part reference each other
class Task:
    id: str
    kind: str  # "doc" | "code" | "sys"
    role: str | None  # optional .claude/skills/develop/<role>.md
    title: str
    lineNo: int
    status: str = STATUS_PENDING
    details: list[str] = field(default_factory=list)
    after: list[str] = field(default_factory=list)  # "after:" IDs of tasks this one builds on
    refs: list[str] = field(default_factory=list)  # "ref:" base materials as written: files, folders, globs, URLs
    refFiles: list[Path] = field(default_factory=list)  # resolved by context.ResolveRefs
    refUrls: list[str] = field(default_factory=list)
    refDirs: list[Path] = field(default_factory=list)  # folders of ref files outside the project, opened to agents
    refTaskIds: list[str] = field(default_factory=list)  # "ref: T03": that task's result file is a base material
    lang: str | None = None  # "lang:" output language of a doc deliverable
    verify: str | None = None  # "verify:" shell command that must pass (code/sys)
    mode: str = MODE_STANDARD  # "mode: full" opts into the multi-agent pipeline
    resultRef: str = ""  # where the result is; "result:" carries it across runs
    resultRun: str = ""  # run that produced resultRef; its reviews and responses feed later tasks
    # Runtime state filled in by the pipeline
    upstream: list[Task] = field(default_factory=list)
    history: str = ""  # review findings and author responses from the upstream tasks' runs
    parts: list[Part] = field(default_factory=list)
    planNote: str = ""
    reviews: list[dict] = field(default_factory=list)  # reviews of the integrated multi-part result
    responses: list[str] = field(default_factory=list)  # fixer's answers to those reviews
    revisions: int = 0
    checkOk: bool | None = None  # automated checks on the integrated result; None = not run
    checkOutput: str = ""
    baseDir: Path | None = None  # sys: the snapshot this task's sandboxes start from
    workspace: Path | None = None  # sys: integrated change, applied when the run ends
    costUsd: float = 0.0  # task-level agents: planner, merger, namer, integration review/fix
    error: str | None = None
    blocked: bool = False  # an upstream task did not succeed; the task goes back to pending
    result: str = ""  # where the merged result went, for the summary
    reportRef: str = ""  # Korean progress report, written when the run ends (reporting/progress.py), for the summary
    notes: list[str] = field(default_factory=list)
    finished: bool = False
    doneEvent: asyncio.Event = field(default_factory=asyncio.Event, repr=False)  # downstream tasks wait on it

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def full(self) -> bool:
        return self.mode == MODE_FULL

    @property
    def detailText(self) -> str:
        return "\n".join(self.details)

    @property
    def outputLang(self) -> str | None:
        if self.lang:
            return LANG_NAMES.get(self.lang.lower(), self.lang)
        return DEFAULT_DOC_LANG if self.kind == "doc" else None

    @property
    def totalCostUsd(self) -> float:
        return self.costUsd + sum(part.costUsd for part in self.parts)


@dataclass(eq=False)
class Part:
    """One agent's share of a task, as assigned by the planner."""

    id: str  # "T02.1"
    task: Task = field(repr=False)
    title: str
    brief: str
    # Runtime state filled in by the pipeline
    workCwd: Path | None = None
    worktree: Path | None = None  # code: git worktree; sys: sandbox copy
    branch: str | None = None
    sessionId: str | None = None
    costUsd: float = 0.0
    error: str | None = None
    reviews: list[dict] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)  # worker's answers to each review it addressed
    revisions: int = 0
    checkOk: bool | None = None  # automated checks (code/sys); None = not run
    checkOutput: str = ""
    merged: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return self.task.kind

    @property
    def ok(self) -> bool:
        return self.error is None


def HasCritical(review: dict) -> bool:
    return any(f.get("severity") == "CRITICAL" for f in review.get("findings", []))


def NeedsRevision(review: dict) -> bool:
    return review.get("verdict") == "revise" or HasCritical(review)


def PartNeedsRevision(part: Part) -> bool:
    # A lone part's checks gate it; split parts may depend on each other, so they are checked after merging
    checkFailed = part.checkOk is False and len(part.task.parts) == 1
    last = part.reviews[-1] if part.reviews else None
    # A review counts as addressed once a revision followed it (standard mode skips verifying WARNING-only fixes)
    reviewAsks = last is not None and NeedsRevision(last) and part.revisions < last["round"]
    return part.ok and (checkFailed or reviewAsks)


def TaskNeedsRevision(task: Task) -> bool:
    return task.checkOk is False or (bool(task.reviews) and NeedsRevision(task.reviews[-1]))


def AssignReviewers(tasks: list[Part]) -> dict[str, str | None]:
    """Ring-pair the parts of one task with their next peer; None means a fresh reviewer."""
    assignment: dict[str, str | None] = {}
    for index, task in enumerate(tasks):
        rotated = tasks[index + 1 :] + tasks[:index]
        sameKind = [peer for peer in rotated if peer.kind == task.kind]
        candidates = sameKind or rotated
        assignment[task.id] = candidates[0].id if candidates else None
    return assignment
