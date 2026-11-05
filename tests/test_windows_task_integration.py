"""Opt-in Task Scheduler smoke test using a disposable task and configuration."""

import csv
import io
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil
import pytest

from obsidian_sync import windows_service
from obsidian_sync.config import SyncConfig, save_config
from obsidian_sync.daemon import SyncDaemon, _is_pid_alive

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("OBSIDIAN_SYNC_TEST_TASKS") != "1",
    reason="Set OBSIDIAN_SYNC_TEST_TASKS=1 on Windows to test a disposable login task",
)


def test_task_scheduler_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows_service, "TASK_NAME_PREFIX", f"obsidian-sync-test-{uuid.uuid4()}-")
    monkeypatch.setattr(windows_service, "_metadata_path", lambda: tmp_path / "windows-task.json")
    monkeypatch.setattr(windows_service, "diagnostic_path", lambda: tmp_path / "startup.log")
    powershell = windows_service._powershell

    def recorded_powershell(script: str, *, timeout: float = 30) -> str:
        print(f"PowerShell script (timeout={timeout}):\n{script}")
        output = powershell(script, timeout=timeout)
        print(f"PowerShell output:\n{output}")
        return output

    monkeypatch.setattr(windows_service, "_powershell", recorded_powershell)
    elevated = windows_service._powershell(
        "$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()\n"
        "$principal = [System.Security.Principal.WindowsPrincipal]::new($identity)\n"
        "$principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)\n"
    )
    print(f"Disposable scheduler test elevated={elevated}")
    assert elevated == "False", "Run this verification in a non-elevated desktop shell"
    config = SyncConfig(vault_path=str(tmp_path / "missing-vault"))
    config.lock_path = str(tmp_path / "daemon.lock")
    config.log.path = str(tmp_path / "sync.log")
    config_path = tmp_path / "user's 日本語 config" / "sync config.yaml"
    save_config(config, config_path)
    lock_path = Path(config.lock_path)
    unresolved = []
    disappeared = []
    obsidian = []
    for process in psutil.process_iter():
        try:
            name = process.name()
            if not name:
                unresolved.append(process.pid)
            elif name.casefold() == "obsidian.exe":
                obsidian.append(process.pid)
        except psutil.NoSuchProcess:
            disappeared.append(process.pid)
        except psutil.AccessDenied:
            unresolved.append(process.pid)
    print(
        f"Process identity probe: unresolved={unresolved}; disappeared={disappeared}; obsidian={obsidian}"
    )
    tasklist = str(Path(os.environ["SystemRoot"]) / "System32" / "tasklist.exe")
    command = [tasklist, "/FO", "CSV", "/NH"]
    snapshot = subprocess.run(command, capture_output=True, encoding="oem", check=True, timeout=10)
    print(f"Process snapshot command: {command!r}; exit={snapshot.returncode}")
    print(f"Process snapshot stdout:\n{snapshot.stdout}")
    print(f"Process snapshot stderr:\n{snapshot.stderr}")
    assert not snapshot.stderr
    rows = list(csv.reader(io.StringIO(snapshot.stdout)))
    observed_names = {row[1]: row[0] for row in rows if row}
    resolved = {pid: observed_names.get(str(pid)) for pid in unresolved}
    print(f"Names for psutil-opaque PIDs from tasklist: {resolved!r}")
    expected_obsidian = any(name.casefold() == "obsidian.exe" for name in observed_names.values())
    assert SyncDaemon(config)._is_obsidian_running() == expected_obsidian
    # Replay this real snapshot without its Obsidian rows. The app stays open,
    # while protected-system rows still exercise the absence decision.
    no_obsidian = [row for row in rows if row and row[0].casefold() != "obsidian.exe"]
    assert no_obsidian
    replay = io.StringIO()
    csv.writer(replay).writerows(no_obsidian)
    with monkeypatch.context() as replay_patch:
        replay_patch.setattr(
            "obsidian_sync.daemon.subprocess.run",
            lambda *args, **kwargs: subprocess.CompletedProcess(command, 0, replay.getvalue(), ""),
        )
        assert not SyncDaemon(config)._is_obsidian_running()
    print(
        "No-Obsidian snapshot replay: sync was not deferred; real Obsidian processes were untouched"
    )
    try:
        windows_service.install_task(config_path)
        assert (
            windows_service._powershell(windows_service._task_script("$task.Settings.Enabled\n"))
            == "False"
        )
        outcome = windows_service.enable_task()
        print(f"Enable observation: {outcome}")
        assert outcome.startswith(("started (", "pending ("))
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
