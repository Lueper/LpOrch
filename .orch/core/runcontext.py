"""State shared by every phase of one orchestrator run. SDK-free."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import OrchConfig


@dataclass
class RunContext:
    """State shared by every phase of one orchestrator run."""

    runId: str
    runDir: Path
    projectDir: Path  # docs land here; doc workers run here
    repoRoot: Path | None  # git root for code tasks; None when the project is not a repo
    worktreeDir: Path
    baseCommit: str | None  # HEAD when the run started, plus its uncommitted changes to tracked files (a snapshot)
    config: OrchConfig  # run options and each role's provider and model (orch.toml + command line)
    log: logging.Logger
    semaphore: asyncio.Semaphore
    gitLock: asyncio.Lock = field(default_factory=asyncio.Lock)  # tasks share one repository
    systemBase: Path | None = None  # snapshot of the system files that [sys] sandboxes start from
    nextTaskNumber: int = 1  # the task list's next free ID when the run started; [task] workers number from it
    # Code results are applied one task at a time onto the run's integration branch (pipeline/merge.ApplyCode):
    # appliedCommit is what the working tree was last brought to, and later code tasks start from it
    appliedCommit: str | None = None
    applyWorktree: Path | None = None
    applyLock: asyncio.Lock = field(default_factory=asyncio.Lock)
    startedAt: datetime = field(default_factory=datetime.now)  # run start, for summary.md
    # Session id -> (cost USD, API milliseconds) the backend last reported for it. A resumed or forked session
    # reports its totals so far, earlier calls included: a call's own share is the difference (agents/claude.py)
    sessionTotals: dict[str, tuple[float, int]] = field(default_factory=dict)

    def TaskDir(self, taskId: str) -> Path:
        path = self.runDir / taskId
        path.mkdir(parents=True, exist_ok=True)
        return path
