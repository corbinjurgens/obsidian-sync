"""Git operations for obsidian-sync."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

CONFLICT_MARKERS = ("CONFLICT", "could not apply", "Merge conflict")


@dataclass
class PullResult:
    success: bool
    conflict: bool
    message: str


@dataclass
class PushResult:
    success: bool
    message: str


class GitError(Exception):
    """Base exception for git operations."""


class GitInterventionError(GitError):
    """Raised when sync cannot safely mutate the repository."""


class PullConflictError(GitError):
    """Raised when pull --rebase encounters a conflict."""


class PushError(GitError):
    """Raised when push fails."""


def _run_git(repo_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command in the repo directory. Returns completed process."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, UnicodeError, subprocess.SubprocessError) as e:
        raise GitError(f"Cannot run git {args[0]}: {e}") from e


def ensure_sync_ready(repo_path: Path) -> None:
    """Refuse unresolved operations before sync can stage, commit, pull or push."""
    try:
        result = _run_git(repo_path, ["ls-files", "--unmerged", "-z"])
        if result.returncode != 0:
            raise GitInterventionError(f"Cannot query unmerged index entries: {result.stderr}")
        if result.stdout:
            raise GitInterventionError("Unmerged index entries require manual intervention")

        for state in (
            "MERGE_HEAD",
            "rebase-merge",
            "rebase-apply",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "sequencer",
        ):
            result = _run_git(repo_path, ["rev-parse", "--git-path", state])
            if result.returncode != 0 or not result.stdout.strip():
                raise GitInterventionError(f"Cannot locate Git state {state}: {result.stderr}")
            state_path = Path(result.stdout.strip())
            if not state_path.is_absolute():
                state_path = repo_path / state_path
            try:
                state_path.lstat()
            except FileNotFoundError:
                continue
            raise GitInterventionError(
                f"Unfinished Git operation ({state}) requires manual intervention"
            )
    except GitInterventionError:
        raise
    except (GitError, OSError, UnicodeError, subprocess.SubprocessError) as e:
        raise GitInterventionError(f"Cannot inspect Git state: {e}") from e


def is_dirty(repo_path: Path) -> bool:
    """Check working tree for changes (modified, staged, or untracked)."""
    result = _run_git(repo_path, ["status", "--porcelain"])
    if result.returncode != 0:
        raise GitError(f"Cannot inspect working tree: {result.stderr}")
    return bool(result.stdout.strip())


def get_changed_files(repo_path: Path) -> list[str]:
    """List files with uncommitted changes (modified + untracked).

    Uses ``git status --porcelain -u`` to enumerate individual untracked files
    rather than collapsing them into directory entries.
    """
    result = _run_git(repo_path, ["status", "--porcelain", "-u"])
    if result.returncode != 0:
        raise GitError(f"Cannot list changed files: {result.stderr}")
    files: list[str] = []
    for line in result.stdout.splitlines():
        if line.strip():
            # Porcelain format: XY filename (first 3 chars are status + space)
            files.append(line[3:].strip())
    return files


def get_staged_files(repo_path: Path) -> list[str]:
    """List files in the staging area."""
    result = _run_git(repo_path, ["diff", "--cached", "--name-only"])
    if result.returncode != 0:
        raise GitError(f"Cannot list staged files: {result.stderr}")
    return [f for f in result.stdout.splitlines() if f.strip()]


def add_all(repo_path: Path) -> None:
    """Stage all changes including untracked files."""
    result = _run_git(repo_path, ["add", "-A"])
    if result.returncode != 0:
        raise GitError(f"git add -A failed: {result.stderr}")


def commit(repo_path: Path, message: str, body: str | None = None) -> None:
    """Create a commit with the given message and optional body."""
    args = ["commit", "--no-verify", "-m", message]
    if body is not None:
        args.extend(["-m", body])
    result = _run_git(repo_path, args)
    if result.returncode != 0:
        raise GitError(f"git commit failed: {result.stderr}")


def pull(
    repo_path: Path,
    remote: str = "origin",
    branch: str = "main",
    strategy: str = "rebase",
) -> PullResult:
    """Pull from remote. Raises PullConflictError on conflicts."""
    if strategy == "rebase":
        args = ["pull", "--rebase", remote, branch]
    elif strategy == "ff-only":
        args = ["pull", "--ff-only", remote, branch]
    else:
        args = ["pull", "--no-rebase", "--ff", remote, branch]

    result = _run_git(repo_path, args)
    combined_output = result.stdout + result.stderr

    if result.returncode != 0:
        for marker in CONFLICT_MARKERS:
            if marker in combined_output:
                raise PullConflictError(combined_output)
        raise GitError(f"git pull failed: {combined_output}")

    return PullResult(success=True, conflict=False, message=combined_output.strip())


def is_ahead(
    repo_path: Path,
    remote: str = "origin",
    branch: str = "main",
    *,
    ref: str | None = None,
) -> bool:
    """Compare HEAD with a tracking ref, or an explicitly fetched reference."""
    reference = ref if ref is not None else f"{remote}/{branch}"
    result = _run_git(repo_path, ["rev-list", f"{reference}..HEAD", "--count"])
    if result.returncode != 0:
        raise GitError(f"Cannot compare outstanding commits: {result.stderr}")
    try:
        count = int(result.stdout.strip())
    except ValueError as e:
        raise GitError(f"Invalid outstanding commit count: {result.stdout!r}") from e
    if count < 0:
        raise GitError(f"Invalid outstanding commit count: {count}")
    return count > 0


def push(
    repo_path: Path,
    remote: str = "origin",
    branch: str = "main",
) -> PushResult:
    """Push to remote. Raises PushError on failure."""
    result = _run_git(repo_path, ["push", "--no-verify", remote, branch])
    if result.returncode != 0:
        raise PushError(f"git push failed: {result.stderr}")
    return PushResult(success=True, message=result.stdout.strip())
