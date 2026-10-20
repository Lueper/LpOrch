"""Claude backend: one agent call through claude_agent_sdk, which drives the Claude Code CLI.

- tools: runner.ROLE_TOOLS as they are (Claude Code's own names), pre-approved; write roles accept edits
- role prompt: appended to the claude_code system prompt preset
- project rules and guards: setting_sources=["project"] - CLAUDE.md, .claude/settings.json deny rules and hooks
- structured output: the SDK's json_schema output format
- limits: budget_per_agent and max_turns per call; [claude] max_retries goes to the CLI as CLAUDE_CODE_MAX_RETRIES
The SDK is imported on the first call, so preview, the sys smoke test and Gemini-only setups run without it.
"""
from __future__ import annotations

import functools
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from agents.runner import ROLE_TOOLS, WRITE_ROLES, AgentResult, AgentSpec, LogToolUse
from core.runcontext import RunContext

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage


async def Run(label: str, prompt: str, spec: AgentSpec, ctx: RunContext, model: str | None) -> AgentResult:
    result = AgentResult(error="agent ended without a result message")
    try:
        from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock, query  # lazy: see the docstring

        async for message in query(prompt=prompt, options=Options(spec, ctx, label, model)):
            if isinstance(message, AssistantMessage):
                for block in (block for block in message.content if isinstance(block, ToolUseBlock)):
                    LogToolUse(label, block.name, block.input, ctx)
            elif isinstance(message, ResultMessage):
                result = ToAgentResult(message)
    except Exception as error:  # system boundary: missing SDK, CLI process / transport / error-result failures
        # Keep cost and session from a result message received before the raise (e.g. budget cap)
        result = AgentResult(error=f"{type(error).__name__}: {error}", sessionId=result.sessionId, costUsd=result.costUsd)
    return result


def Options(spec: AgentSpec, ctx: RunContext, label: str, model: str | None) -> ClaudeAgentOptions:
    from claude_agent_sdk import ClaudeAgentOptions  # lazy: see the module docstring

    tools = ROLE_TOOLS[spec.role]
    settings = ctx.config.providers["claude"]
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
        output_format={"type": "json_schema", "schema": spec.schema} if spec.schema else None,
        max_turns=ctx.config.maxTurns,
        max_budget_usd=ctx.config.budgetPerAgent,
        model=model,  # orch.toml: [roles.<role>], small_model, model
        cli_path=CliPath(settings.cliPath),
        env=Env(settings.maxRetries),
    )


def Env(retries: int | None) -> dict[str, str]:
    # Added to the inherited environment: API retries only when orch.toml sets [claude] max_retries
    return {} if retries is None else {"CLAUDE_CODE_MAX_RETRIES": str(retries)}


@functools.lru_cache(maxsize=None)
def CliPath(explicit: str | None) -> str | None:
    """Pick a native claude executable: the SDK refuses Windows .cmd/.bat shims (cmd.exe injection risk).
    None = the SDK's own lookup."""
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
