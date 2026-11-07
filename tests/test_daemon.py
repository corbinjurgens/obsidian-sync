"""Tests for the obsidian-sync daemon module."""

import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from obsidian_sync.cli import main
from obsidian_sync.config import SyncConfig
from obsidian_sync.daemon import LockError, SyncDaemon, SyncResult, _is_pid_alive
from obsidian_sync.git_ops import GitError, _run_git, push

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
                assert result.deferred is False
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
                assert result.deferred is False
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
                assert result.deferred is False
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
        assert result.deferred is False
        assert result.synced is False
        assert result.error is not None and "manual intervention" in result.error
        stage.assert_not_called()
        commit.assert_not_called()
        pull.assert_not_called()
        push.assert_not_called()
        assert _snapshot_git_state(local, bare) == before

    def test_loop_retries_after_inspection_failure(self, daemon: SyncDaemon) -> None:
        daemon.config.sync.interval_seconds = 30
        result = SyncResult(
            False,
            0,
            "Inspection failed",
            error="Inspection failed",
            intervention_required=True,
        )
        delays: list[int] = []

        def wait(seconds: int) -> None:
            delays.append(seconds)
            if len(delays) == 2:
                daemon._running = False

        with (
            patch.object(daemon, "_setup_signals"),
            patch.object(daemon, "_acquire_lock"),
            patch.object(daemon, "_release_lock") as release,
            patch.object(daemon, "_stop_requested", return_value=False),
            patch.object(daemon, "sync_cycle", return_value=result) as sync,
            patch.object(daemon, "_sleep", side_effect=wait),
        ):
            daemon.run()
        assert sync.call_count == 2
        assert delays == [30, 60]
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
        assert result.intervention_required is True

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


class TestRetryBackoff:
    @pytest.mark.parametrize(
        "interval,outcomes,expected,backoff",
        [
            (
                30,
                "ffffffffsff",
                [30, 60, 120, 240, 480, 900, 900, 900, 30, 30, 60],
                {},
            ),
            (1000, "fffsf", [1000, 1000, 1000, 1000, 1000], {}),
            (30, "fffddfsf", [30, 60, 120, 30, 30, 240, 30, 30], {}),
            (
                60,
                "ffffsf",
                [10, 30, 90, 120, 60, 10],
                {
                    "backoff_initial_seconds": 10,
                    "backoff_multiplier": 3,
                    "backoff_max_seconds": 120,
                },
            ),
            (60, "ffff", [60, 90, 135, 200], {"backoff_multiplier": 1.5, "backoff_max_seconds": 200}),
            (60, "fff", [60, 60, 60], {"backoff_multiplier": 1.0}),
            (60, "ff", [60, 60], {"backoff_initial_seconds": 500, "backoff_max_seconds": 30}),
            (60, "ffff", [1, 2, 3, 5], {"backoff_initial_seconds": 1, "backoff_multiplier": 1.5}),
            (60, "fff", [60, 900, 900], {"backoff_multiplier": 1.7e308}),
        ],
    )
    def test_delays_cap_reset_and_preserve_history_during_deferral(
        self,
        tmp_path: Path,
        interval: int,
        outcomes: str,
        expected: list[int],
        backoff: dict[str, float],
    ) -> None:
        daemon = SyncDaemon(
            SyncConfig(vault_path=str(tmp_path), lock_path=str(tmp_path / "daemon.lock"))
        )
        daemon.config.sync.interval_seconds = interval
        for name, value in backoff.items():
            setattr(daemon.config.sync, name, value)
        results = {
            "f": SyncResult(False, 0, "Git failed", error="Git failed"),
            "s": SyncResult(True, 0, "Clean"),
            "d": SyncResult(False, 0, "Obsidian running", deferred=True),
        }
        delays: list[int] = []

        def wait(seconds: int) -> None:
            assert Path(daemon.config.lock_path).exists()
            delays.append(seconds)
            if len(delays) == len(outcomes):
                daemon._running = False

        with (
            patch.object(daemon, "_setup_signals"),
            patch.object(daemon, "_stop_requested", return_value=False),
            patch.object(
                daemon, "sync_cycle", side_effect=[results[key] for key in outcomes]
            ) as sync,
            patch.object(daemon, "_sleep", side_effect=wait),
        ):
            daemon.run()
        assert delays == expected
        assert sync.call_count == len(outcomes)
        assert not Path(daemon.config.lock_path).exists()
        assert daemon._running is False

    def test_signal_stops_during_backoff(self, tmp_path: Path) -> None:
        daemon = SyncDaemon(
            SyncConfig(vault_path=str(tmp_path), lock_path=str(tmp_path / "daemon.lock"))
        )
        daemon.config.sync.interval_seconds = 900
        elapsed = 0.0

        def wait(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds
            daemon._handle_signal(signal.SIGTERM, None)

        with (
            patch.object(daemon, "_setup_signals"),
            patch.object(daemon, "_stop_requested", return_value=False),
            patch.object(
                daemon, "sync_cycle", return_value=SyncResult(False, 0, "Failed", error="Failed")
            ) as sync,
            patch("obsidian_sync.daemon.time.monotonic", side_effect=lambda: elapsed),
            patch("obsidian_sync.daemon.time.sleep", side_effect=wait),
        ):
            daemon.run()
        assert elapsed == 1.0
        sync.assert_called_once()
        assert not Path(daemon.config.lock_path).exists()

    def test_cooperative_request_stops_during_backoff(self, tmp_path: Path) -> None:
        daemon = SyncDaemon(
            SyncConfig(vault_path=str(tmp_path), lock_path=str(tmp_path / "daemon.lock"))
        )
        daemon.config.sync.interval_seconds = 900
        elapsed = 0.0

        def wait(seconds: float) -> None:
            nonlocal elapsed
            elapsed += seconds
            assert daemon.stop() is True

        with (
            patch.object(daemon, "_setup_signals"),
            patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
            patch.object(
                daemon, "sync_cycle", return_value=SyncResult(False, 0, "Failed", error="Failed")
            ) as sync,
            patch("obsidian_sync.daemon.time.monotonic", side_effect=lambda: elapsed),
            patch("obsidian_sync.daemon.time.sleep", side_effect=wait),
        ):
            daemon.run()
        assert elapsed == 1.0
        sync.assert_called_once()
        assert not Path(daemon.config.lock_path).exists()
        assert not daemon._stop_path.exists()

    def test_stop_during_active_git_finishes_cycle(
        self, daemon: SyncDaemon, vault_with_remote: tuple[Path, Path]
    ) -> None:
        local, bare = vault_with_remote
        (local / "note.md").write_text("Keep active Git work\n")

        def finish_push(repo: Path, remote: str, branch: str) -> object:
            daemon._handle_signal(signal.SIGTERM, None)
            assert Path(daemon.config.lock_path).exists()
            return push(repo, remote, branch)

        with (
            patch.object(daemon, "_setup_signals"),
            patch.object(daemon, "_stop_requested", return_value=False),
            patch.object(daemon, "_is_obsidian_running", return_value=False),
            patch("obsidian_sync.daemon.push", side_effect=finish_push) as pushed,
        ):
            daemon.run()
        pushed.assert_called_once()
        assert _git(bare, "show", "main:note.md").stdout == "Keep active Git work\n"
        assert _git(local, "rev-parse", "HEAD").stdout == _git(bare, "rev-parse", "main").stdout
        assert not Path(daemon.config.lock_path).exists()


class TestFailureRecovery:
    @pytest.mark.parametrize(
        "operation", ["add", "commit", "pull", "push", "status", "changed", "ahead"]
    )
    def test_transient_failure_retries_without_restart(
        self, daemon: SyncDaemon, vault_with_remote: tuple[Path, Path], operation: str
    ) -> None:
        local, bare = vault_with_remote
        initial_head = _git(local, "rev-parse", "HEAD").stdout
        (local / "note.md").write_text("Retained through failure\n")
        if operation == "ahead":
            _git(local, "add", "note.md")
            _git(local, "commit", "-m", "Outstanding note")
        fail = True
        delays: list[int] = []
        results: list[SyncResult] = []
        daemon.config.sync.interval_seconds = 30
        sync_cycle = daemon.sync_cycle

        def cycle() -> SyncResult:
            result = sync_cycle()
            results.append(result)
            return result

        def git(repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
            target = {
                "status": ["status", "--porcelain"],
                "changed": ["status", "--porcelain", "-u"],
                "ahead": ["rev-list", "FETCH_HEAD..HEAD", "--count"],
            }.get(operation)
            if fail and (args == target if target is not None else args[0] == operation):
                return subprocess.CompletedProcess(args, 128, "", "Injected transient Git failure")
            return _run_git(repo, args)

        def wait(seconds: int) -> None:
            nonlocal fail
            delays.append(seconds)
            assert daemon._running is True
            assert Path(daemon.config.lock_path).exists()
            if fail:
                assert results[-1].synced is False
                assert results[-1].error is not None
                assert "Injected transient Git failure" in results[-1].error
                assert _git(bare, "rev-parse", "main").stdout == initial_head
                assert (local / "note.md").read_text() == "Retained through failure\n"
                assert not _git(local, "ls-files", "--unmerged").stdout
                staged = _git(local, "diff", "--cached", "--name-only").stdout
                assert staged == ("note.md\n" if operation == "commit" else "")
                head = _git(local, "rev-parse", "HEAD").stdout
                if operation in {"pull", "push", "ahead"}:
                    assert head != initial_head
                else:
                    assert head == initial_head
                fail = False
            else:
                assert results[-1].synced is True
                assert results[-1].error is None
                assert _git(local, "status", "--porcelain").stdout == ""
                assert _git(bare, "show", "main:note.md").stdout == "Retained through failure\n"
                assert (
                    _git(local, "rev-parse", "HEAD").stdout
                    == _git(bare, "rev-parse", "main").stdout
                )
                daemon._running = False

        with (
            patch.object(daemon, "_setup_signals"),
            patch.object(daemon, "_stop_requested", return_value=False),
            patch.object(daemon, "_is_obsidian_running", return_value=False),
            patch.object(daemon, "sync_cycle", side_effect=cycle),
            patch("obsidian_sync.git_ops._run_git", side_effect=git),
            patch.object(daemon, "_sleep", side_effect=wait),
        ):
            daemon.run()
        assert delays == [30, 30]
        assert not Path(daemon.config.lock_path).exists()

    def test_conflict_refuses_mutation_then_resumes_after_manual_resolution(
        self, daemon: SyncDaemon, vault_with_remote: tuple[Path, Path], tmp_path: Path
    ) -> None:
        local, bare = vault_with_remote
        other = tmp_path / "other"
        _git(tmp_path, "clone", str(bare), str(other))
        _git(other, "config", "user.email", "other@test.com")
        _git(other, "config", "user.name", "Other")
        (other / "README.md").write_text("# Remote Vault\n")
        _git(other, "add", "README.md")
        _git(other, "commit", "-m", "Remote change")
        _git(other, "push", "origin", "main")
        remote_head = _git(bare, "rev-parse", "main").stdout
        (local / "README.md").write_text("# Local Vault\n")
        daemon.config.sync.interval_seconds = 30
        delays: list[int] = []
        unresolved: dict[str, object] = {}
        results: list[SyncResult] = []
        sync_cycle = daemon.sync_cycle

        def cycle() -> SyncResult:
            result = sync_cycle()
            results.append(result)
            return result

        def wait(seconds: int) -> None:
            nonlocal unresolved
            delays.append(seconds)
            assert daemon._running is True
            if len(delays) == 1:
                assert results[-1].synced is False
                assert results[-1].intervention_required is True
                assert results[-1].error is not None and "Pull conflict" in results[-1].error
                assert _git(local, "ls-files", "--unmerged").stdout
                assert _git(bare, "rev-parse", "main").stdout == remote_head
                unresolved = _snapshot_git_state(local, bare)
            elif len(delays) == 2:
                assert results[-1].synced is False
                assert results[-1].intervention_required is True
                assert results[-1].error is not None and "manual intervention" in results[-1].error
                assert _snapshot_git_state(local, bare) == unresolved
                (local / "README.md").write_text("# Manually resolved Vault\n")
                _git(local, "add", "README.md")
                _git(local, "-c", "core.editor=true", "rebase", "--continue")
            else:
                assert results[-1].synced is True
                assert results[-1].error is None
                assert _git(bare, "show", "main:README.md").stdout == "# Manually resolved Vault\n"
                assert (
                    _git(local, "rev-parse", "HEAD").stdout
                    == _git(bare, "rev-parse", "main").stdout
                )
                daemon._running = False

        with (
            patch.object(daemon, "_setup_signals"),
            patch.object(daemon, "_stop_requested", return_value=False),
            patch.object(daemon, "_is_obsidian_running", return_value=False),
            patch.object(daemon, "sync_cycle", side_effect=cycle),
            patch.object(daemon, "_sleep", side_effect=wait),
        ):
            daemon.run()
        assert delays == [30, 60, 30]
        assert not Path(daemon.config.lock_path).exists()

    @pytest.mark.parametrize(
        "operation",
        ["add_all", "commit", "pull", "push", "is_dirty", "get_changed_files", "is_ahead"],
    )
    def test_foreground_git_failure_exits_nonzero(
        self, daemon_config: SyncConfig, operation: str
    ) -> None:
        (Path(daemon_config.vault_path) / "note.md").write_text("Foreground failure\n")
        with (
            patch("obsidian_sync.cli._load_and_expand", return_value=daemon_config),
            patch("obsidian_sync.cli.setup_logging"),
            patch.object(SyncDaemon, "_is_obsidian_running", return_value=False),
            patch("obsidian_sync.daemon.is_dirty", return_value=operation != "is_ahead"),
            patch(f"obsidian_sync.daemon.{operation}", side_effect=GitError("Injected failure")),
        ):
            result = CliRunner().invoke(main, ["sync"])
        assert result.exit_code == 1
        assert "Injected failure" in result.output



class TestUnixObsidianDetection:
    @pytest.fixture
    def daemon(self, tmp_path: Path) -> SyncDaemon:
        return SyncDaemon(
            SyncConfig(vault_path=str(tmp_path), lock_path=str(tmp_path / "daemon.lock"))
        )

    @pytest.mark.parametrize("system", ["Darwin", "Linux"])
    @pytest.mark.parametrize(
        "outcome,expected",
        [
            (subprocess.CompletedProcess([], 0, "123\n", ""), True),
            (subprocess.CompletedProcess([], 1, "", ""), False),
            (subprocess.CompletedProcess([], 2, "", "syntax"), True),
            (FileNotFoundError("pgrep"), True),
            (subprocess.TimeoutExpired(["pgrep"], 10), True),
        ],
    )
    def test_detection_defers_whenever_pgrep_cannot_answer(
        self, daemon: SyncDaemon, system: str, outcome: object, expected: bool
    ) -> None:
        with (
            patch("obsidian_sync.daemon.platform.system", return_value=system),
            patch("obsidian_sync.daemon.subprocess.run", side_effect=[outcome])
            if isinstance(outcome, BaseException)
            else patch("obsidian_sync.daemon.subprocess.run", return_value=outcome),
        ):
            assert daemon._is_obsidian_running() is expected
