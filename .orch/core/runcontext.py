"""State shared by every phase of one orchestrator run. SDK-free."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
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
    baseCommit: str | None
    config: OrchConfig  # run options and each role's provider and model (orch.toml + command line)
    log: logging.Logger
    semaphore: asyncio.Semaphore
    gitLock: asyncio.Lock = field(default_factory=asyncio.Lock)  # tasks share one repository
    systemBase: Path | None = None  # snapshot of the system files that [sys] sandboxes start from

    def TaskDir(self, taskId: str) -> Path:
        path = self.runDir / taskId
        path.mkdir(parents=True, exist_ok=True)
        return path
