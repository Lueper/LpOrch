"""Gemini CLI backend: one agent call as a headless `gemini` process, mapped onto what the pipeline uses from
claude_agent_sdk.

- tools: runner.ROLE_TOOLS translated to Gemini built-ins; a per-role policy denies everything else (MCP included)
- role prompt: the CLI cannot append to its system prompt, so it opens the first message of every new session
- project rules, turn cap and guards: user settings in a per-run GEMINI_CLI_HOME (the CLI skips system settings
  files in folders an administrator does not own) - the user's own settings, plus CLAUDE.md as a context file,
  max_turns, and gemini_guard.py before write and shell calls (the project's Claude Code hooks and deny rules).
  Sessions live in that home too, apart from the user's own; the API key comes from the OS keychain or env vars.
- structured output: asked for in the prompt, parsed from the reply, asked again once when it is not valid JSON
- resume: --resume <session id> from the session's own folder; fork: a copy of the saved session (--session-file)
The CLI reports tokens, not money: calls count as $0 and their token use goes to run.log. budget_per_agent does not
apply to Gemini calls; max_turns does.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path

from runner import PROGRESS_KEYS, ROLE_TOOLS, AgentResult, AgentSpec, RunContext, WriteText

GUARD_SCRIPT = Path(__file__).resolve().parent / "gemini_guard.py"
# Claude Code tool -> its Gemini CLI built-ins
TOOL_MAP = {
    "Read": ["read_file", "read_many_files"],
    "Glob": ["glob", "list_directory"],
    "Grep": ["grep_search"],
    "Edit": ["replace"],
    "Write": ["write_file"],
    "Bash": ["run_shell_command"],
    "WebSearch": ["google_web_search"],
    "WebFetch": ["web_fetch"],
}
GUARDED_TOOLS = "write_file|replace|run_shell_command"  # the calls gemini_guard.py checks
GUARD_TIMEOUT_MS = 60_000
CONTEXT_FILES = ["CLAUDE.md", "GEMINI.md"]  # project rules, as setting_sources=["project"] gives Claude agents
NO_MCP_SERVER = "__orch_none__"  # --allowed-mcp-server-names that matches no server: none is started
GEMINI_DIR = ".gemini"  # the CLI's folder under its home: settings.json, sessions (tmp/<project>/chats)
STREAM_LIMIT = 64 * 1024**2  # one stream-json line can carry a whole tool result
MAX_STDERR_CHARS = 2000  # tail of the CLI's stderr kept in a failed call's error
EXTRA_PROGRESS_KEYS = ("dir_path", "prompt")  # Gemini parameter names worth echoing besides runner's
# Failed API attempts and quota details on stderr: shown on the console, or a rate-limited agent looks stalled
API_FAILURE = re.compile(r"^(?:Attempt \d+ failed\b|\* Quota exceeded for metric:)")
MAX_NOTICE_CHARS = 200
ROLE_INTRO = "Instructions for your role from the orchestrator that runs you (treat them as your system instructions):"
TASK_INTRO = "=== YOUR TASK ==="
JSON_REQUEST = (
    "Reply with one JSON object and nothing else - no prose before or after it, no Markdown code fence - "
    "matching this JSON schema:"
)
JSON_RETRY = "Your last reply was not a valid JSON object for the schema. Reply again with only that JSON object."
FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


class GeminiError(Exception):
    """The CLI or a saved session cannot be found."""


async def RunGemini(label: str, prompt: str, spec: AgentSpec, ctx: RunContext, model: str | None) -> AgentResult:
    """One agent call; with outputFormat, the reply's JSON lands in result.structured (asked once more if invalid)."""
    try:
        command = GeminiCommand(ctx.config.providers["gemini"].cliPath)
        sessionFile = ForkSource(spec.resume, ctx) if spec.fork and spec.resume else None
        result = await RunProcess(label, command, ComposePrompt(prompt, spec), spec, ctx, model, sessionFile)
        if spec.outputFormat and result.ok:
            await ParseStructured(label, command, result, spec, ctx, model)
    except Exception as error:  # system boundary: CLI process / stream / session-file failures
        return AgentResult(error=f"{type(error).__name__}: {error}")
    return result


# ---------- prompt and structured output ----------

def ComposePrompt(prompt: str, spec: AgentSpec) -> str:
    """The role prompt opens every new or forked session; a resumed session of the same role already has it."""
    parts = [ROLE_INTRO, spec.systemAppend, TASK_INTRO] if spec.systemAppend and (not spec.resume or spec.fork) else []
    parts.append(prompt)
    if spec.outputFormat:
        parts += [JSON_REQUEST, json.dumps(spec.outputFormat["schema"], ensure_ascii=False)]
    return "\n\n".join(parts)


async def ParseStructured(label: str, command: tuple[str, ...], result: AgentResult, spec: AgentSpec, ctx: RunContext,
                          model: str | None) -> None:
    schema = spec.outputFormat["schema"]
    result.structured = ExtractJson(result.text, schema)
    if result.structured is not None or not result.sessionId:
        return
    # One short turn in the same session usually fixes prose around the JSON or a missing key
    retrySpec = AgentSpec(role=spec.role, cwd=spec.cwd, systemAppend="", resume=result.sessionId, addDirs=spec.addDirs)
    retry = await RunProcess(f"{label} json retry", command, JSON_RETRY, retrySpec, ctx, model, None)
    if retry.ok:
        result.text, result.structured = retry.text, ExtractJson(retry.text, schema)


def ExtractJson(text: str, schema: dict) -> dict | None:
    """The reply's JSON object when it has the schema's required keys: the whole reply, a fenced block, or the span
    from the first "{" to the last "}"."""
    stripped = text.strip()
    fenced = FENCED_JSON.search(stripped)
    start, end = stripped.find("{"), stripped.rfind("}")
    for candidate in (stripped, fenced[1] if fenced else "", stripped[start:end + 1] if 0 <= start < end else ""):
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict) and all(key in data for key in schema.get("required", [])):
            return data
    return None


# ---------- process ----------

async def RunProcess(label: str, command: tuple[str, ...], prompt: str, spec: AgentSpec, ctx: RunContext,
                     model: str | None, sessionFile: Path | None) -> AgentResult:
    process = await asyncio.create_subprocess_exec(
        *command,
        *Arguments(spec, ctx, model, sessionFile),
        cwd=str(spec.cwd),
        env={**os.environ, "GEMINI_CLI_HOME": str(GeminiHome(ctx)), "NO_COLOR": "1"},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=STREAM_LIMIT,
    )
    stream = Stream(label, ctx)
    try:
        _, _, stderr = await asyncio.gather(
            Feed(process, prompt), ReadEvents(process, stream), ReadStderr(label, process, ctx)
        )
        code = await process.wait()
    finally:
        if process.returncode is None:  # cancelled or failed mid-call: never leave the CLI running
            process.kill()
    stream.LogTokens()
    return stream.Result(code, stderr)


def Arguments(spec: AgentSpec, ctx: RunContext, model: str | None, sessionFile: Path | None) -> list[str]:
    arguments = [
        "--prompt", "",  # headless; the prompt itself comes on stdin, free of command-line length limits
        "--output-format", "stream-json",
        "--skip-trust",  # worktrees and sandboxes are new, untrusted folders: the CLI would refuse to run there
        "--approval-mode", "yolo",  # nobody can approve; the role's policy decides every call instead
        "--policy", str(PolicyFile(spec.role, ctx)),
        "--extensions", "none",
        "--allowed-mcp-server-names", NO_MCP_SERVER,  # parallel agents must not each start MCP servers
    ]
    if model:
        arguments += ["--model", model]
    for path in spec.addDirs:
        arguments += ["--include-directories", str(path)]
    if sessionFile:
        arguments += ["--session-file", str(sessionFile)]
    elif spec.resume:
        arguments += ["--resume", spec.resume]
    return arguments


async def Feed(process: asyncio.subprocess.Process, prompt: str) -> None:
    try:
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()
    except (BrokenPipeError, ConnectionResetError):  # the CLI exited early; its exit code tells why
        pass


async def ReadEvents(process: asyncio.subprocess.Process, stream: Stream) -> None:
    async for raw in process.stdout:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            stream.ctx.log.debug("[%s] cli: %s", stream.label, line)
            continue
        if isinstance(event, dict):
            stream.Handle(event)


async def ReadStderr(label: str, process: asyncio.subprocess.Process, ctx: RunContext) -> str:
    # CLI notices go to run.log only, as with the Claude CLI's stderr; failed API attempts also reach the console
    lines = []
    async for raw in process.stderr:
        line = raw.decode("utf-8", errors="replace").rstrip()
        ctx.log.debug("[%s] cli: %s", label, line)
        if API_FAILURE.match(line):
            ctx.log.warning("[%s]   gemini: %s", label, line[:MAX_NOTICE_CHARS])
        lines.append(line)
    return "\n".join(lines)[-MAX_STDERR_CHARS:]


class Stream:
    """What one call's stream-json events add up to: session, final message, outcome and token use."""

    def __init__(self, label: str, ctx: RunContext):
        self.label, self.ctx = label, ctx
        self.sessionId: str | None = None
        self.text = ""
        self.afterTool = False  # assistant text after a tool call starts a new message: the last one is the reply
        self.outcome: dict = {}
        self.lastError = ""  # the CLI's last error event: the reason when a failed call reports none

    def Handle(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "init":
            self.sessionId = event.get("session_id") or self.sessionId
        elif kind == "message" and event.get("role") == "assistant":
            if self.afterTool:
                self.text, self.afterTool = "", False
            content = str(event.get("content") or "")
            self.text = self.text + content if event.get("delta") else content
        elif kind == "tool_use":
            self.afterTool = True
            self.LogToolUse(event)
        elif kind == "tool_result":
            self.afterTool = True
            if event.get("status") == "error":
                self.ctx.log.debug("[%s] tool error: %s", self.label, event.get("error"))
        elif kind == "error":
            self.lastError = str(event.get("message") or "")
            self.ctx.log.warning("[%s]   gemini: %s", self.label, self.lastError)
        elif kind == "result":
            self.outcome = event

    def LogToolUse(self, event: dict) -> None:
        # Same progress line as runner.LogToolUse: shows a long-running agent is alive
        params = event.get("parameters") or {}
        keys = (*PROGRESS_KEYS, *EXTRA_PROGRESS_KEYS)
        detail = next((str(params[key]) for key in keys if key in params), "")
        self.ctx.log.info("[%s]   %s %s", self.label, event.get("tool_name", "?"), detail.replace("\n", " ")[:80])

    def LogTokens(self) -> None:
        stats = self.outcome.get("stats") or {}
        if stats:  # the CLI reports no cost: token use is the record of what a call consumed
            models = ", ".join(stats.get("models") or {}) or "model not reported"
            self.ctx.log.info(
                "[%s]   tokens: input %s (cached %s), output %s - %s", self.label,
                stats.get("input_tokens", 0), stats.get("cached", 0), stats.get("output_tokens", 0), models,
            )

    def Result(self, code: int, stderr: str) -> AgentResult:
        error = None
        if self.outcome.get("status") != "success" or code != 0:
            details = self.outcome.get("error") or {}
            if details:
                error = f"{details.get('type', 'error')}: {details.get('message', '')}".strip(": ")
            else:  # stderr holds startup notices too: only a last resort
                reason = self.lastError or stderr.strip() or "no result reported"
                error = f"gemini exited with code {code}: {reason}"
        return AgentResult(text=self.text, sessionId=self.sessionId, costUsd=0.0, error=error)


# ---------- CLI, policy, settings and sessions ----------

@functools.lru_cache(maxsize=None)
def GeminiCommand(explicit: str | None) -> tuple[str, ...]:
    """How to start the CLI: node + gemini.js for npm installs, whose Windows .cmd shim would need cmd.exe."""
    found = explicit or shutil.which("gemini")
    if not found:
        raise GeminiError("Gemini CLI not found: install @google/gemini-cli or set [gemini] cli_path in orch.toml")
    path = Path(found)
    if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        path = path.parent / "node_modules" / "@google" / "gemini-cli" / "bundle" / "gemini.js"
        if not path.is_file():
            raise GeminiError(f"gemini.js not found next to {found}; set [gemini] cli_path in orch.toml")
    if path.suffix.lower() in (".js", ".mjs"):
        node = shutil.which("node")
        if not node:
            raise GeminiError("node not found: the Gemini CLI needs Node.js")
        return node, str(path)
    return (str(path),)


def GeminiDir(ctx: RunContext) -> Path:
    path = ctx.runDir / "gemini"  # per-run policy and settings files, kept with the run's logs
    path.mkdir(exist_ok=True)
    return path


def PolicyFile(role: str, ctx: RunContext) -> Path:
    """The role's tools (runner.ROLE_TOOLS, translated) allowed; everything else denied, MCP tools and subagents
    included. User-tier rules outrank the allow-all of --approval-mode yolo."""
    path = GeminiDir(ctx) / f"policy-{role}.toml"
    if not path.is_file():
        tools = [name for tool in ROLE_TOOLS[role] for name in TOOL_MAP[tool]]
        rules = ['[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 1\n']
        if tools:  # allowRedirection: commands with > or | would otherwise wait for an approval nobody gives
            rules.append(
                f'[[rule]]\ntoolName = {json.dumps(tools)}\ndecision = "allow"\npriority = 100\nallowRedirection = true\n'
            )
        WriteText(path, "\n".join(rules))
    return path


def GeminiHome(ctx: RunContext) -> Path:
    """The run's GEMINI_CLI_HOME. Its user settings are the user's own (auth type and the rest) plus CLAUDE.md as a
    context file, the turn cap, the API retry limit and the guard hook; system settings files would be skipped
    unless admin-owned."""
    home = GeminiDir(ctx) / "home"
    path = home / GEMINI_DIR / "settings.json"
    if not path.is_file():
        settings = UserSettings(ctx)
        settings.setdefault("context", {})["fileName"] = CONTEXT_FILES
        settings.setdefault("model", {})["maxSessionTurns"] = ctx.config.maxTurns
        retries = ctx.config.providers["gemini"].maxRetries
        if retries is not None:  # attempts per API request, the first one included
            settings.setdefault("general", {})["maxAttempts"] = retries + 1
        guard ={"type": "command", "command": ShellCommand([sys.executable, str(GUARD_SCRIPT)]), "timeout": GUARD_TIMEOUT_MS}
        settings.setdefault("hooks", {}).setdefault("BeforeTool", []).append({"matcher": GUARDED_TOOLS, "hooks": [guard]})
        path.parent.mkdir(parents=True, exist_ok=True)
        WriteText(path, json.dumps(settings, ensure_ascii=False, indent=2))
    return home


def UserSettings(ctx: RunContext) -> dict:
    home = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home())
    path = home / GEMINI_DIR / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    except (OSError, ValueError) as error:  # e.g. comments in the file: agents may then miss the auth type
        ctx.log.warning("gemini: cannot copy %s (%s); agents run without the user's Gemini settings", path, error)
        data = {}
    return data if isinstance(data, dict) else {}


def ShellCommand(argv: list[str]) -> str:
    """argv as a command line for the shell the CLI runs hooks in: PowerShell on Windows, bash elsewhere."""
    if os.name == "nt":
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


def ForkSource(sessionId: str, ctx: RunContext) -> Path:
    """The saved session to fork: --session-file continues a copy under a new id and leaves the original as it is."""
    for path in (GeminiHome(ctx) / GEMINI_DIR / "tmp").glob(f"*/chats/session-*-{sessionId[:8]}.jsonl"):
        try:
            with open(path, encoding="utf-8") as file:
                head = json.loads(file.readline())
        except (OSError, ValueError):
            continue
        if isinstance(head, dict) and head.get("sessionId") == sessionId:
            return path
    raise GeminiError(f"saved gemini session {sessionId} not found in this run's Gemini home; cannot fork it")
