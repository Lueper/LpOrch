"""claude_agent_sdk wrapper: shared run context, role presets and prompt loading."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, ToolUseBlock, query

PROJECT_ROOT = Path(__file__).resolve().parent.parent  # the folder that contains .orch/
PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
SKILL_DIR = PROJECT_ROOT / ".claude" / "skills"
FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)

READ_TOOLS = ["Read", "Glob", "Grep"]
# Each role sees only the built-in tools it needs; write access is limited to code roles
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
class RunContext:
    """State shared by every phase of one orchestrator run."""

    runId: str
    runDir: Path
    projectDir: Path  # docs land here; doc workers run here
    repoRoot: Path | None  # git root for code tasks; None when the project is not a repo
    worktreeDir: Path
    baseCommit: str | None
    budgetPerAgent: float
    maxTurns: int
    model: str | None
    log: logging.Logger
    semaphore: asyncio.Semaphore
    cliPath: str | None = None  # native claude executable; None = SDK default lookup
    gitLock: asyncio.Lock = field(default_factory=asyncio.Lock)  # tasks share one repository
    systemBase: Path | None = None  # snapshot of the system files that [sys] sandboxes start from

    def TaskDir(self, taskId: str) -> Path:
        path = self.runDir / taskId
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass
class AgentSpec:
    role: str
    cwd: Path
    systemAppend: str
    resume: str | None = None
    fork: bool = False
    addDirs: list[Path] = field(default_factory=list)
    outputFormat: dict | None = None
    model: str | None = None  # per-call override, e.g. a cheap model for small chores


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


def BuildOptions(spec: AgentSpec, ctx: RunContext, label: str) -> ClaudeAgentOptions:
    tools = ROLE_TOOLS[spec.role]
    return ClaudeAgentOptions(
        # CLI stderr goes to run.log only; benign notices (e.g. untrusted-workspace allow rules,
        # unused here because tools are pre-approved below) no longer look like failures on the console
        stderr=lambda line: ctx.log.debug("[%s] cli: %s", label, line.rstrip()),
        cwd=str(spec.cwd),
        tools=tools,  # expose only this role's built-ins
        allowed_tools=tools,  # headless: pre-approve them so nothing waits on a prompt
        permission_mode="acceptEdits" if spec.role in WRITE_ROLES else "default",
        system_prompt={"type": "preset", "preset": "claude_code", "append": spec.systemAppend},
        setting_sources=["project"],  # inherit the target project's CLAUDE.md and hooks
        strict_mcp_config=True,  # skip .mcp.json so parallel agents don't each spawn npx servers
        mcp_servers={},
        add_dirs=[str(path) for path in spec.addDirs],
        resume=spec.resume,
        fork_session=spec.fork,
        output_format=spec.outputFormat,
        max_turns=ctx.maxTurns,
        max_budget_usd=ctx.budgetPerAgent,
        model=spec.model or ctx.model,
        cli_path=ctx.cliPath,
    )


def ResolveCliPath(explicit: str | None) -> str | None:
    """Pick a native claude executable: the SDK refuses Windows .cmd/.bat shims (cmd.exe injection risk)."""
    if explicit:
        return explicit
    found = shutil.which("claude")
    if not found or Path(found).suffix.lower() not in (".cmd", ".bat"):
        return None
    # npm installs ship the native binary next to the shim
    native = Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
    return str(native) if native.is_file() else None


def ToAgentResult(message: ResultMessage) -> AgentResult:
    error = None
    if message.is_error:
        details = "; ".join(getattr(message, "errors", None) or []) or (message.result or "")
        error = f"{message.subtype}: {details}".strip(": ")
    return AgentResult(
        text=message.result or "",
        structured=message.structured_output,
        sessionId=message.session_id,
        costUsd=message.total_cost_usd or 0.0,
        error=error,
    )


async def RunAgent(label: str, prompt: str, spec: AgentSpec, ctx: RunContext) -> AgentResult:
    async with ctx.semaphore:  # one global cap on concurrent agents across all phases
        ctx.log.info("[%s] start (%s)", label, spec.role)
        result = AgentResult(error="agent ended without a result message")
        try:
            async for message in query(prompt=prompt, options=BuildOptions(spec, ctx, label)):
                if isinstance(message, AssistantMessage):
                    LogToolUse(label, message, ctx)
                elif isinstance(message, ResultMessage):
                    result = ToAgentResult(message)
        except Exception as error:  # system boundary: CLI process / transport / error-result failures
            # Keep cost and session from a result message received before the raise (e.g. budget cap)
            result = AgentResult(
                error=f"{type(error).__name__}: {error}", sessionId=result.sessionId, costUsd=result.costUsd
            )
        status = "ok" if result.ok else f"failed - {result.error}"
        ctx.log.info("[%s] %s ($%.3f)", label, status, result.costUsd)
        return result


def LogToolUse(label: str, message: AssistantMessage, ctx: RunContext) -> None:
    # Progress lines show long-running agents are alive (one line per tool call)
    for block in message.content:
        if not isinstance(block, ToolUseBlock):
            continue
        detail = next((str(block.input[key]) for key in PROGRESS_KEYS if key in block.input), "")
        ctx.log.info("[%s]   %s %s", label, block.name, detail.replace("\n", " ")[:80])


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
