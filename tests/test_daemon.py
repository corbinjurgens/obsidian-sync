"""Tests for the obsidian-sync daemon module."""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from obsidian_sync.config import SyncConfig
from obsidian_sync.daemon import LockError, SyncDaemon, SyncResult, _is_pid_alive
from obsidian_sync.git_ops import GitError, _run_git

DEFAULT_BRANCH = "main"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in a repo directory."""
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass global git hooks/config so tests run in any environment."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


@pytest.fixture
def vault_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare remote + local clone for a vault."""
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", DEFAULT_BRANCH)

    setup = tmp_path / "setup"
    _git(tmp_path, "clone", str(bare), str(setup))
    _git(setup, "config", "user.email", "test@test.com")
    _git(setup, "config", "user.name", "Test User")
    readme = setup / "README.md"
    readme.write_text("# Vault\n")
    _git(setup, "add", "-A")
    _git(setup, "commit", "-m", "Initial commit")
    _git(setup, "push", "origin", DEFAULT_BRANCH)

    local = tmp_path / "vault"
    _git(tmp_path, "clone", str(bare), str(local))
    _git(local, "config", "user.email", "test@test.com")
    _git(local, "config", "user.name", "Test User")

    return local, bare


@pytest.fixture
def daemon_config(vault_with_remote: tuple[Path, Path], tmp_path: Path) -> SyncConfig:
    """Create a SyncConfig pointing at the test vault."""
    local, _bare = vault_with_remote
    return SyncConfig(
        vault_path=str(local),
        lock_path=str(tmp_path / "daemon.lock"),
        log=__import__("obsidian_sync.config", fromlist=["LogSettings"]).LogSettings(
            path=str(tmp_path / "sync.log"),
        ),
    )


@pytest.fixture
def daemon(daemon_config: SyncConfig) -> SyncDaemon:
    """Create a SyncDaemon instance."""
    return SyncDaemon(daemon_config)


def _git_path(repo: Path, name: str) -> Path:
    path = Path(_git(repo, "rev-parse", "--git-path", name).stdout.strip())
    return path if path.is_absolute() else repo / path


def _snapshot_git_state(repo: Path, remote: Path) -> dict[str, object]:
    """Include working files, exact index bytes, HEAD, refs and operation metadata."""
    state: dict[str, object] = {
        "files": {
            str(path.relative_to(repo)): path.read_bytes()
            for path in repo.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(repo).parts
        },
        "index_entries": _git(repo, "ls-files", "--stage", "-z").stdout,
        "head": _git_path(repo, "HEAD").read_bytes(),
        "refs": _git(repo, "show-ref", "--head").stdout,
        "remote_refs": _git(remote, "show-ref", "--head").stdout,
    }
    for name in (
        "MERGE_HEAD",
        "rebase-merge",
        "rebase-apply",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "sequencer",
    ):
        path = _git_path(repo, name)
        if path.is_dir():
            state[name] = {
                str(child.relative_to(path)): child.read_bytes()
                for child in path.rglob("*")
                if child.is_file()
            }
        elif path.exists():
            state[name] = path.read_bytes()
    state["index"] = _git_path(repo, "index").read_bytes()
    return state


def _start_unfinished_operation(repo: Path, operation: str) -> None:
    """Use Git itself to leave an operation interrupted by a content conflict."""
    branch = _git(repo, "branch", "--show-current").stdout.strip()
    _git(repo, "checkout", "-b", "topic")
    (repo / "README.md").write_text("# Topic Vault\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "Topic change")
    topic = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "topic.md").write_text("Another topic change\n")
    _git(repo, "add", "topic.md")
    _git(repo, "commit", "-m", "Second topic change")
    second_topic = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", branch)
    (repo / "README.md").write_text("# Local Vault\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "Local change")
    commands = {
        "merge": ["merge", topic],
        "rebase-merge": ["rebase", "--merge", topic],
        "rebase-apply": ["rebase", "--apply", topic],
        "cherry-pick": ["cherry-pick", topic],
        "revert": ["revert", "--no-edit", topic],
        "sequencer": ["cherry-pick", topic, second_topic],
    }
    result = subprocess.run(["git", *commands[operation]], cwd=repo, capture_output=True, text=True)
    assert result.returncode != 0, result.stdout + result.stderr
    assert _git(repo, "ls-files", "--unmerged").stdout
    expected_state = {
        "merge": "MERGE_HEAD",
        "rebase-merge": "rebase-merge",
        "rebase-apply": "rebase-apply",
        "cherry-pick": "CHERRY_PICK_HEAD",
        "revert": "REVERT_HEAD",
        "sequencer": "sequencer",
    }[operation]
    assert _git_path(repo, expected_state).exists()


class TestUnfinishedGitOperations:
    def test_unmerged_index_without_operation_marker_is_preserved(
        self, daemon: SyncDaemon, vault_with_remote: tuple[Path, Path]
    ) -> None:
        local, bare = vault_with_remote
        _start_unfinished_operation(local, "merge")
        _git_path(local, "MERGE_HEAD").unlink()
        assert _git(local, "ls-files", "--unmerged").stdout
        before = _snapshot_git_state(local, bare)
        with patch.object(daemon, "_is_obsidian_running", return_value=False):
            result = daemon.run_once()
        assert result.intervention_required is True
        assert result.error is not None and "Unmerged index entries" in result.error
        assert _snapshot_git_state(local, bare) == before

    @pytest.mark.parametrize("strategy", ["rebase", "merge", "ff-only"])
    @pytest.mark.parametrize(
        "operation", ["merge", "rebase-merge", "rebase-apply", "cherry-pick", "revert", "sequencer"]
    )
    def test_repeated_sync_preserves_unresolved_state(
        self,
        daemon: SyncDaemon,
        vault_with_remote: tuple[Path, Path],
        strategy: str,
        operation: str,
    ) -> None:
        local, bare = vault_with_remote
        daemon.config.sync.pull_strategy = strategy
        _start_unfinished_operation(local, operation)
        before = _snapshot_git_state(local, bare)
        with (
            patch.object(daemon, "_is_obsidian_running", return_value=False),
            patch("obsidian_sync.daemon.add_all") as stage,
            patch("obsidian_sync.daemon.commit") as commit,
            patch("obsidian_sync.daemon.pull") as pull,
            patch("obsidian_sync.daemon.push") as push,
        ):
            for _ in range(2):
                result = daemon.run_once()
                assert result.synced is False
                assert result.intervention_required is True
                assert result.stop_required is False
                assert result.error is not None and "manual intervention" in result.error
                assert _snapshot_git_state(local, bare) == before

            # Staging a manual resolution still does not authorize completing the operation.
            (local / "README.md").write_text("# Staged manual resolution\n")
            _git(local, "add", "README.md")
            assert _git(local, "ls-files", "--unmerged").stdout == ""
            before = _snapshot_git_state(local, bare)
            for _ in range(2):
                result = daemon.run_once()
                assert result.intervention_required is True
                assert result.stop_required is False
                assert result.error is not None and "Unfinished Git operation" in result.error
                assert _snapshot_git_state(local, bare) == before
            stage.assert_not_called()
            commit.assert_not_called()
            pull.assert_not_called()
            push.assert_not_called()

    @pytest.mark.parametrize("strategy", ["rebase", "merge", "ff-only"])
    @pytest.mark.parametrize("linked_worktree", [False, True])
    def test_staged_unfinished_merge_is_preserved(
        self,
        daemon: SyncDaemon,
        vault_with_remote: tuple[Path, Path],
        tmp_path: Path,
        strategy: str,
        linked_worktree: bool,
    ) -> None:
        local, bare = vault_with_remote
        if linked_worktree:
            linked = tmp_path / "linked"
            _git(local, "worktree", "add", "-b", "linked", str(linked))
            local = linked
            daemon.config.vault_path = str(local)
        daemon.config.sync.pull_strategy = strategy
        _start_unfinished_operation(local, "merge")
        (local / "README.md").write_text("# Manually resolved but uncommitted\n")
        _git(local, "add", "README.md")
        assert _git(local, "ls-files", "--unmerged").stdout == ""
        assert _git(local, "diff", "--cached", "--name-only").stdout == "README.md\n"
        before = _snapshot_git_state(local, bare)
        with patch.object(daemon, "_is_obsidian_running", return_value=False):
            for _ in range(2):
                result = daemon.sync_cycle()
                assert result.synced is False
                assert result.intervention_required is True
                assert result.stop_required is False
                assert result.error is not None and "MERGE_HEAD" in result.error
                assert _snapshot_git_state(local, bare) == before

    @pytest.mark.parametrize("strategy", ["rebase", "merge", "ff-only"])
    @pytest.mark.parametrize("failed_query", ["index", "path", "exception"])
    def test_unqueryable_state_refuses_all_mutations(
        self,
        daemon: SyncDaemon,
        vault_with_remote: tuple[Path, Path],
        strategy: str,
        failed_query: str,
    ) -> None:
        local, bare = vault_with_remote
        daemon.config.sync.pull_strategy = strategy
        (local / "note.md").write_text("Unstaged note\n")
        (local / "staged.md").write_text("Staged note\n")
        _git(local, "add", "staged.md")
        before = _snapshot_git_state(local, bare)

        def query(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
            if failed_query == "exception":
                raise OSError("Git unavailable")
            if (failed_query == "index" and args[0] == "ls-files") or (
                failed_query == "path" and args == ["rev-parse", "--git-path", "sequencer"]
            ):
                return subprocess.CompletedProcess(args, 1, "", "query failed")
            return _run_git(repo, args)

        with (
            patch.object(daemon, "_is_obsidian_running", return_value=False),
            patch("obsidian_sync.git_ops._run_git", side_effect=query),
            patch("obsidian_sync.daemon.add_all") as stage,
            patch("obsidian_sync.daemon.commit") as commit,
            patch("obsidian_sync.daemon.pull") as pull,
            patch("obsidian_sync.daemon.push") as push,
        ):
            result = daemon.sync_cycle()
        assert result.intervention_required is True
        assert result.stop_required is False
        assert result.synced is False
        assert result.error is not None and "manual intervention" in result.error
        stage.assert_not_called()
        commit.assert_not_called()
        pull.assert_not_called()
        push.assert_not_called()
        assert _snapshot_git_state(local, bare) == before

    def test_loop_stops_on_structured_signal(self, daemon: SyncDaemon) -> None:
        result = SyncResult(
            False, 0, "Inspection failed", error="Inspection failed", stop_required=True
        )
        with (
            patch.object(daemon, "_setup_signals"),
            patch.object(daemon, "_acquire_lock"),
            patch.object(daemon, "_release_lock") as release,
            patch.object(daemon, "_stop_requested", return_value=False),
            patch.object(daemon, "sync_cycle", return_value=result) as sync,
            patch.object(daemon, "_sleep") as sleep,
            pytest.raises(SystemExit) as error,
        ):
            daemon.run()
        assert error.value.code == 1
        sync.assert_called_once()
        sleep.assert_not_called()
        release.assert_called_once()
        assert daemon._running is False

    def test_loop_keeps_running_for_manual_intervention(self, daemon: SyncDaemon) -> None:
        result = SyncResult(
            False,
            0,
            "Unfinished operation",
            error="Unfinished operation",
            intervention_required=True,
        )

        def stop_after_wait(_seconds: int) -> None:
            daemon._running = False

        with (
            patch.object(daemon, "_setup_signals"),
            patch.object(daemon, "_acquire_lock"),
            patch.object(daemon, "_release_lock"),
            patch.object(daemon, "_stop_requested", return_value=False),
            patch.object(daemon, "sync_cycle", return_value=result) as sync,
            patch.object(daemon, "_sleep", side_effect=stop_after_wait) as sleep,
        ):
            daemon.run()
        sync.assert_called_once()
        sleep.assert_called_once_with(daemon.config.sync.interval_seconds)

    def test_transient_pull_error_still_retries(self, daemon: SyncDaemon) -> None:
        def stop_after_wait(_seconds: int) -> None:
            daemon._running = False

        with (
            patch.object(daemon, "_setup_signals"),
            patch.object(daemon, "_is_obsidian_running", return_value=False),
            patch.object(daemon, "_stop_requested", return_value=False),
            patch("obsidian_sync.daemon.pull", side_effect=GitError("server conflict response")),
            patch.object(daemon, "_sleep", side_effect=stop_after_wait) as sleep,
        ):
            daemon.run()
        sleep.assert_called_once_with(daemon.config.sync.interval_seconds)


class TestLockFile:
    def test_acquire_and_release(self, daemon: SyncDaemon, tmp_path: Path) -> None:
        lock_path = Path(daemon.config.lock_path)
        daemon._acquire_lock()
        assert lock_path.exists()
        assert lock_path.read_text().strip() == str(os.getpid())
        daemon._release_lock()
        assert not lock_path.exists()

    def test_acquire_fails_when_held(self, daemon: SyncDaemon) -> None:
        daemon._acquire_lock()
        other = SyncDaemon(daemon.config)
        with pytest.raises(LockError, match="Another daemon is running"):
            other._acquire_lock()
        daemon._release_lock()

    def test_stale_lock_overwritten(self, daemon: SyncDaemon) -> None:
        lock_path = Path(daemon.config.lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("99999999")  # PID that doesn't exist
        daemon._acquire_lock()
        assert lock_path.read_text().strip() == str(os.getpid())
        daemon._release_lock()


class TestIsPidAlive:
    def test_current_process(self) -> None:
        assert _is_pid_alive(os.getpid()) is True

    def test_nonexistent_pid(self) -> None:
        assert _is_pid_alive(99999999) is False


class TestObsidianDetection:
    def test_obsidian_not_running(self, daemon: SyncDaemon) -> None:
        with (
            patch("obsidian_sync.daemon.platform.system", return_value="Linux"),
            patch("obsidian_sync.daemon.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=1)
            assert daemon._is_obsidian_running() is False

    def test_obsidian_running(self, daemon: SyncDaemon) -> None:
        with (
            patch("obsidian_sync.daemon.platform.system", return_value="Darwin"),
            patch("obsidian_sync.daemon.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            assert daemon._is_obsidian_running() is True


class TestSyncCycle:
    def test_clean_repo(self, daemon: SyncDaemon) -> None:
        with patch.object(daemon, "_is_obsidian_running", return_value=False):
            result = daemon.sync_cycle()
        assert result.synced is True
        assert result.files_changed == 0
        assert "clean" in result.message.lower()

    def test_dirty_repo_syncs(
        self, daemon: SyncDaemon, vault_with_remote: tuple[Path, Path]
    ) -> None:
        local, _bare = vault_with_remote
        note = local / "note.md"
        note.write_text("New note\n")
        with patch.object(daemon, "_is_obsidian_running", return_value=False):
            result = daemon.sync_cycle()
        assert result.synced is True
        assert result.files_changed == 1

    def test_defers_when_obsidian_running(self, daemon: SyncDaemon) -> None:
        with patch.object(daemon, "_is_obsidian_running", return_value=True):
            result = daemon.sync_cycle()
        assert result.deferred is True
        assert result.synced is False

    def test_dirty_tree_with_overlapping_remote_changes(
        self, daemon: SyncDaemon, vault_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Regression: dirty tree + same file changed remotely must not fail.

        Before the fix, pull --rebase would fail with 'cannot pull with rebase:
        You have unstaged changes' when local dirty files overlapped with remote.
        """
        local, bare = vault_with_remote

        # Make README multi-line so edits to different regions can auto-merge
        (local / "README.md").write_text("# Vault\n\nOriginal content\n")
        _git(local, "add", "-A")
        _git(local, "commit", "-m", "multi-line README")
        _git(local, "push", "origin", "main")

        # Simulate another device appending to README.md and pushing
        other = tmp_path / "other"
        _git(tmp_path, "clone", str(bare), str(other))
        _git(other, "config", "user.email", "other@test.com")
        _git(other, "config", "user.name", "Other")
        _git(other, "config", "core.hooksPath", "/dev/null")
        readme = (other / "README.md").read_text()
        (other / "README.md").write_text(readme + "\nAppended by other device\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "remote README append")
        _git(other, "push", "origin", "main")

        # Local has uncommitted edit to the SAME file (different region)
        (local / "README.md").write_text("# Vault (edited locally)\n\nOriginal content\n")

        with patch.object(daemon, "_is_obsidian_running", return_value=False):
            result = daemon.sync_cycle()

        # Must succeed — not stuck with "cannot pull with rebase"
        assert result.synced is True
        assert result.error is None
        assert result.files_changed >= 1

        # Both edits should be present
        final = (local / "README.md").read_text()
        assert "edited locally" in final
        assert "Appended by other device" in final

    def test_conflicting_edits_detected_cleanly(
        self, daemon: SyncDaemon, vault_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        """Conflicting edits to the same line should produce a conflict error,
        not get stuck on 'cannot pull with rebase: You have unstaged changes'.
        """
        local, bare = vault_with_remote

        # Simulate another device rewriting README.md line 1
        other = tmp_path / "other"
        _git(tmp_path, "clone", str(bare), str(other))
        _git(other, "config", "user.email", "other@test.com")
        _git(other, "config", "user.name", "Other")
        _git(other, "config", "core.hooksPath", "/dev/null")
        (other / "README.md").write_text("# Remote Vault\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "remote README rewrite")
        _git(other, "push", "origin", "main")

        # Local also rewrites the same line (conflicting)
        (local / "README.md").write_text("# Local Vault\n")

        with patch.object(daemon, "_is_obsidian_running", return_value=False):
            result = daemon.sync_cycle()

        # Should fail cleanly with a conflict error
        assert result.synced is False
        assert result.error is not None
        assert "conflict" in result.error.lower()
        assert result.stop_required is True

        # A restarted daemon must preserve the unresolved operation from the first cycle.
        before = _snapshot_git_state(local, bare)
        restarted = SyncDaemon(daemon.config)
        with patch.object(restarted, "_is_obsidian_running", return_value=False):
            second = restarted.run_once()
        assert second.synced is False
        assert second.intervention_required is True
        assert second.error is not None and "manual intervention" in second.error
        assert _snapshot_git_state(local, bare) == before

    def test_nonexistent_vault(self, tmp_path: Path) -> None:
        config = SyncConfig(
            vault_path=str(tmp_path / "nonexistent"),
            lock_path=str(tmp_path / "daemon.lock"),
        )
        d = SyncDaemon(config)
        with patch.object(d, "_is_obsidian_running", return_value=False):
            result = d.sync_cycle()
        assert result.synced is False
        assert result.error is not None


class TestRunOnce:
    def test_run_once(self, daemon: SyncDaemon, vault_with_remote: tuple[Path, Path]) -> None:
        local, _bare = vault_with_remote
        note = local / "daily.md"
        note.write_text("Today's note\n")
        with patch.object(daemon, "_is_obsidian_running", return_value=False):
            result = daemon.run_once()
        assert result.synced is True
        assert result.files_changed == 1
