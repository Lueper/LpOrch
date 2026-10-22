"""Git helpers for code tasks: worktrees, commits, diffs and merges.

Commands run through subprocess with argument lists (no shell) and are SDK-free.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

FALLBACK_IDENTITY = ["-c", "user.name=orchestrator", "-c", "user.email=orchestrator@localhost"]
NO_CHANGES = "no file changes"  # ApplyToWorkingTree: the result adds nothing the working tree does not hold yet
CONFLICT_MARKER = re.compile(r"^(<{7}|>{7})( |$)", re.MULTILINE)
# Caches that workers create by running tests; never commit them even if the repo lacks a .gitignore
COMMIT_EXCLUDES = [
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/.mypy_cache/**",
]


class GitError(Exception):
    """A git command exited with a non-zero status."""


def Git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],  # print non-ASCII paths raw so they can be reused
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and completed.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {completed.stderr.strip()}")
    return completed


def IdentityArgs(cwd: Path) -> list[str]:
    # Fresh repos may lack user.email; commit as the orchestrator instead of failing
    configured = Git(cwd, "config", "user.email", check=False).stdout.strip()
    return [] if configured else FALLBACK_IDENTITY


def ResolveRepo(path: Path) -> tuple[Path, str] | None:
    """Return (repo root, HEAD commit), or None when path is not a git repo with a commit."""
    root = Git(path, "rev-parse", "--show-toplevel", check=False)
    head = Git(path, "rev-parse", "HEAD", check=False)
    if root.returncode != 0 or head.returncode != 0:
        return None
    return Path(root.stdout.strip()), head.stdout.strip()


def SnapshotCommit(repo: Path) -> tuple[str, bool]:
    """(the commit code tasks start from, whether it holds uncommitted changes): HEAD plus the working tree's staged
    and unstaged changes to tracked files, as a commit that touches neither the working tree nor the index
    (git stash create). Results a person has not committed yet - earlier runs' staged code - are then the base
    instead of a conflict. Untracked files are not in it; HEAD alone when nothing is uncommitted."""
    stash = Git(repo, *IdentityArgs(repo), "stash", "create", "orch: run base", check=False).stdout.strip()
    if stash:
        return stash, True
    return Git(repo, "rev-parse", "HEAD").stdout.strip(), False


def RevParse(worktree: Path, revision: str = "HEAD") -> str:
    return Git(worktree, "rev-parse", revision).stdout.strip()


def ResetHard(worktree: Path, commit: str) -> None:
    """Move a worktree of the orchestrator's own (never the project's) back to commit, dropping what came after."""
    Git(worktree, "reset", "--hard", "-q", commit)


def DiffPatch(repo: Path, base: str, branch: str) -> str:
    """The branch's change as a patch a person can apply with git apply --3way."""
    return Git(repo, "diff", "--binary", base, branch).stdout


def CreateWorktree(repo: Path, path: Path, branch: str, base: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Git(repo, "worktree", "add", "-b", branch, str(path), base)


def RemoveWorktree(repo: Path, path: Path) -> None:
    # --force drops leftovers; task work is already committed on its branch or stashed
    Git(repo, "worktree", "remove", "--force", str(path), check=False)


def CommitAll(worktree: Path, message: str) -> bool:
    """Stage and commit everything; False when there was nothing to commit."""
    Git(worktree, "add", "-A", "--", ".", *COMMIT_EXCLUDES)
    # Check the index, not the worktree: excluded cache files stay untracked
    if Git(worktree, "diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    Git(worktree, *IdentityArgs(worktree), "commit", "-m", message)
    return True


def StashChanges(worktree: Path, message: str) -> None:
    # Park partial edits from a failed agent instead of committing or discarding them
    Git(worktree, "stash", "push", "--include-untracked", "-m", message, check=False)


def DiffAgainstBase(worktree: Path, base: str) -> str:
    return Git(worktree, "diff", base, "HEAD").stdout


def StartMerge(worktree: Path, branch: str) -> str | None:
    """Merge branch with a merge commit; returns git's error output on failure."""
    completed = Git(worktree, *IdentityArgs(worktree), "merge", "--no-ff", "--no-edit", branch, check=False)
    return None if completed.returncode == 0 else (completed.stderr or completed.stdout).strip()


def ConflictedFiles(worktree: Path) -> list[str]:
    output = Git(worktree, "diff", "--name-only", "--diff-filter=U").stdout
    return [line for line in output.splitlines() if line.strip()]


def HasConflictMarkers(worktree: Path, files: list[str]) -> bool:
    for name in files:
        path = worktree / name
        if path.is_file() and CONFLICT_MARKER.search(path.read_text(encoding="utf-8", errors="replace")):
            return True
    return False


def FinishMerge(worktree: Path) -> None:
    Git(worktree, "add", "-A", "--", ".", *COMMIT_EXCLUDES)  # a merger that ran tests leaves caches behind
    Git(worktree, *IdentityArgs(worktree), "commit", "--no-edit")


def AbortMerge(worktree: Path) -> None:
    Git(worktree, "merge", "--abort", check=False)


def ChangedFiles(repo: Path, base: str, branch: str) -> list[tuple[str, str]]:
    """(status, path) pairs between base and branch; status is A, M or D."""
    output = Git(repo, "diff", "--name-status", "--no-renames", base, branch).stdout
    return [tuple(line.split("\t", 1)) for line in output.splitlines() if "\t" in line]


def LocalChanges(repo: Path, base: str, paths: list[str]) -> list[str]:
    """Paths whose working-tree content is no longer base's - edited, staged or deleted since - and untracked or
    ignored files in the way of a file the change adds: the user's work, which applying must not overwrite.
    Content is what counts, so committing the very same changes meanwhile is no conflict."""
    edited = Git(repo, "diff", "--name-only", "--no-renames", base, "--", *paths).stdout.splitlines()
    untracked = Git(repo, "ls-files", "--others", "--", *paths).stdout.splitlines()
    return sorted({*edited, *untracked} - {""})


def ApplyToWorkingTree(repo: Path, base: str, branch: str) -> str | None:
    """Stage branch's changes over base in the main working tree without committing; returns why it was skipped.
    base is what the working tree was last brought to: the run's snapshot, or the last result applied."""
    changes = ChangedFiles(repo, base, branch)
    if not changes:
        return NO_CHANGES
    # Never overwrite the user's own work: all-or-nothing, only onto files untouched since base
    conflicts = LocalChanges(repo, base, [path for _, path in changes])
    if conflicts:
        return "local changes in " + ", ".join(conflicts)
    updated = [path for status, path in changes if status != "D"]
    deleted = [path for status, path in changes if status == "D"]
    if updated:
        Git(repo, "checkout", branch, "--", *updated)
    if deleted:
        Git(repo, "rm", "-q", "--", *deleted)
    return None
