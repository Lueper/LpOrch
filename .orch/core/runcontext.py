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
    worktreeDir: Path
    config: OrchConfig  # run options and each role's provider and model (orch.toml + command line)
    log: logging.Logger
    semaphore: asyncio.Semaphore
    systemBase: Path | None = None  # snapshot of the project that [sys] sandboxes start from
    nextTaskNumber: int = 1  # the task list's next free ID when the run started; [task] workers number from it
    # One code task at a time works in a src/ folder (pipeline/phases.CodeFolder); tasks of other folders run in parallel
    codeLocks: dict[Path, asyncio.Lock] = field(default_factory=dict)
    folderLock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one code folder is named at a time (phases.CodeDir)
    startedAt: datetime = field(default_factory=datetime.now)  # run start, for summary.md
    # Session id -> (cost USD, API milliseconds) the backend last reported for it. A resumed or forked session
    # reports its totals so far, earlier calls included: a call's own share is the difference (agents/claude.py)
    sessionTotals: dict[str, tuple[float, int]] = field(default_factory=dict)

    def TaskDir(self, taskId: str) -> Path:
        path = self.runDir / taskId
        path.mkdir(parents=True, exist_ok=True)
        return path
