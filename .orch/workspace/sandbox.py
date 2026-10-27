"""Task workspaces as plain file copies: [sys] sandboxes of the whole project, the src/ folder a [code] task works in
with its base copy (and the temporary copies split parts work in), and the disposable project copies [test] tasks
run in; diffs, 3-way apply, roll-back, and automated checks.

Plain file copies plus `git merge-file` (which needs no repository), so no task kind needs the project to be a git
repo. SDK-free.
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

from core.paths import IsOsMetadata

# A workspace holds every file of its root except these. [sys] sandboxes are rooted at the project (the whole
# project is in scope), [code] workspaces at their src/ folder.
# Folders left out at any depth: git data, caches, virtual environments and installed packages
EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".venv", "venv", "node_modules"}
# The orchestrator's run artifacts, the sandboxes themselves (made under .orch/worktrees/) and its record of the code
# folders (written while a run lasts), from the root
EXCLUDED_PATHS = (".orch/runs", ".orch/worktrees", ".orch/code_folders.json")
# Local or secret files never enter a sandbox, and a sandbox never writes them back
EXCLUDED_FILES = {".env", ".mcp.json", "settings.local.json"}
SECRET_PREFIX = ".env."  # .env.local, .env.production, ... (.env.example is a template, not a secret)
EXCLUDED_SUFFIXES = {".pyc", ".log"}
# The orchestrator's own settings: the smoke test parses their JSON files (a project's own may be broken on purpose)
SETTINGS_DIRS = (".orch", ".claude")
# A [test] copy of the project leaves out git's own data and caches, and the orchestrator's run artifacts (EXCLUDED_PATHS)
PROJECT_COPY_SKIPPED = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
CHECK_TIMEOUT = 120  # seconds per built-in check
VERIFY_TIMEOUT = 600  # seconds for a task's own "verify:" command
MAX_CHECK_OUTPUT = 4000  # tail of a failed check kept for prompts


def IsSecret(name: str) -> bool:
    return name in EXCLUDED_FILES or (name.startswith(SECRET_PREFIX) and not name.endswith(".example"))


def ExcludedDir(relative: str) -> bool:
    """A folder no workspace descends into, by its posix path from the workspace root."""
    return relative.rsplit("/", 1)[-1] in EXCLUDED_DIRS or relative in EXCLUDED_PATHS


def InScope(relative: str) -> bool:
    """Whether a file, by its posix path from the workspace root, belongs to the workspace."""
    parts = relative.split("/")
    folders = ["/".join(parts[:index]) for index in range(1, len(parts))]
    if relative in EXCLUDED_PATHS or any(ExcludedDir(folder) for folder in folders):
        return False
    return not IsSecret(parts[-1]) and Path(parts[-1]).suffix not in EXCLUDED_SUFFIXES


def WalkFiles(root: Path, start: str = ""):
    """Every file under root/start as (posix path from root, path), skipping excluded folders and file-manager
    metadata (a "._x.py" AppleDouble file would otherwise be compiled by the smoke test)."""
    for folder, dirs, names in os.walk(root / start):
        here = Path(folder).relative_to(root).as_posix()
        dirs[:] = [name for name in dirs if not ExcludedDir(f"{here}/{name}".removeprefix("./"))]
        for name in (name for name in names if not IsOsMetadata(name)):
            path = Path(folder) / name
            yield path.relative_to(root).as_posix(), path


def WorkspaceFiles(root: Path) -> dict[str, Path]:
    return {relative: path for relative, path in WalkFiles(root) if InScope(relative)}


def CopyTree(source: Path, dest: Path) -> Path:
    """Copy the workspace files of source to dest: a run's [sys] snapshot of the project, a sandbox, a [code]
    folder's base, a checkpoint."""
    for relative, path in WorkspaceFiles(source).items():
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def CopyProject(projectDir: Path, dest: Path) -> dict[str, tuple[int, int]]:
    """Copy the project as it is now, uncommitted changes included, for a [test] task to work in; returns the copy's
    files with their size and modification time, which ChangedFiles compares against."""
    skipped = {(projectDir / relative).resolve() for relative in EXCLUDED_PATHS}

    def Ignore(folder: str, names: list[str]) -> list[str]:
        here = Path(folder).resolve()
        return [name for name in names if name in PROJECT_COPY_SKIPPED or here / name in skipped]

    shutil.copytree(projectDir, dest, ignore=Ignore)
    return FileStamps(dest)


def FileStamps(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under root (caches and git data left out) with its size and modification time."""
    stamps = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [name for name in dirs if name not in PROJECT_COPY_SKIPPED]
        for name in names:
            path = Path(folder) / name
            info = path.stat()
            stamps[path.relative_to(root).as_posix()] = (info.st_size, info.st_mtime_ns)
    return stamps


def ChangedFiles(root: Path, stamps: dict[str, tuple[int, int]]) -> list[tuple[str, str]]:
    """(status, path) for files a [test] worker added (A), modified (M) or deleted (D) in its copy."""
    now = FileStamps(root)
    changes = [("D", name) for name in stamps if name not in now]
    changes += [("A" if name not in stamps else "M", name) for name, stamp in now.items() if stamps.get(name) != stamp]
    return sorted(changes, key=lambda change: change[1])


def Changes(base: Path, workspace: Path) -> list[tuple[str, str]]:
    """(status, path) for workspace files the workspace added (A), modified (M) or deleted (D) against base."""
    before, after = WorkspaceFiles(base), WorkspaceFiles(workspace)
    changes = [("D", name) for name in before if name not in after]
    for name, path in after.items():
        if name not in before:
            changes.append(("A", name))
        elif not filecmp.cmp(before[name], path, shallow=False):
            changes.append(("M", name))
    return sorted(changes, key=lambda change: change[1])


def OutOfScope(workspace: Path) -> list[str]:
    """Files a worker created that are no workspace files (secrets, logs); they are never applied."""
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


def Checkpoint(workspace: Path, dest: Path) -> Path:
    """Copy the workspace to dest before an agent edits it again, so a failed attempt can be rolled back. dest lies
    outside the workspace: a [code] task's workspace is its folder in src/."""
    shutil.rmtree(dest, ignore_errors=True)
    return CopyTree(workspace, dest)


def Restore(workspace: Path, checkpoint: Path) -> list[str]:
    """Bring the workspace files back to checkpoint (or a base): files added since are deleted, changed and deleted
    ones copied back. Only workspace files are touched, so a src/ folder keeps its caches and virtual environment.
    Returns the files restored."""
    changes = Changes(checkpoint, workspace)
    for status, name in changes:
        if status == "A":
            (workspace / name).unlink(missing_ok=True)
            continue
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(checkpoint / name, workspace / name)
    return [name for _, name in changes]


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
    sources = sorted(  # skips runs/, worktrees/ and caches
        relative.removeprefix(".orch/") for relative, _ in WalkFiles(root, ".orch") if relative.endswith(".py")
    )
    imports = "; ".join(f"import {ModuleName(relative)}" for relative in sources)
    steps = [
        ("compile", [sys.executable, "-m", "py_compile", *(str(orch / relative) for relative in sources)]),
        ("import", [sys.executable, "-c", f"import sys; sys.path.insert(0, {str(orch)!r}); {imports}"]),
        # --source file: the checks read the sandbox's TASKS.md, never a Notion database over the network
        ("preview", [sys.executable, str(orch / "orchestrate.py"), "preview", "--source", "file"]),
    ]
    results = []
    for name, command in steps:  # each step assumes the previous one passed
        results.append(RunStep(name, command, root, CHECK_TIMEOUT))
        if not results[-1][0]:
            break
    settings = [
        (relative, path) for relative, path in WorkspaceFiles(root).items()
        if path.suffix == ".json" and relative.split("/", 1)[0] in SETTINGS_DIRS
    ]
    return results + [CheckJson(relative, path) for relative, path in settings]


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
    """Automated checks for a code/sys workspace: (passed, report), or None when there is nothing to run.

    phases.RunChecks repeats the "nothing to run" rule (not sys, no verify command) for its start line: keep both in sync.
    """
    results = SmokeTest(workdir) if kind == "sys" else []
    if verifyCommand:
        results.append(RunStep("verify", verifyCommand, workdir, VERIFY_TIMEOUT))
    if not results:
        return None
    return all(ok for ok, _ in results), "\n".join(text for _, text in results)
