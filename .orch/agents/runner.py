"""Agent calls: role presets, prompt loading, and the call that runs an agent on the provider orch.toml picks for its
role. Provider-neutral: each backend (claude.py, gemini.py) is registered in core/providers.py and imported on its
first call."""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.paths import PROMPT_DIR, SKILL_DIR
from core.providers import PROVIDER_SPECS
from core.runcontext import RunContext

FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)

READ_TOOLS = ["Read", "Glob", "Grep"]
# Each role sees only the built-in tools it needs; write access is limited to code roles (keys = config.ROLES).
# Claude Code's tool names are the common vocabulary: each backend maps them to its own tools
ROLE_TOOLS = {
    "doc-worker": [*READ_TOOLS, "WebSearch", "WebFetch"],
    "code-worker": [*READ_TOOLS, "Edit", "Write", "Bash"],
    "sys-worker": [*READ_TOOLS, "Edit", "Write", "Bash"],  # edits a sandbox copy of the orchestrator itself
    "reviewer": READ_TOOLS,
    "doc-merger": READ_TOOLS,
    "code-merger": [*READ_TOOLS, "Edit"],
    "planner": READ_TOOLS,  # may look at the project to split along real files or sections
    "namer": [],  # answers from the prompt alone
    "reporter": [],  # summarizes the run's records, all of which are in the prompt
}
WRITE_ROLES = {"code-worker", "sys-worker", "code-merger"}
# Tool input keys worth echoing in progress lines, in priority order
PROGRESS_KEYS = ("file_path", "pattern", "command", "url", "query", "path")


@dataclass
class AgentSpec:
    role: str
    cwd: Path
    systemAppend: str
    resume: str | None = None
    fork: bool = False
    addDirs: list[Path] = field(default_factory=list)
    schema: dict | None = None  # JSON schema of the reply; the backend puts the parsed object in AgentResult.structured


@dataclass
class AgentResult:
    text: str = ""
    structured: Any = None
    sessionId: str | None = None
    costUsd: float = 0.0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def RunAgent(label: str, prompt: str, spec: AgentSpec, ctx: RunContext) -> AgentResult:
    choice = ctx.config.AgentFor(spec.role)
    async with ctx.semaphore:  # one global cap on concurrent agents across all phases
        ctx.log.info("[%s] start (%s on %s)", label, spec.role, choice.provider)
        result = await Backend(choice.provider).Run(label, prompt, spec, ctx, choice.model)
        status = "ok" if result.ok else f"failed - {result.error}"
        ctx.log.info("[%s] %s ($%.3f)", label, status, result.costUsd)
        return result


def Backend(provider: str):
    """The provider's backend module, imported on first use: a provider no role uses needs neither SDK nor CLI."""
    return importlib.import_module(PROVIDER_SPECS[provider].module)


def LogToolUse(label: str, tool: str, params: dict, ctx: RunContext, extraKeys: tuple[str, ...] = ()) -> None:
    # Progress lines show long-running agents are alive (one line per tool call, from every backend)
    detail = next((str(params[key]) for key in (*PROGRESS_KEYS, *extraKeys) if key in params), "")
    ctx.log.info("[%s]   %s %s", label, tool, detail.replace("\n", " ")[:80])


def LoadPrompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def LoadSkill(relativePath: str, arguments: str = "") -> str:
    """Reuse a .claude/skills file as prompt text; empty string when it does not exist."""
    path = SKILL_DIR / relativePath
    if not path.is_file():
        return ""
    body = FRONTMATTER.sub("", path.read_text(encoding="utf-8"), count=1)
    return body.replace("$ARGUMENTS", arguments or "the assigned target")


def WriteText(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="")
