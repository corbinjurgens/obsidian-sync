"""Smoke tests: full workflows through the CLI against real git repos.

These tests exercise the same sequences a user would run manually:
config init → sync → status → log. They use real git repos and verify
observable outcomes (commits in git log, files on disk, CLI output).

The only thing mocked is Obsidian process detection — we can't control
whether Obsidian is running on the CI/test machine.
"""

import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from obsidian_sync.cli import main

DEFAULT_BRANCH = "main"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def _git_log(repo: Path, n: int = 1) -> str:
    """Return the last n commit messages (subject + body)."""
    result = _git(repo, "log", f"-{n}", "--format=%B")
    return result.stdout


def _commit_count(repo: Path) -> int:
    result = _git(repo, "rev-list", "--count", "HEAD")
    return int(result.stdout.strip())


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


@pytest.fixture(autouse=True)
def _no_obsidian() -> object:
    """Ensure Obsidian detection always returns False in smoke tests."""
    with patch("obsidian_sync.daemon.SyncDaemon._is_obsidian_running", return_value=False):
        yield


@pytest.fixture
def vault(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Set up a vault (local clone + bare remote) and a config file.

    Returns (vault_path, remote_path, config_path).
    """
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-b", DEFAULT_BRANCH)

    setup = tmp_path / "setup"
    _git(tmp_path, "clone", str(bare), str(setup))
    _git(setup, "config", "user.email", "test@test.com")
    _git(setup, "config", "user.name", "Test User")
    (setup / "README.md").write_text("# Vault\n")
    _git(setup, "add", "-A")
    _git(setup, "commit", "-m", "Initial commit")
    _git(setup, "push", "origin", DEFAULT_BRANCH)

    local = tmp_path / "vault"
    _git(tmp_path, "clone", str(bare), str(local))
    _git(local, "config", "user.email", "test@test.com")
    _git(local, "config", "user.name", "Test User")

    config_path = tmp_path / "config.yaml"
    config_data = {
        "vault_path": str(local),
        "sync": {
            "interval_seconds": 300,
            "pull_strategy": "rebase",
            "remote": "origin",
            "branch": "main",
        },
        "commit": {
            "template": (
                "{{date}} — Auto-sync completed"
                " [host={{hostname}}] [files={{numFiles}}] [changed={{files}}]"
            ),
            "date_format": "%Y-%m-%d %H:%M:%S",
            "list_files_in_body": True,
        },
        "log": {
            "path": str(tmp_path / "sync.log"),
            "max_size_mb": 10,
        },
        "lock_path": str(tmp_path / "daemon.lock"),
    }
    config_path.write_text(yaml.dump(config_data, default_flow_style=False))

    return local, bare, config_path


def _cli(config_path: Path, *args: str) -> object:
    """Invoke the CLI with --config pointing at the test config."""
    runner = CliRunner()
    return runner.invoke(main, ["--config", str(config_path), *args])


# ---------------------------------------------------------------------------
# Scenario 1: Config init creates a usable config, show reads it back
# ---------------------------------------------------------------------------


class TestConfigRoundtrip:
    def test_init_then_show(self, tmp_path: Path) -> None:
        config_path = tmp_path / "fresh" / "config.yaml"
        runner = CliRunner()

        result = runner.invoke(
            main,
            ["--config", str(config_path), "config", "--init"],
            input="/tmp/my-vault\n60\n",
        )
        assert result.exit_code == 0

        # The file should exist and be valid YAML
        data = yaml.safe_load(config_path.read_text())
        assert data["vault_path"] == "/tmp/my-vault"
        assert data["sync"]["interval_seconds"] == 60

        # Show should display what we just wrote
        result = runner.invoke(main, ["--config", str(config_path), "config"])
        assert result.exit_code == 0
        assert "/tmp/my-vault" in result.output
        assert "60" in result.output


# ---------------------------------------------------------------------------
# Scenario 2: Sync a dirty vault — commit lands with correct message format
# ---------------------------------------------------------------------------


class TestSyncDirtyVault:
    def test_new_files_are_committed_and_pushed(self, vault: tuple[Path, Path, Path]) -> None:
        local, bare, config_path = vault

        # Create some notes
        (local / "daily.md").write_text("# Daily note\n")
        (local / "ideas.md").write_text("# Ideas\n")

        result = _cli(config_path, "sync")
        assert result.exit_code == 0
        assert "Synced 2 file(s)" in result.output

        # The commit message should match the template format
        log_output = _git_log(local)
        assert "Auto-sync completed" in log_output
        assert "files=2" in log_output
        assert "daily.md" in log_output
        assert "ideas.md" in log_output
        # Date should be in YYYY-MM-DD HH:MM:SS format
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", log_output)

    def test_commit_message_includes_hostname(self, vault: tuple[Path, Path, Path]) -> None:
        local, _bare, config_path = vault
        (local / "note.md").write_text("test\n")

        _cli(config_path, "sync")

        log_output = _git_log(local)
        assert "host=" in log_output

    def test_commit_body_lists_changed_files(self, vault: tuple[Path, Path, Path]) -> None:
        local, _bare, config_path = vault
        (local / "one.md").write_text("1\n")
        (local / "two.md").write_text("2\n")

        _cli(config_path, "sync")

        log_output = _git_log(local)
        # Body should list full paths (not just basenames)
        assert "one.md" in log_output
        assert "two.md" in log_output

    def test_changes_reach_the_remote(self, vault: tuple[Path, Path, Path], tmp_path: Path) -> None:
        local, bare, config_path = vault
        (local / "pushed.md").write_text("content\n")

        _cli(config_path, "sync")

        # Clone the bare remote from scratch and verify the file arrived
        verify = tmp_path / "verify"
        _git(tmp_path, "clone", str(bare), str(verify))
        assert (verify / "pushed.md").exists()
        assert (verify / "pushed.md").read_text() == "content\n"


# ---------------------------------------------------------------------------
# Scenario 3: Sync a clean vault — no new commits, no errors
# ---------------------------------------------------------------------------


class TestSyncCleanVault:
    def test_clean_vault_reports_nothing_to_sync(self, vault: tuple[Path, Path, Path]) -> None:
        _local, _bare, config_path = vault

        result = _cli(config_path, "sync")
        assert result.exit_code == 0
        assert "nothing to sync" in result.output.lower()

    def test_clean_vault_creates_no_new_commits(self, vault: tuple[Path, Path, Path]) -> None:
        local, _bare, config_path = vault
        count_before = _commit_count(local)

        _cli(config_path, "sync")

        assert _commit_count(local) == count_before


# ---------------------------------------------------------------------------
# Scenario 4: Sync defers when Obsidian is running
# ---------------------------------------------------------------------------


class TestObsidianDeferral:
    def test_sync_skips_when_obsidian_detected(self, vault: tuple[Path, Path, Path]) -> None:
        local, _bare, config_path = vault
        (local / "note.md").write_text("should not be committed\n")

        # Override the autouse fixture for this specific test
        with patch("obsidian_sync.daemon.SyncDaemon._is_obsidian_running", return_value=True):
            result = _cli(config_path, "sync")

        assert result.exit_code == 0
        assert "Deferring" in result.output

        # The file should still be uncommitted
        status = _git(local, "status", "--porcelain")
        assert "note.md" in status.stdout


# ---------------------------------------------------------------------------
# Scenario 5: Double sync is idempotent — second run is a no-op
# ---------------------------------------------------------------------------


class TestSyncIdempotency:
    @pytest.mark.parametrize("fetch_style", ["wildcard", "narrow"])
    @pytest.mark.parametrize("strategy", ["rebase", "merge", "ff-only"])
    def test_clean_retry_pushes_commit_after_failed_push(
        self, vault: tuple[Path, Path, Path], tmp_path: Path, strategy: str, fetch_style: str
    ) -> None:
        local, bare, config_path = vault
        _git(local, "remote", "rename", "origin", "backup")
        _git(local, "branch", "-m", "notes")
        _git(local, "push", "--set-upstream", "backup", "notes")
        if fetch_style == "narrow":
            _git(
                local, "config", "remote.backup.fetch", "+refs/heads/main:refs/remotes/backup/main"
            )
            _git(local, "update-ref", "-d", "refs/remotes/backup/notes")
        config = yaml.safe_load(config_path.read_text())
        config["sync"].update(pull_strategy=strategy, remote="backup", branch="notes")
        config_path.write_text(yaml.safe_dump(config))
        initial_tip = _git(bare, "rev-parse", "refs/heads/notes").stdout.strip()
        _git(local, "config", "remote.backup.pushurl", str(tmp_path / "missing.git"))
        (local / "queued.md").write_text("Keep this note across a failed push\n")

        failed = _cli(config_path, "sync")
        assert failed.exit_code == 1, failed.output
        assert "Push failed" in failed.output
        unsent_tip = _git(local, "rev-parse", "HEAD").stdout.strip()
        assert unsent_tip != initial_tip
        assert _git(local, "status", "--porcelain").stdout == ""
        assert _git(bare, "rev-parse", "refs/heads/notes").stdout.strip() == initial_tip
        assert _git(local, "show", "HEAD:queued.md").stdout == (
            "Keep this note across a failed push\n"
        )

        _git(local, "config", "--unset", "remote.backup.pushurl")
        retried = _cli(config_path, "sync")
        assert retried.exit_code == 0, retried.output
        assert _git(local, "rev-parse", "HEAD").stdout.strip() == unsent_tip
        assert _git(bare, "rev-parse", "refs/heads/notes").stdout.strip() == unsent_tip
        if fetch_style == "wildcard":
            assert _git(local, "rev-parse", "backup/notes").stdout.strip() == unsent_tip
        else:
            assert "backup/notes" not in _git(local, "branch", "-r").stdout
        assert _git(bare, "rev-parse", "refs/heads/main").stdout.strip() == initial_tip
        assert _git(bare, "show", "notes:queued.md").stdout == (
            "Keep this note across a failed push\n"
        )

    def test_second_sync_is_noop(self, vault: tuple[Path, Path, Path]) -> None:
        local, _bare, config_path = vault
        (local / "note.md").write_text("content\n")

        first = _cli(config_path, "sync")
        assert first.exit_code == 0
        assert "Synced" in first.output
        count_after_first = _commit_count(local)

        second = _cli(config_path, "sync")
        assert second.exit_code == 0
        assert "nothing to sync" in second.output.lower()
        assert _commit_count(local) == count_after_first


# ---------------------------------------------------------------------------
# Scenario 6: Status reports correctly when daemon is not running
# ---------------------------------------------------------------------------


class TestStatusWhenStopped:
    def test_shows_stopped(self, vault: tuple[Path, Path, Path]) -> None:
        _local, _bare, config_path = vault

        result = _cli(config_path, "status")
        assert result.exit_code == 0
        assert "Stopped" in result.output

    def test_shows_vault_row(self, vault: tuple[Path, Path, Path]) -> None:
        _local, _bare, config_path = vault

        result = _cli(config_path, "status")
        # Status table should include a Vault row (Rich may truncate long paths)
        assert "Vault" in result.output

    def test_shows_sync_settings(self, vault: tuple[Path, Path, Path]) -> None:
        _local, _bare, config_path = vault

        result = _cli(config_path, "status")
        assert "300s" in result.output
        assert "rebase" in result.output
        assert "origin/main" in result.output


# ---------------------------------------------------------------------------
# Scenario 7: Log reflects what actually happened during sync
# ---------------------------------------------------------------------------


class TestLogAfterSync:
    def test_log_contains_sync_entry(self, vault: tuple[Path, Path, Path]) -> None:
        local, _bare, config_path = vault
        (local / "note.md").write_text("content\n")

        _cli(config_path, "sync")

        result = _cli(config_path, "log")
        assert result.exit_code == 0
        # Log should mention the commit or sync activity
        assert "Synced" in result.output or "Committed" in result.output

    def test_log_missing_before_first_sync(self, vault: tuple[Path, Path, Path]) -> None:
        _local, _bare, config_path = vault

        # Log file doesn't exist yet — should error clearly
        result = _cli(config_path, "log")
        assert result.exit_code != 0
        assert "Log file not found" in result.output

    def test_log_after_clean_sync(self, vault: tuple[Path, Path, Path]) -> None:
        _local, _bare, config_path = vault

        # Run sync to create the log file (clean vault = nothing to sync)
        _cli(config_path, "sync")

        result = _cli(config_path, "log")
        assert result.exit_code == 0
        assert "clean" in result.output.lower()


# ---------------------------------------------------------------------------
# Scenario 8: Multiple files with varied paths get correct commit messages
# ---------------------------------------------------------------------------


class TestCommitMessageFormatting:
    def test_nested_files_show_basenames_in_subject(self, vault: tuple[Path, Path, Path]) -> None:
        local, _bare, config_path = vault
        (local / "journal").mkdir()
        (local / "journal" / "2026-02-10.md").write_text("Today\n")
        (local / "notes").mkdir()
        (local / "notes" / "ideas.md").write_text("Ideas\n")

        _cli(config_path, "sync")

        log_output = _git_log(local)
        # The changed= field should have basenames
        assert "2026-02-10.md" in log_output
        assert "ideas.md" in log_output

    def test_many_files_are_truncated_in_subject(self, vault: tuple[Path, Path, Path]) -> None:
        local, _bare, config_path = vault
        for i in range(8):
            (local / f"note-{i}.md").write_text(f"Note {i}\n")

        _cli(config_path, "sync")

        log_output = _git_log(local)
        assert "files=8" in log_output
        assert "..." in log_output

    def test_single_file_no_truncation(self, vault: tuple[Path, Path, Path]) -> None:
        local, _bare, config_path = vault
        (local / "only.md").write_text("solo\n")

        _cli(config_path, "sync")

        log_output = _git_log(local)
        assert "files=1" in log_output
        assert "only.md" in log_output
        assert "..." not in log_output


# ---------------------------------------------------------------------------
# Scenario 9: Sync pulls remote changes before committing
# ---------------------------------------------------------------------------


class TestPullBeforePush:
    def test_remote_changes_are_pulled_before_local_commit(
        self, vault: tuple[Path, Path, Path], tmp_path: Path
    ) -> None:
        local, bare, config_path = vault

        # Simulate another device pushing a change
        other = tmp_path / "other-device"
        _git(tmp_path, "clone", str(bare), str(other))
        _git(other, "config", "user.email", "other@test.com")
        _git(other, "config", "user.name", "Other Device")
        (other / "from-other.md").write_text("from laptop\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", "Sync from laptop")
        _git(other, "push", "origin", "main")

        # Now local has a new file too
        (local / "from-local.md").write_text("from desktop\n")

        result = _cli(config_path, "sync")
        assert result.exit_code == 0

        # Both files should be present locally
        assert (local / "from-other.md").exists()
        assert (local / "from-local.md").exists()

        # Both should be on the remote
        verify = tmp_path / "verify"
        _git(tmp_path, "clone", str(bare), str(verify))
        assert (verify / "from-other.md").exists()
        assert (verify / "from-local.md").exists()
