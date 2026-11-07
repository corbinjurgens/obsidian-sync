"""Tests for obsidian_sync.config module."""

from dataclasses import fields
from pathlib import Path

import pytest
import yaml

from obsidian_sync.config import (
    CommitSettings,
    LogSettings,
    SyncConfig,
    SyncSettings,
    default_vault_path,
    expand_paths,
    load_config,
    save_config,
)


class TestDefaultConfigValues:
    def test_sync_defaults(self):
        config = SyncConfig()
        assert config.sync.interval_seconds == 60
        assert config.sync.pull_strategy == "rebase"
        assert config.sync.remote == "origin"
        assert config.sync.branch == "main"

    def test_commit_defaults(self):
        config = SyncConfig()
        assert "{{date}}" in config.commit.template
        assert "{{hostname}}" in config.commit.template
        assert "{{numFiles}}" in config.commit.template
        assert "{{files}}" in config.commit.template
        assert config.commit.date_format == "%Y-%m-%d %H:%M:%S"
        assert config.commit.list_files_in_body is True

    def test_log_defaults(self):
        config = SyncConfig()
        assert config.log.path == "~/.local/state/obsidian-sync/sync.log"
        assert config.log.max_size_mb == 10

    def test_vault_and_lock_defaults(self):
        config = SyncConfig()
        assert config.vault_path == default_vault_path()
        assert config.lock_path == "~/.local/state/obsidian-sync/daemon.lock"

    def test_vault_default_uses_factory(self):
        vault_field = next(field for field in fields(SyncConfig) if field.name == "vault_path")
        assert vault_field.default_factory is default_vault_path

    def test_vault_default_honors_repo_root_env(self, monkeypatch):
        monkeypatch.setenv("REPOS_ROOT", "~/src")
        assert default_vault_path() == str(Path.home() / "src" / "obsidian-vault")


class TestLoadConfig:
    def test_load_config_no_file(self, tmp_path):
        nonexistent = tmp_path / "nonexistent" / "config.yaml"
        config = load_config(nonexistent)
        assert config == SyncConfig()

    def test_load_config_from_file(self, sample_config_yaml):
        config = load_config(sample_config_yaml)
        assert config.vault_path == "/tmp/test-vault"
        assert config.sync.interval_seconds == 600
        assert config.sync.pull_strategy == "merge"
        assert config.sync.remote == "upstream"
        assert config.sync.branch == "develop"
        assert config.commit.template == "{{date}} — sync [{{numFiles}} files]"
        assert config.commit.date_format == "%Y-%m-%d"
        assert config.commit.list_files_in_body is False
        assert config.log.path == "/tmp/test-sync.log"
        assert config.log.max_size_mb == 5
        assert config.lock_path == "/tmp/test-daemon.lock"

    def test_load_config_partial_override(self, tmp_config_dir):
        partial_data = {
            "vault_path": "/custom/vault",
            "sync": {"interval_seconds": 120},
        }
        config_path = tmp_config_dir / "partial.yaml"
        config_path.write_text(yaml.dump(partial_data))
        config = load_config(config_path)
        assert config.vault_path == "/custom/vault"
        assert config.sync.interval_seconds == 120
        # Unspecified fields retain defaults
        assert config.sync.pull_strategy == "rebase"
        assert config.sync.remote == "origin"
        assert config.commit.date_format == "%Y-%m-%d %H:%M:%S"
        assert config.log.max_size_mb == 10


class TestExpandPaths:
    def test_expand_paths_home(self):
        config = SyncConfig()
        expanded = expand_paths(config)
        home = str(Path.home())
        assert expanded.vault_path.startswith(home)
        assert expanded.lock_path.startswith(home)
        assert expanded.log.path.startswith(home)
        # Verify tilde is gone
        assert "~" not in expanded.vault_path
        assert "~" not in expanded.lock_path
        assert "~" not in expanded.log.path

    def test_expand_paths_absolute_unchanged(self, tmp_path):
        config = SyncConfig(
            vault_path=str(tmp_path / "vault"),
            lock_path=str(tmp_path / "lock"),
            log=LogSettings(path=str(tmp_path / "log")),
        )
        expanded = expand_paths(config)
        assert expanded.vault_path == config.vault_path
        assert expanded.lock_path == config.lock_path
        assert expanded.log.path == config.log.path


class TestSaveConfig:
    def test_save_config_creates_file(self, tmp_path):
        config = SyncConfig(vault_path="/test/vault")
        config_path = tmp_path / "saved_config.yaml"
        save_config(config, config_path)
        assert config_path.exists()
        reloaded = load_config(config_path)
        assert reloaded.vault_path == "/test/vault"
        assert reloaded.sync == SyncSettings()
        assert reloaded.commit == CommitSettings()

    def test_save_config_creates_dirs(self, tmp_path):
        config = SyncConfig()
        nested_path = tmp_path / "deep" / "nested" / "config.yaml"
        save_config(config, nested_path)
        assert nested_path.exists()
        reloaded = load_config(nested_path)
        assert reloaded == SyncConfig()


class TestBackoffSettings:
    def _load(self, tmp_path, sync_text):
        path = tmp_path / "config.yaml"
        path.write_text(f"vault_path: /vault\nsync:\n{sync_text}", encoding="utf-8")
        return load_config(path, required=True)

    def test_defaults_preserve_interval_first_retry_and_fifteen_minute_cap(self):
        settings = SyncConfig().sync
        assert settings.backoff_initial_seconds == 0
        assert settings.backoff_multiplier == 2.0
        assert settings.backoff_max_seconds == 900

    def test_integer_multiplier_is_accepted_by_installed_validation(self, tmp_path):
        assert self._load(tmp_path, "  backoff_multiplier: 3\n").sync.backoff_multiplier == 3

    @pytest.mark.parametrize(
        "sync_text,message",
        [
            ("  backoff_initial_seconds: -1\n", "backoff_initial_seconds"),
            ("  backoff_multiplier: 0.5\n", "backoff_multiplier"),
            ("  backoff_max_seconds: 0\n", "backoff_max_seconds"),
            ("  backoff_multiplier: fast\n", "backoff_multiplier"),
            ("  backoff_max_seconds: 1.5\n", "backoff_max_seconds"),
        ],
    )
    def test_invalid_values_are_rejected_for_installed_startup(self, tmp_path, sync_text, message):
        with pytest.raises(ValueError, match=message):
            self._load(tmp_path, sync_text)
