"""BeforeTool hook of Gemini agents (registered by gemini.py): the project's Claude Code guards applied to Gemini's
write and shell tools, so a role runs under the same rules on either provider.

Reads the project's own .claude/settings.json - never a sandbox copy an agent could edit - and checks
write_file (as Write), replace (as Edit) and run_shell_command (as Bash) against:
  - permissions.deny rules: "Bash", "Bash(git push --force*)", "Edit(.env)" ... (globs, case-insensitive; a command
    is also checked part by part across &&, ||, ; and |)
  - PreToolUse command hooks whose matcher fits (e.g. protect-files.sh), run through bash with Claude Code's input;
    exit code 2 or a "block"/"deny" decision blocks the call
Blocking = exit code 2 with the reason on stderr. Fails closed: input or settings that cannot be read, or a hook that
cannot be run, block the call too. SDK-free and standalone (stdlib only): the Gemini CLI runs it by its path.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]  # agents/ -> .orch/ -> the project
SETTINGS_FILE = PROJECT_DIR / ".claude" / "settings.json"
CLAUDE_TOOLS = {"write_file": "Write", "replace": "Edit", "run_shell_command": "Bash"}
DENY_RULE = re.compile(r"^(?P<tool>\w+)(?:\((?P<pattern>.*)\))?$", re.DOTALL)
COMMAND_SEPARATOR = re.compile(r"&&|\|\||;|\||\n")
DEFAULT_HOOK_TIMEOUT = 60  # seconds, Claude Code's default
BLOCK = 2  # the exit code both CLIs read as "deny this call"


def Main() -> int:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        tool = CLAUDE_TOOLS.get(event.get("tool_name", ""))
        if tool is None:
            return 0
        toolInput = event.get("tool_input") or {}
        settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.is_file() else {}
        reason = DeniedByRule(settings, tool, toolInput, event) or DeniedByHook(settings, tool, toolInput, event)
    except Exception as error:  # fail closed: a call that cannot be checked does not run
        reason = f"Blocked: the orchestrator's guard could not check this call ({type(error).__name__}: {error})"
    if reason:
        print(reason, file=sys.stderr)
        return BLOCK
    return 0


# ---------- permissions.deny ----------

def DeniedByRule(settings: dict, tool: str, toolInput: dict, event: dict) -> str | None:
    for rule in settings.get("permissions", {}).get("deny", []):
        match = DENY_RULE.match(str(rule).strip())
        if not match or match["tool"] != tool:
            continue
        pattern = match["pattern"]
        if pattern is None or any(Matches(subject, pattern) for subject in Subjects(tool, toolInput, event)):
            return f"Blocked by the deny rule {rule} in .claude/settings.json"
    return None


def Subjects(tool: str, toolInput: dict, event: dict) -> list[str]:
    """What a deny pattern is matched against: the command and each chained part, or the file path in its forms."""
    if tool == "Bash":
        command = str(toolInput.get("command", ""))
        return [command, *(part.strip() for part in COMMAND_SEPARATOR.split(command) if part.strip())]
    path = Path(str(toolInput.get("file_path", "")))
    cwd = Path(event.get("cwd") or PROJECT_DIR)
    full = path if path.is_absolute() else cwd / path
    try:
        relative = full.resolve().relative_to(cwd.resolve()).as_posix()
    except ValueError:  # outside the agent's folder: only the full path and the name can match
        relative = full.as_posix()
    return [str(path), full.as_posix(), relative, path.name]


def Matches(subject: str, pattern: str) -> bool:
    glob = pattern[:-2] + "*" if pattern.endswith(":*") else pattern  # Claude Code's prefix form "npm run test:*"
    globs = [glob, glob[3:]] if glob.startswith("**/") else [glob]  # "**/.env" also matches ".env" itself
    return any(fnmatch.fnmatchcase(subject.lower(), item.lower()) for item in globs)


# ---------- PreToolUse hooks ----------

def DeniedByHook(settings: dict, tool: str, toolInput: dict, event: dict) -> str | None:
    claudeEvent = {
        "session_id": event.get("session_id", ""),
        "cwd": event.get("cwd") or str(PROJECT_DIR),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": toolInput,  # Gemini's keys are Claude Code's: file_path, content, old_string, new_string, command
    }
    for entry in settings.get("hooks", {}).get("PreToolUse", []):
        if not MatcherFits(str(entry.get("matcher", "")), tool):
            continue
        for hook in entry.get("hooks", []):
            reason = RunHook(hook, claudeEvent) if hook.get("type") == "command" else None
            if reason:
                return reason
    return None


def MatcherFits(matcher: str, tool: str) -> bool:
    if matcher in ("", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return matcher == tool


def RunHook(hook: dict, claudeEvent: dict) -> str | None:
    """The block reason from one command hook, run as Claude Code runs it: bash, the event as JSON on stdin."""
    completed = subprocess.run(
        [FindBash(), "-c", hook["command"]],
        input=json.dumps(claudeEvent).encode("utf-8"),
        capture_output=True,
        timeout=hook.get("timeout", DEFAULT_HOOK_TIMEOUT),
        cwd=claudeEvent["cwd"],
        env={**os.environ, "CLAUDE_PROJECT_DIR": PROJECT_DIR.as_posix()},
    )
    if completed.returncode == BLOCK:
        return completed.stderr.decode("utf-8", errors="replace").strip() or f"Blocked by hook: {hook['command']}"
    if completed.returncode == 0:
        return OutputDecision(completed.stdout.decode("utf-8", errors="replace"))
    return None  # other exit codes are non-blocking hook errors in Claude Code as well


def OutputDecision(stdout: str) -> str | None:
    """A "block"/"deny" decision in a hook's JSON output, Claude Code's structured way to block."""
    try:
        output = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(output, dict):
        return None
    specific = output.get("hookSpecificOutput") or {}
    if output.get("decision") == "block" or specific.get("permissionDecision") == "deny":
        return output.get("reason") or specific.get("permissionDecisionReason") or "Blocked by hook"
    return None


def FindBash() -> str:
    """Claude Code runs hooks in Git Bash on Windows; System32's bash.exe would start WSL instead."""
    explicit = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if explicit:
        return explicit
    if os.name != "nt":
        return shutil.which("bash") or "/bin/bash"
    git = shutil.which("git")
    for folder in Path(git).resolve().parents if git else []:
        if (folder / "bin" / "bash.exe").is_file():
            return str(folder / "bin" / "bash.exe")
    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    raise FileNotFoundError("Git Bash not found; set CLAUDE_CODE_GIT_BASH_PATH")


if __name__ == "__main__":
    sys.exit(Main())
