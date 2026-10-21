"""The Task/Part model: task settings, runtime state, and the review rules that decide revisions.

A Task is one line of TASKS.md (taskfile.py) - copied there from a row of a Notion database first when the task list
lives in Notion (notion.py, tasksource.py); the planner splits it into Parts, one per parallel agent.
Kept free of claude_agent_sdk imports so `preview` works without the SDK installed.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

STATUS_PENDING = " "
STATUS_RUNNING = "~"
STATUS_DONE = "x"
STATUS_FAILED = "!"
# doc: a document in doc/; code: staged changes; sys: the orchestrator's own files; test: a run in a disposable copy
# of the project that leaves only its report; task: new tasks written to the task list
TASK_KINDS = ("doc", "code", "sys", "test", "task")
SINGLE_PART_KINDS = ("test", "task")  # no "mode: full": a test or a task list is one agent's single result
# standard: one agent, one fresh review, revision, verify only after CRITICAL findings
# full: planner splits into 1..MAX_PARTS parallel parts, ring review, every revision verified, merge + integration review
MODE_STANDARD = "standard"
MODE_FULL = "full"
MAX_PARTS = 4  # parallel agents per "mode: full" task; the planner picks 1..MAX_PARTS
MAX_FOLLOWUPS = 5  # follow-up tasks one task may add to the task list; more means the agent is planning, not noting
MAX_WRITTEN_TASKS = 20  # tasks one [task] task may write: planning is its job
DEFAULT_DOC_LANG = "Korean"  # doc deliverables; "lang:" overrides per task
LANG_NAMES = {"ko": "Korean", "kr": "Korean", "en": "English", "ja": "Japanese"}
SYS_APPLIED_PREFIX = "applied to the project: "  # a sys task's changed files: its result until its report replaces it


class TaskFileError(Exception):
    """The task list cannot be read, or a task's line or database row can no longer be located."""


def TaskId(number: int) -> str:
    """T01, T02, ... - task IDs as the task list numbers them."""
    return f"T{number:02d}"


def TitleKey(kind: str, title: str) -> tuple[str, str]:
    """A task's identity by kind and title, spacing and case ignored: how a follow-up is recognized as a duplicate."""
    return kind, re.sub(r"\s+", " ", title).strip().lower()


@dataclass
class FollowUp:
    """A follow-up task an agent asked for while working on a task.

    Written as a task line in the agent's final message (pipeline/phases.py, parsed by core/taskfile.py) and
    appended to the task list as a new pending task once the run's results are in (orchestrate.AddFollowUpTasks).
    """

    kind: str  # one of TASK_KINDS, like a task's
    role: str | None  # optional role skill, as in "[code:refactorer]"
    title: str
    details: list[str] = field(default_factory=list)  # detail lines as written: "ref: ...", "verify: ...", ...
    origin: str = ""  # the part or task whose agent proposed it

    @property
    def key(self) -> tuple[str, str]:
        return TitleKey(self.kind, self.title)


@dataclass(eq=False)  # identity equality: Task and Part reference each other
class Task:
    id: str
    kind: str  # one of TASK_KINDS
    role: str | None  # optional .claude/skills/develop/<role>.md
    title: str
    lineNo: int  # line in TASKS.md (for a Notion row: its line in the TASKS.md cache)
    status: str = STATUS_PENDING
    sourceId: str = ""  # Notion page id of the row this task came from; empty for a TASKS.md line
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
    followUps: list[FollowUp] = field(default_factory=list)  # follow-up tasks the merger or integration fixer asked for
    writtenTasks: list[FollowUp] = field(default_factory=list)  # task: its result, added to the task list at the end
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
    worktree: Path | None = None  # code: git worktree; sys: sandbox copy; test: disposable copy of the project
    copyStamps: dict[str, tuple[int, int]] = field(default_factory=dict)  # test: the copy's files as made (size, mtime)
    branch: str | None = None
    sessionId: str | None = None
    contextTokens: int = 0  # peak context of that worker session; 0 = not reported (the fix then resumes it)
    costUsd: float = 0.0
    error: str | None = None
    reviews: list[dict] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)  # worker's answers to each review it addressed
    followUps: list[FollowUp] = field(default_factory=list)  # follow-up tasks this part's worker asked for
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


def FollowUpsOf(task: Task) -> list[FollowUp]:
    """Every follow-up task asked for while working on the task, in the order proposed, duplicates dropped.

    A part proposes the same follow-up again in a revision (the worker keeps its session), and parts of one task
    often see the same gap, so only the first of each kind-and-title survives.
    """
    unique: dict[tuple[str, str], FollowUp] = {}
    for item in [*(entry for part in task.parts for entry in part.followUps), *task.followUps]:
        unique.setdefault(item.key, item)
    return list(unique.values())


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
