"""Tests for git operations using real temporary git repositories."""

import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from obsidian_sync.git_ops import (
    GitError,
    GitInterventionError,
    PullConflictError,
    PushError,
    add_all,
    commit,
    ensure_sync_ready,
    get_changed_files,
    get_staged_files,
    is_ahead,
    is_dirty,
    pull,
    push,
)


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass global git hooks/config so tests run in any environment."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in a repo directory."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


DEFAULT_BRANCH = "main"


@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("Git unavailable"),
        PermissionError("Access denied"),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "Invalid output"),
        subprocess.SubprocessError("Git subprocess failed"),
    ],
)
@pytest.mark.parametrize(
    "operation",
    [
        is_dirty,
        get_changed_files,
        get_staged_files,
        add_all,
        pytest.param(lambda path: commit(path, "Test commit"), id="commit"),
        pull,
        push,
        is_ahead,
        ensure_sync_ready,
    ],
)
def test_git_launch_and_decode_failures_use_git_error_boundary(
    tmp_path: Path, error: Exception, operation: Callable[[Path], object]
) -> None:
    with (
        patch("obsidian_sync.git_ops.subprocess.run", side_effect=error),
        pytest.raises(GitError, match="Cannot") as caught,
    ):
        operation(tmp_path)
    if operation is ensure_sync_ready:
        assert isinstance(caught.value, GitInterventionError)


@pytest.mark.parametrize("query", [is_dirty, get_changed_files, get_staged_files])
def test_unsuccessful_inspection_is_never_empty_success(
    tmp_path: Path, query: Callable[[Path], object]
) -> None:
    with (
        patch(
            "obsidian_sync.git_ops._run_git",
            return_value=subprocess.CompletedProcess([], 128, "", "Inspection failed"),
        ),
        pytest.raises(GitError, match="Inspection failed"),
    ):
        query(tmp_path)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Init a git repo with an initial commit containing a README."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", DEFAULT_BRANCH)
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test User")
    readme = repo / "README.md"
    readme.write_text("# Test Repo\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "Initial commit")
    return repo


@pytest.fixture
def git_repo_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare remote repo and a local clone with an initial commit.

    Returns (local_path, remote_path).
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", DEFAULT_BRANCH)

    # First clone to set up initial commit
    setup = tmp_path / "setup"
    _git(tmp_path, "clone", str(bare), str(setup))
    _git(setup, "config", "user.email", "test@test.com")
    _git(setup, "config", "user.name", "Test User")
    readme = setup / "README.md"
    readme.write_text("# Test Repo\n")
    _git(setup, "add", "-A")
    _git(setup, "commit", "-m", "Initial commit")
    _git(setup, "push", "origin", DEFAULT_BRANCH)

    # Clone again as "local" working copy
    local = tmp_path / "local"
    _git(tmp_path, "clone", str(bare), str(local))
    _git(local, "config", "user.email", "test@test.com")
    _git(local, "config", "user.name", "Test User")

    return local, bare


@pytest.fixture
def dirty_repo(git_repo: Path) -> Path:
    """A git_repo with an uncommitted file 'notes.md'."""
    notes = git_repo / "notes.md"
    notes.write_text("Some notes\n")
    return git_repo


class TestEnsureSyncReady:
    def test_clean_and_dirty_repositories_are_ready(self, dirty_repo: Path) -> None:
        ensure_sync_ready(dirty_repo)
        _git(dirty_repo, "add", "-A")
        _git(dirty_repo, "commit", "-m", "Add notes")
        ensure_sync_ready(dirty_repo)

    @pytest.mark.parametrize(
        "state",
        [
            "MERGE_HEAD",
            "rebase-merge",
            "rebase-apply",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "sequencer",
        ],
    )
    @pytest.mark.parametrize("linked_worktree", [False, True])
    def test_git_resolved_operation_paths(
        self, git_repo: Path, tmp_path: Path, state: str, linked_worktree: bool
    ) -> None:
        repo = git_repo
        if linked_worktree:
            repo = tmp_path / "linked"
            _git(git_repo, "worktree", "add", "-b", "linked", str(repo))
            assert (repo / ".git").is_file()
        state_path = Path(_git(repo, "rev-parse", "--git-path", state).stdout.strip())
        if not state_path.is_absolute():
            state_path = repo / state_path
        if state in {"rebase-merge", "rebase-apply", "sequencer"}:
            state_path.mkdir()
        else:
            state_path.write_text(_git(repo, "rev-parse", "HEAD").stdout)
        with pytest.raises(GitInterventionError, match=state):
            ensure_sync_ready(repo)

    @pytest.mark.parametrize(
        "failed_query",
        [
            ["ls-files", "--unmerged", "-z"],
            *[
                ["rev-parse", "--git-path", state]
                for state in (
                    "MERGE_HEAD",
                    "rebase-merge",
                    "rebase-apply",
                    "CHERRY_PICK_HEAD",
                    "REVERT_HEAD",
                    "sequencer",
                )
            ],
        ],
    )
    def test_failed_query_refuses_sync(self, git_repo: Path, failed_query: list[str]) -> None:
        from obsidian_sync.git_ops import _run_git

        def query(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args == failed_query:
                return subprocess.CompletedProcess(args, 1, "", "query failed")
            return _run_git(repo, args)

        with (
            patch("obsidian_sync.git_ops._run_git", side_effect=query),
            pytest.raises(GitInterventionError, match="Cannot"),
        ):
            ensure_sync_ready(git_repo)

    @pytest.mark.parametrize("error", [OSError("unavailable"), UnicodeError("decode failed")])
    def test_query_exception_refuses_sync(self, git_repo: Path, error: Exception) -> None:
        with (
            patch("obsidian_sync.git_ops._run_git", side_effect=error),
            pytest.raises(GitInterventionError, match="Cannot inspect Git state"),
        ):
            ensure_sync_ready(git_repo)

    def test_unreadable_operation_path_refuses_sync(self, git_repo: Path) -> None:
        with (
            patch("obsidian_sync.git_ops.Path.lstat", side_effect=PermissionError("unreadable")),
            pytest.raises(GitInterventionError, match="Cannot inspect Git state"),
        ):
            ensure_sync_ready(git_repo)

    def test_empty_git_path_refuses_sync(self, git_repo: Path) -> None:
        from obsidian_sync.git_ops import _run_git

        def query(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
            if args == ["rev-parse", "--git-path", "MERGE_HEAD"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            return _run_git(repo, args)

        with (
            patch("obsidian_sync.git_ops._run_git", side_effect=query),
            pytest.raises(GitInterventionError, match="Cannot locate Git state MERGE_HEAD"),
        ):
            ensure_sync_ready(git_repo)


# --- is_dirty ---


class TestIsDirty:
    def test_clean_repo(self, git_repo: Path) -> None:
        assert is_dirty(git_repo) is False

    def test_modified_file(self, git_repo: Path) -> None:
        readme = git_repo / "README.md"
        readme.write_text("# Modified\n")
        assert is_dirty(git_repo) is True

    def test_new_file(self, dirty_repo: Path) -> None:
        assert is_dirty(dirty_repo) is True

    def test_staged_file(self, git_repo: Path) -> None:
        new_file = git_repo / "staged.txt"
        new_file.write_text("staged content\n")
        _git(git_repo, "add", "staged.txt")
        assert is_dirty(git_repo) is True


# --- get_changed_files ---


class TestGetChangedFiles:
    def test_returns_filenames(self, dirty_repo: Path) -> None:
        result = get_changed_files(dirty_repo)
        assert "notes.md" in result

    def test_clean_repo(self, git_repo: Path) -> None:
        result = get_changed_files(git_repo)
        assert result == []


# --- add_all / get_staged_files ---


class TestAddAll:
    def test_stages_everything(self, dirty_repo: Path) -> None:
        add_all(dirty_repo)
        staged = get_staged_files(dirty_repo)
        assert "notes.md" in staged


# --- commit ---


class TestCommit:
    def test_creates_commit(self, dirty_repo: Path) -> None:
        add_all(dirty_repo)
        commit(dirty_repo, "Add notes")
        log = _git(dirty_repo, "log", "--oneline", "-1")
        assert "Add notes" in log.stdout

    def test_with_body(self, dirty_repo: Path) -> None:
        add_all(dirty_repo)
        commit(dirty_repo, "Add notes", body="Detailed body text")
        log = _git(dirty_repo, "log", "-1", "--format=%B")
        assert "Detailed body text" in log.stdout


# --- pull ---


class TestPull:
    @pytest.mark.parametrize("strategy", ["merge", "ff-only"])
    def test_selected_strategy_overrides_conflicting_git_defaults(
        self, git_repo_pair: tuple[Path, Path], tmp_path: Path, strategy: str
    ) -> None:
        local, bare = git_repo_pair
        other = tmp_path / "other"
        _git(tmp_path, "clone", str(bare), str(other))
        _git(other, "config", "user.email", "other@test.com")
        _git(other, "config", "user.name", "Other User")
        (other / "remote.md").write_text("Remote note\n")
        _git(other, "add", "remote.md")
        _git(other, "commit", "-m", "Remote note")
        _git(other, "push", "origin", "main")
        remote_tip = _git(bare, "rev-parse", "refs/heads/main").stdout.strip()

        (local / "local.md").write_text("Local note\n")
        _git(local, "add", "local.md")
        _git(local, "commit", "-m", "Local note")
        local_tip = _git(local, "rev-parse", "HEAD").stdout.strip()
        _git(local, "config", "pull.rebase", "true")
        _git(local, "config", "pull.ff", "only" if strategy == "merge" else "false")

        if strategy == "merge":
            result = pull(local, strategy="merge")
            assert result.success is True
            assert _git(local, "show", "-s", "--format=%P", "HEAD").stdout.split() == [
                local_tip,
                remote_tip,
            ]
            assert (local / "remote.md").read_text() == "Remote note\n"
            assert (local / "local.md").read_text() == "Local note\n"
        else:
            with pytest.raises(GitError, match="git pull failed"):
                pull(local, strategy="ff-only")
            assert _git(local, "rev-parse", "HEAD").stdout.strip() == local_tip
            assert not (local / "remote.md").exists()
        assert _git(bare, "rev-parse", "refs/heads/main").stdout.strip() == remote_tip

    def test_no_changes(self, git_repo_pair: tuple[Path, Path]) -> None:
        local, _remote = git_repo_pair
        result = pull(local)
        assert result.success is True
        assert result.conflict is False

    def test_with_remote_changes(self, git_repo_pair: tuple[Path, Path], tmp_path: Path) -> None:
        local, bare = git_repo_pair

        # Create another clone, push a change
        other = tmp_path / "other"
        _git(tmp_path, "clone", str(bare), str(other))
        _git(other, "config", "user.email", "other@test.com")
        _git(other, "config", "user.name", "Other User")
        new_file = other / "remote_change.txt"
        new_file.write_text("from other\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "Remote change")
        _git(other, "push", "origin", "main")

        result = pull(local)
        assert result.success is True
        assert (local / "remote_change.txt").exists()

    def test_conflict(self, git_repo_pair: tuple[Path, Path], tmp_path: Path) -> None:
        local, bare = git_repo_pair

        # Push conflicting change from another clone
        other = tmp_path / "other"
        _git(tmp_path, "clone", str(bare), str(other))
        _git(other, "config", "user.email", "other@test.com")
        _git(other, "config", "user.name", "Other User")
        readme = other / "README.md"
        readme.write_text("# Other change\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "Other change to README")
        _git(other, "push", "origin", "main")

        # Make conflicting local change
        readme = local / "README.md"
        readme.write_text("# Local change\n")
        _git(local, "add", "-A")
        _git(local, "commit", "-m", "Local change to README")

        with pytest.raises(PullConflictError):
            pull(local)

        # Clean up rebase state
        _git(local, "rebase", "--abort")


# --- push ---


@pytest.mark.parametrize("returncode,output", [(128, ""), (0, "garbled"), (0, "-1")])
def test_ahead_query_failure_is_never_clean_success(returncode: int, output: str) -> None:
    with (
        patch(
            "obsidian_sync.git_ops._run_git",
            return_value=subprocess.CompletedProcess([], returncode, output, "query failed"),
        ),
        pytest.raises(GitError),
    ):
        is_ahead(Path.cwd(), ref="FETCH_HEAD")


class TestPush:
    def test_success(self, git_repo_pair: tuple[Path, Path]) -> None:
        local, _remote = git_repo_pair
        new_file = local / "pushed.txt"
        new_file.write_text("push me\n")
        add_all(local)
        commit(local, "Add pushed file")
        result = push(local)
        assert result.success is True

    def test_no_remote(self, git_repo: Path) -> None:
        new_file = git_repo / "file.txt"
        new_file.write_text("content\n")
        add_all(git_repo)
        commit(git_repo, "Add file")
        with pytest.raises(PushError):
            push(git_repo)
