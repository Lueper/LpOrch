"""[sys] task workspaces: copies of the orchestrator's own files, diffs, 3-way apply, and automated checks.

Plain file copies plus `git merge-file` (which needs no repository), so [sys] tasks work
whether or not the project is a git repo. SDK-free.
"""
from __future__ import annotations

import difflib
import filecmp
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# The system a [sys] task may change
SCOPE_DIRS = (".orch", ".claude")
SCOPE_FILES = ("CLAUDE.md", "TASKS.md", "orch.toml", "orch.bat", "orch-reset.bat")  # orch.toml: checked by preview
# Run artifacts, sandboxes, caches and local or secret files never enter a sandbox
EXCLUDED_DIRS = {"runs", "worktrees", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv"}
EXCLUDED_FILES = {"settings.local.json"}
EXCLUDED_SUFFIXES = {".pyc", ".log"}
CHECK_TIMEOUT = 120  # seconds per built-in check
VERIFY_TIMEOUT = 600  # seconds for a task's own "verify:" command
MAX_CHECK_OUTPUT = 4000  # tail of a failed check kept for prompts


def InScope(relative: str) -> bool:
    parts = relative.split("/")
    if len(parts) == 1:
        return relative in SCOPE_FILES
    if parts[0] not in SCOPE_DIRS or EXCLUDED_DIRS.intersection(parts[:-1]):
        return False
    return parts[-1] not in EXCLUDED_FILES and Path(parts[-1]).suffix not in EXCLUDED_SUFFIXES


def WalkFiles(root: Path):
    """Every file under root as (relative posix path, path), skipping excluded folders."""
    for folder, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
        for name in names:
            path = Path(folder) / name
            yield path.relative_to(root).as_posix(), path


def SystemFiles(root: Path) -> dict[str, Path]:
    return {relative: path for relative, path in WalkFiles(root) if InScope(relative)}


def CreateSnapshot(projectDir: Path, dest: Path) -> Path:
    """Copy the project's system files to dest: the base every sandbox of this run starts from."""
    for relative in SystemFiles(projectDir):
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(projectDir / relative, target)
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def CopyTree(source: Path, dest: Path) -> Path:
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns(*EXCLUDED_DIRS))
    return dest


def Changes(base: Path, workspace: Path) -> list[tuple[str, str]]:
    """(status, path) for system files the workspace added (A), modified (M) or deleted (D) against base."""
    before, after = SystemFiles(base), SystemFiles(workspace)
    changes = [("D", name) for name in before if name not in after]
    for name, path in after.items():
        if name not in before:
            changes.append(("A", name))
        elif not filecmp.cmp(before[name], path, shallow=False):
            changes.append(("M", name))
    return sorted(changes, key=lambda change: change[1])


def OutOfScope(workspace: Path) -> list[str]:
    """Files a worker created outside the system scope; they are never applied."""
    return sorted(relative for relative, _ in WalkFiles(workspace) if not InScope(relative))


def DiffLines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    return [line if line.endswith("\n") else line + "\n" for line in lines]


def Diff(base: Path, workspace: Path) -> str:
    chunks = (
        difflib.unified_diff(DiffLines(base / name), DiffLines(workspace / name), f"a/{name}", f"b/{name}")
        for _, name in Changes(base, workspace)
    )
    return "".join("".join(chunk) for chunk in chunks)


def Merge3(current: Path, base: Path, other: Path) -> bytes | None:
    """git merge-file: other's changes over current, relative to base; None on conflict."""
    try:
        completed = subprocess.run(
            ["git", "merge-file", "-p", str(current), str(base), str(other)], capture_output=True
        )
    except OSError:
        return None
    return completed.stdout if completed.returncode == 0 else None


def ReadBytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def PlanApply(base: Path, target: Path, workspace: Path) -> tuple[dict[str, bytes | None], list[str]]:
    """New content per target file (None = delete) carrying workspace's changes over base; plus conflicts."""
    updates: dict[str, bytes | None] = {}
    conflicts = []
    for status, name in Changes(base, workspace):
        original, mine, theirs = ReadBytes(base / name), ReadBytes(target / name), ReadBytes(workspace / name)
        if mine == theirs:  # already there
            continue
        if mine == original:  # untouched in target since base
            updates[name] = theirs
            continue
        merged = Merge3(target / name, base / name, workspace / name) if status == "M" and mine is not None else None
        if merged is None:
            conflicts.append(name)
        else:
            updates[name] = merged
    return updates, conflicts


def ApplyChanges(base: Path, target: Path, workspace: Path, backupDir: Path | None = None) -> tuple[list[str], list[str]]:
    """Apply workspace's changes to target, all or nothing; returns (changed files, conflicting files)."""
    updates, conflicts = PlanApply(base, target, workspace)
    if conflicts:
        return [], conflicts
    for name, content in updates.items():
        path = target / name
        if backupDir and path.is_file():
            (backupDir / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backupDir / name)
        if content is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return sorted(updates), []


def SaveChangedFiles(workspace: Path, names: list[str], dest: Path) -> None:
    """Keep a copy of changed files that could not be applied, for a manual merge."""
    for name in names:
        if (workspace / name).is_file():
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(workspace / name, dest / name)


def Checkpoint(workspace: Path) -> Path:
    """Copy the workspace before an agent edits it again, so a failed attempt can be rolled back."""
    checkpoint = workspace.with_name(f"{workspace.name}.checkpoint")
    shutil.rmtree(checkpoint, ignore_errors=True)
    return CopyTree(workspace, checkpoint)


def Restore(workspace: Path, checkpoint: Path) -> None:
    shutil.rmtree(workspace, ignore_errors=True)
    CopyTree(checkpoint, workspace)


# ---------- automated checks ----------

def Tail(text: str) -> str:
    return text if len(text) <= MAX_CHECK_OUTPUT else "[...]\n" + text[-MAX_CHECK_OUTPUT:]


def RunStep(name: str, command: list[str] | str, cwd: Path, timeout: int) -> tuple[bool, str]:
    # A string command comes from the user's own TASKS.md ("verify:") and runs through the shell
    try:
        completed = subprocess.run(
            command, cwd=cwd, shell=isinstance(command, str), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"{name}: FAILED - {error}"
    if completed.returncode == 0:
        return True, f"{name}: ok"
    return False, f"{name}: FAILED (exit {completed.returncode})\n{Tail(completed.stdout + completed.stderr)}"


def SmokeTest(root: Path) -> list[tuple[bool, str]]:
    """Compile and import every orchestrator module in root (packages included), run its preview, then parse its JSON
    settings."""
    orch = root / ".orch"
    sources = sorted(relative for relative, _ in WalkFiles(orch) if relative.endswith(".py"))  # skips runs/, caches
    imports = "; ".join(f"import {ModuleName(relative)}" for relative in sources)
    steps = [
        ("compile", [sys.executable, "-m", "py_compile", *(str(orch / relative) for relative in sources)]),
        ("import", [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(orch)!r}); {imports}"]),
        ("preview", [sys.executable, str(orch / "orchestrate.py"), "preview"]),
    ]
    results = []
    for name, command in steps:  # each step assumes the previous one passed
        results.append(RunStep(name, command, root, CHECK_TIMEOUT))
        if not results[-1][0]:
            break
    return results + [CheckJson(relative, path) for relative, path in SystemFiles(root).items() if path.suffix == ".json"]


def ModuleName(relative: str) -> str:
    """The import name of a module under .orch/: "agents/claude.py" -> "agents.claude", "core/__init__.py" -> "core"."""
    return relative.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def CheckJson(relative: str, path: Path) -> tuple[bool, str]:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        return False, f"json {relative}: FAILED - {error}"
    return True, f"json {relative}: ok"


def RunChecks(kind: str, workdir: Path, verifyCommand: str | None) -> tuple[bool, str] | None:
    """Automated checks for a code/sys workspace: (passed, report), or None when there is nothing to run."""
    results = SmokeTest(workdir) if kind == "sys" else []
    if verifyCommand:
        results.append(RunStep("verify", verifyCommand, workdir, VERIFY_TIMEOUT))
    if not results:
        return None
    return all(ok for ok, _ in results), "\n".join(text for _, text in results)
