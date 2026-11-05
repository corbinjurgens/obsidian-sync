"""Opt-in Task Scheduler smoke test using a disposable task and configuration."""

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from obsidian_sync import windows_service
from obsidian_sync.config import SyncConfig, save_config
from obsidian_sync.daemon import _is_pid_alive

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("OBSIDIAN_SYNC_TEST_TASKS") != "1",
    reason="Set OBSIDIAN_SYNC_TEST_TASKS=1 on Windows to test a disposable login task",
)


def test_task_scheduler_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_service, "TASK_NAME_PREFIX", f"obsidian-sync-test-{uuid.uuid4()}-")
    monkeypatch.setattr(windows_service, "_metadata_path", lambda: tmp_path / "windows-task.json")
    config = SyncConfig(vault_path=str(tmp_path / "missing-vault"))
    config.lock_path = str(tmp_path / "daemon.lock")
    config.log.path = str(tmp_path / "sync.log")
    config_path = tmp_path / "user's 日本語 config" / "sync config.yaml"
    save_config(config, config_path)
    lock_path = Path(config.lock_path)
    try:
        windows_service.install_task(config_path)
        assert (
            windows_service._powershell(windows_service._task_script("$task.Settings.Enabled\n"))
            == "False"
        )
        windows_service.enable_task()
        deadline = time.monotonic() + 15
        while not lock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert lock_path.exists(), "Scheduled daemon did not acquire its lock"
        assert _is_pid_alive(int(lock_path.read_text()))
        windows_service.disable_task()
        assert not lock_path.exists()
        assert (
            windows_service._powershell(windows_service._task_script("$task.Settings.Enabled\n"))
            == "False"
        )
    finally:
        windows_service.uninstall_task()
    assert not windows_service._metadata_path().exists()
    assert windows_service._powershell(windows_service._task_script("$null -ne $task\n")) == "False"
