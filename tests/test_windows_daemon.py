"""Windows detection, cooperative shutdown and background-process regression tests."""

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psutil
import pytest

from obsidian_sync.config import SyncConfig, save_config
from obsidian_sync.daemon import SyncDaemon, SyncResult, _is_pid_alive, daemonize


@pytest.fixture
def daemon(tmp_path: Path) -> SyncDaemon:
    config = SyncConfig(vault_path=str(tmp_path / "missing-vault"))
    config.lock_path = str(tmp_path / "daemon.lock")
    config.log.path = str(tmp_path / "sync.log")
    return SyncDaemon(config)


@pytest.mark.parametrize("name", ["Obsidian.exe", "OBSIDIAN.EXE", "obsidian.exe"])
def test_windows_detects_obsidian(daemon: SyncDaemon, name: str) -> None:
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch("obsidian_sync.daemon.psutil.process_iter") as processes,
    ):
        processes.return_value = [SimpleNamespace(info={"name": name})]
        assert daemon._is_obsidian_running()


def test_windows_detection_without_obsidian(daemon: SyncDaemon) -> None:
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch("obsidian_sync.daemon.psutil.process_iter") as processes,
    ):
        processes.return_value = [
            SimpleNamespace(info={"name": None}),
            SimpleNamespace(info={"name": "python.exe"}),
        ]
        assert not daemon._is_obsidian_running()


def test_windows_detection_failure_defers(daemon: SyncDaemon) -> None:
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch("obsidian_sync.daemon.psutil.process_iter", side_effect=psutil.AccessDenied),
    ):
        assert daemon._is_obsidian_running()


def test_pid_check_does_not_send_a_signal() -> None:
    with patch("obsidian_sync.daemon.os.kill") as kill:
        assert _is_pid_alive(os.getpid())
        assert not _is_pid_alive(-1)
        assert not _is_pid_alive(0)
        assert not _is_pid_alive(99999999)
        kill.assert_not_called()


def test_windows_stop_finishes_current_cycle(daemon: SyncDaemon) -> None:
    cycles = []

    def sync_cycle() -> SyncResult:
        assert daemon.stop()
        assert daemon._stop_requested()
        cycles.append("finished")
        return SyncResult(synced=True, files_changed=0, message="Done")

    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch.object(daemon, "_setup_signals"),
        patch.object(daemon, "sync_cycle", side_effect=sync_cycle),
        patch("obsidian_sync.daemon.os.kill") as kill,
    ):
        daemon.run()
        kill.assert_not_called()
    assert cycles == ["finished"]
    assert not Path(daemon.config.lock_path).exists()
    assert not daemon._stop_path.exists()


def test_windows_ignores_other_pid_stop_request(daemon: SyncDaemon) -> None:
    daemon._stop_path.write_text("99999999", encoding="ascii")
    with patch("obsidian_sync.daemon.platform.system", return_value="Windows"):
        assert not daemon._stop_requested()


def test_windows_stop_rejects_stale_pid(daemon: SyncDaemon) -> None:
    Path(daemon.config.lock_path).write_text("99999999")
    with patch("obsidian_sync.daemon.platform.system", return_value="Windows"):
        assert not daemon.stop()
    assert not daemon._stop_path.exists()


def test_windows_sleep_wakes_for_stop(daemon: SyncDaemon) -> None:
    daemon._running = True
    daemon._stop_path.write_text(str(os.getpid()), encoding="ascii")
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch("obsidian_sync.daemon.time.sleep") as sleep,
    ):
        daemon._sleep(60)
        sleep.assert_not_called()


def test_windows_daemonize_preserves_config(tmp_path: Path) -> None:
    config_path = tmp_path / "a user's vault" / "config.yaml"
    with (
        patch("obsidian_sync.daemon.sys.platform", "win32"),
        patch("obsidian_sync.daemon.subprocess.Popen") as spawn,
        patch("obsidian_sync.daemon.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True),
        pytest.raises(SystemExit) as exited,
    ):
        daemonize(config_path, verbose=True)
    assert exited.value.code == 0
    command = spawn.call_args.args[0]
    assert command == [
        sys.executable,
        "-m",
        "obsidian_sync",
        "--config",
        str(config_path.resolve()),
        "--verbose",
        "start",
    ]
    assert spawn.call_args.kwargs["creationflags"] == 0x08000000


@pytest.mark.skipif(sys.platform != "win32", reason="Windows background process integration")
def test_windows_background_lifecycle(daemon: SyncDaemon, tmp_path: Path) -> None:
    config_path = tmp_path / "vault 日本語 user's config.yaml"
    save_config(daemon.config, config_path)
    lock_path = Path(daemon.config.lock_path)
    process = None
    try:
        started = subprocess.run(
            [
                sys.executable,
                "-m",
                "obsidian_sync",
                "--config",
                str(config_path),
                "start",
                "--daemon",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 10
        while not lock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert lock_path.exists(), "Background daemon did not acquire its lock"
        process = psutil.Process(int(lock_path.read_text()))
        assert process.is_running()
        stopped = subprocess.run(
            [sys.executable, "-m", "obsidian_sync", "--config", str(config_path), "stop"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert stopped.returncode == 0, stopped.stderr
        process.wait(timeout=10)
        assert not lock_path.exists()
        assert not daemon._stop_path.exists()
    finally:
        if process is not None and process.is_running():
            process.terminate()
            process.wait(timeout=10)
