"""Tests for the obsidian-sync daemon module."""

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from obsidian_sync.config import SyncConfig
from obsidian_sync.daemon import LockError, SyncDaemon, _is_pid_alive

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
