"""Agent calls: role presets, prompt loading, and the call that runs an agent on the provider orch.toml picks for its
role, retrying a transient failure. Provider-neutral: each backend (claude.py, gemini.py) is registered in
core/providers.py and imported on its first call."""
from __future__ import annotations

import asyncio
import importlib
import re
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any

from core.config import AgentChoice
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
    "test-worker": [*READ_TOOLS, "Edit", "Write", "Bash"],  # works in a disposable copy of the project
    "task-worker": READ_TOOLS,  # reads the project to plan; its final message is the task list
    "reviewer": READ_TOOLS,
    "doc-merger": READ_TOOLS,
    "code-merger": [*READ_TOOLS, "Edit"],
    "planner": READ_TOOLS,  # may look at the project to split along real files or sections
    "namer": [],  # answers from the prompt alone
    "reporter": [],  # summarizes the run's records, all of which are in the prompt
}
WRITE_ROLES = {"code-worker", "sys-worker", "test-worker", "code-merger"}
# Tool input keys worth echoing in progress lines, in priority order
PROGRESS_KEYS = ("file_path", "pattern", "command", "url", "query", "path")
MAX_CONSOLE_DETAIL = 80  # a progress line's tool detail on the console; run.log keeps all of it
CONSOLE_TEXT = "consoleText"  # log record attribute with a shorter console version of the message (orchestrate.py)
TRANSIENT_RETRIES = 1  # more attempts after a failure the backend's TRANSIENT_ERROR matches (connection dropped, overload)
RETRY_DELAY_SECONDS = 15  # time for a dropped connection or an overloaded API to recover
# A retry continues the failed session when one was saved: its research and edits are kept, only the reply is redone
RETRY_PROMPT = (
    "Your previous reply was cut off by a connection or API error before it was complete. Building on the work "
    "already done in this session, give your complete final reply again, exactly as the original instructions ask."
)


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
class TokenUsage:
    """What one agent call read and wrote, as far as the backend reports it (claude.py reports all of it).

    A session re-reads its whole context on every request, so peakContext - the largest single request - and
    cacheReadTokens are what make a long session expensive; run.log shows them next to the cost, with the call's
    wall-clock and API time and the models that served it.
    """

    turns: int = 0
    inputTokens: int = 0  # uncached input
    cacheReadTokens: int = 0
    cacheWriteTokens: int = 0
    outputTokens: int = 0
    peakContext: int = 0  # tokens in the largest request: the session's context at its longest; 0 = not reported
    durationMs: int = 0  # wall-clock time of the call; 0 = not reported
    apiDurationMs: int = 0  # time spent waiting on the API; 0 = not reported
    models: list[str] = field(default_factory=list)  # model IDs that served the call (subagents included)

    def Add(self, other: TokenUsage | None) -> TokenUsage:
        """Both calls together, as RunAgent counts a retry; the peak is the larger of the two."""
        if other is None:
            return self
        return TokenUsage(
            self.turns + other.turns, self.inputTokens + other.inputTokens, self.cacheReadTokens + other.cacheReadTokens,
            self.cacheWriteTokens + other.cacheWriteTokens, self.outputTokens + other.outputTokens,
            max(self.peakContext, other.peakContext), self.durationMs + other.durationMs,
            self.apiDurationMs + other.apiDurationMs, list(dict.fromkeys([*self.models, *other.models])),
        )

    def Summary(self) -> str:
        """"40 turns, peak context 103456, cache read 2012345, cache write 103456, input 1234, output 40000, time 6m12s,
        API 5m40s, model claude-opus-5-5" for run.log: exact token counts, for benchmarks."""
        parts = [f"{self.turns} turns"] if self.turns else []
        if self.peakContext:
            parts.append(f"peak context {TokenCount(self.peakContext)}")
        parts += [
            f"cache read {TokenCount(self.cacheReadTokens)}", f"cache write {TokenCount(self.cacheWriteTokens)}",
            f"input {TokenCount(self.inputTokens)}", f"output {TokenCount(self.outputTokens)}",
        ]
        if self.durationMs:
            parts.append(f"time {Duration(self.durationMs)}")
        if self.apiDurationMs:
            parts.append(f"API {Duration(self.apiDurationMs)}")
        if self.models:
            parts.append(f"model {'+'.join(self.models)}")
        return ", ".join(parts)


def TokenCount(count: int) -> str:
    return str(count)  # the raw integer: rounded to k/M, runs could not be compared token by token


def Duration(milliseconds: int) -> str:
    """"6m12s", or "42.3s" under a minute."""
    seconds = milliseconds / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m{rest:02d}s"


@dataclass
class AgentResult:
    text: str = ""
    structured: Any = None
    sessionId: str | None = None
    costUsd: float = 0.0
    error: str | None = None
    tokens: TokenUsage | None = None  # None = the backend reports no token use (gemini.py logs its own line)

    @property
    def ok(self) -> bool:
        return self.error is None


async def RunAgent(label: str, prompt: str, spec: AgentSpec, ctx: RunContext) -> AgentResult:
    """One agent call on its role's provider; a transient failure is retried, and the result's cost covers every
    attempt."""
    choice = ctx.config.AgentFor(spec.role)
    backend = Backend(choice.provider)
    result = await RunOnce(label, prompt, spec, ctx, choice, backend)
    for attempt in range(1, TRANSIENT_RETRIES + 1):
        if result.ok or not backend.TRANSIENT_ERROR.search(result.error):  # auth, budget, turn caps: same again
            break
        ctx.log.warning("[%s] transient failure - retry %d/%d in %ds", label, attempt, TRANSIENT_RETRIES, RETRY_DELAY_SECONDS)
        await asyncio.sleep(RETRY_DELAY_SECONDS)  # outside the semaphore: a waiting retry holds no agent slot
        retryPrompt, retrySpec = RetryCall(prompt, spec, result)
        spent, used = result.costUsd, result.tokens
        result = await RunOnce(f"{label} retry{attempt}", retryPrompt, retrySpec, ctx, choice, backend)
        result.costUsd += spent  # the failed attempt was paid for too
        result.tokens = result.tokens.Add(used) if result.tokens else used
    return result


async def RunOnce(label: str, prompt: str, spec: AgentSpec, ctx: RunContext, choice: AgentChoice,
                  backend: ModuleType) -> AgentResult:
    queuedAt = time.monotonic()
    async with ctx.semaphore:  # one global cap on concurrent agents across all phases
        waited = time.monotonic() - queuedAt  # time spent waiting for a free agent slot
        ctx.log.info("[%s] start (%s on %s; waited %.1fs for a slot)", label, spec.role, choice.provider, waited)
        result = await backend.Run(label, prompt, spec, ctx, choice.model)
        status = "ok" if result.ok else f"failed - {result.error}"
        usage = f"; {result.tokens.Summary()}" if result.tokens else ""  # where the cost came from
        ctx.log.info("[%s] %s ($%.3f%s)", label, status, result.costUsd, usage)
        return result


def RetryCall(prompt: str, spec: AgentSpec, failed: AgentResult) -> tuple[str, AgentSpec]:
    """The failed session continued when the backend saved it (for a fork, the copy - never the session it was forked
    from); otherwise the same call again."""
    if not failed.sessionId:
        return prompt, spec
    return RETRY_PROMPT, replace(spec, resume=failed.sessionId, fork=False)


def Backend(provider: str) -> ModuleType:
    """The provider's backend module, imported on first use: a provider no role uses needs neither SDK nor CLI."""
    return importlib.import_module(PROVIDER_SPECS[provider].module)


def LogToolUse(label: str, tool: str, params: dict, ctx: RunContext, extraKeys: tuple[str, ...] = ()) -> None:
    # Progress lines show long-running agents are alive (one line per tool call, from every backend).
    # run.log gets the whole detail (file paths, commands); only the console line is cut to fit one screen line
    detail = next((str(params[key]) for key in (*PROGRESS_KEYS, *extraKeys) if key in params), "").replace("\n", " ")
    short = f"[{label}]   {tool} {detail[:MAX_CONSOLE_DETAIL]}"
    ctx.log.info("[%s]   %s %s", label, tool, detail, extra={CONSOLE_TEXT: short})


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
