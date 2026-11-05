"""Tests for the obsidian-sync CLI using click.testing.CliRunner."""

from pathlib import Path

import yaml
from click.testing import CliRunner

from obsidian_sync.cli import main
from obsidian_sync.config import SyncConfig, save_config


@staticmethod
def _make_config_file(tmp_path: Path, vault_path: str = "/tmp/vault") -> Path:
    """Create a config file in tmp_path and return its path."""
    config_path = tmp_path / "config.yaml"
    cfg = SyncConfig(
        vault_path=vault_path,
        lock_path=str(tmp_path / "daemon.lock"),
    )
    cfg.log.path = str(tmp_path / "sync.log")
    save_config(cfg, config_path)
    return config_path


class TestMainGroup:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Git sync daemon for Obsidian vaults" in result.output

    def test_subcommands_listed(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert "start" in result.output
        assert "stop" in result.output
        assert "status" in result.output
        assert "sync" in result.output
        assert "log" in result.output
        assert "config" in result.output
        assert "install" in result.output
        assert "uninstall" in result.output
        assert "enable" in result.output
        assert "disable" in result.output


class TestStartCommand:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["start", "--help"])
        assert result.exit_code == 0
        assert "Start the sync loop" in result.output
        assert "--daemon" in result.output


class TestStopCommand:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["stop", "--help"])
        assert result.exit_code == 0
        assert "Stop a running daemon" in result.output

    def test_no_daemon_running(self, tmp_path: Path) -> None:
        config_path = _make_config_file(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["--config", str(config_path), "stop"])
        assert result.exit_code != 0
        assert "No daemon is running" in result.output


class TestStatusCommand:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", "--help"])
        assert result.exit_code == 0

    def test_status_not_running(self, tmp_path: Path) -> None:
        config_path = _make_config_file(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["--config", str(config_path), "status"])
        assert result.exit_code == 0
        assert "Stopped" in result.output

    def test_status_running_requires_live_pid(self, tmp_path: Path, monkeypatch) -> None:
        config_path = _make_config_file(tmp_path)
        lock_path = tmp_path / "daemon.lock"
        lock_path.write_text("12345")
        runner = CliRunner()
        monkeypatch.setattr("obsidian_sync.cli._is_pid_alive", lambda pid: pid == 12345)

        result = runner.invoke(main, ["--config", str(config_path), "status"])

        assert result.exit_code == 0
        assert "Running" in result.output
        assert "12345" in result.output

    def test_status_reports_stale_lock(self, tmp_path: Path, monkeypatch) -> None:
        config_path = _make_config_file(tmp_path)
        lock_path = tmp_path / "daemon.lock"
        lock_path.write_text("12345")
        runner = CliRunner()
        monkeypatch.setattr("obsidian_sync.cli._is_pid_alive", lambda pid: False)

        result = runner.invoke(main, ["--config", str(config_path), "status"])

        assert result.exit_code == 0
        assert "Stale lock" in result.output
        assert "12345" in result.output


class TestSyncCommand:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["sync", "--help"])
        assert result.exit_code == 0
        assert "Run one sync cycle" in result.output


class TestLogCommand:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["log", "--help"])
        assert result.exit_code == 0

    def test_log_no_file(self, tmp_path: Path) -> None:
        config_path = _make_config_file(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["--config", str(config_path), "log"])
        assert result.exit_code != 0
        assert "Log file not found" in result.output

    def test_tail_follows_without_external_commands(self, tmp_path: Path, monkeypatch) -> None:
        config_path = _make_config_file(tmp_path)
        log_path = tmp_path / "sync.log"
        log_path.write_text("old entry\nlatest entry\n", encoding="utf-8")
        waits = []

        def wait(seconds):
            if waits:
                raise KeyboardInterrupt
            waits.append(seconds)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write("new entry\n")

        monkeypatch.setattr("obsidian_sync.cli.time.sleep", wait)
        result = CliRunner().invoke(
            main, ["--config", str(config_path), "log", "--tail", "-n", "1"]
        )
        assert "latest entry" in result.output
        assert "new entry" in result.output
        assert "old entry" not in result.output


class TestConfigCommand:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["config", "--help"])
        assert result.exit_code == 0

    def test_config_init(self, tmp_path: Path) -> None:
        config_path = tmp_path / "new_config.yaml"
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--config", str(config_path), "config", "--init"],
            input="~/my-vault\n300\n",
        )
        assert result.exit_code == 0
        assert config_path.exists()
        data = yaml.safe_load(config_path.read_text())
        assert data["vault_path"] == "~/my-vault"

    def test_config_init_already_exists(self, tmp_path: Path) -> None:
        config_path = _make_config_file(tmp_path)
        runner = CliRunner()
        result = runner.invoke(main, ["--config", str(config_path), "config", "--init"])
        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_config_init_uses_repo_root_default(self, tmp_path: Path, monkeypatch) -> None:
        config_path = tmp_path / "default_config.yaml"
        runner = CliRunner()
        monkeypatch.setenv("REPOS_ROOT", str(tmp_path / "repos"))
        result = runner.invoke(
            main,
            ["--config", str(config_path), "config", "--init"],
            input="\n300\n",
        )
        assert result.exit_code == 0
        data = yaml.safe_load(config_path.read_text())
        assert str(tmp_path / "repos" / "obsidian-vault") in data["vault_path"]

    def test_config_show(self, tmp_path: Path) -> None:
        config_path = _make_config_file(tmp_path, vault_path="/my/vault")
        runner = CliRunner()
        result = runner.invoke(main, ["--config", str(config_path), "config"])
        assert result.exit_code == 0
        assert "/my/vault" in result.output


class TestInstallCommand:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["install", "--help"])
        assert result.exit_code == 0

    def test_uninstall_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["uninstall", "--help"])
        assert result.exit_code == 0

    def test_enable_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["enable", "--help"])
        assert result.exit_code == 0

    def test_disable_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["disable", "--help"])
        assert result.exit_code == 0
