"""Windows detection, cooperative shutdown and background-process regression tests."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import psutil
import pytest

from obsidian_sync.config import SyncConfig, save_config
from obsidian_sync.daemon import LockError, SyncDaemon, SyncResult, _is_pid_alive, daemonize


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SyncDaemon:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    config = SyncConfig(vault_path=str(tmp_path / "missing-vault"))
    config.lock_path = str(tmp_path / "daemon.lock")
    config.log.path = str(tmp_path / "sync.log")
    return SyncDaemon(config)


@pytest.mark.parametrize("name", ["Obsidian.exe", "OBSIDIAN.EXE", "obsidian.exe"])
def test_windows_detects_obsidian(daemon: SyncDaemon, name: str) -> None:
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch("obsidian_sync.daemon.subprocess.run") as run,
    ):
        run.return_value = subprocess.CompletedProcess(
            [], 0, f'"{name}","123","Console","1","123 K"\r\n', ""
        )
        assert daemon._is_obsidian_running()
        assert run.call_args.args[0][1:] == ["/FO", "CSV", "/NH"]
        assert Path(run.call_args.args[0][0]).name == "tasklist.exe"
        assert run.call_args.kwargs["encoding"] == "oem"
        assert run.call_args.kwargs["errors"] == "strict"
        assert run.call_args.kwargs["check"] is True
        assert run.call_args.kwargs["timeout"] == 10


def test_windows_detection_without_obsidian(daemon: SyncDaemon) -> None:
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch("obsidian_sync.daemon.subprocess.run") as run,
    ):
        run.return_value = subprocess.CompletedProcess(
            [],
            0,
            '"Secure System","42","Services","0","12 K"\r\n'
            '"日本語, quoted.exe","123","Console","1","123 K"\r\n',
            "",
        )
        assert not daemon._is_obsidian_running()


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("tasklist missing"),
        PermissionError("denied"),
        subprocess.CalledProcessError(1, ["tasklist"], stderr="Access denied"),
        subprocess.TimeoutExpired(["tasklist"], 10),
        UnicodeDecodeError("oem", b"\xff", 0, 1, "cannot decode"),
    ],
)
def test_windows_detection_failure_defers(daemon: SyncDaemon, failure: Exception, caplog) -> None:
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch("obsidian_sync.daemon.subprocess.run", side_effect=failure),
    ):
        assert daemon._is_obsidian_running()
    assert "deferring sync" in caplog.text


def test_windows_protected_names_do_not_require_per_process_access(daemon: SyncDaemon) -> None:
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch(
            "obsidian_sync.daemon.psutil.process_iter", side_effect=psutil.AccessDenied
        ) as processes,
        patch(
            "obsidian_sync.daemon.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, '"Secure System","42","Services","0","12 K"\r\n', ""
            ),
        ),
    ):
        assert not daemon._is_obsidian_running()
        processes.assert_not_called()


@pytest.mark.parametrize(
    "output",
    [
        "",
        "INFO: No tasks are running which match the specified criteria.",
        "エラー: アクセスが拒否されました",
        '"","42","Services","0","12 K"',
        '"python.exe","unknown","Services","0","12 K"',
        '"python.exe","42"',
        '"unterminated',
        '"python.exe","42","Services","0","12 K"\nERROR: incomplete snapshot',
    ],
)
def test_windows_incomplete_snapshot_defers(daemon: SyncDaemon, output: str, caplog) -> None:
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch(
            "obsidian_sync.daemon.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, output, ""),
        ),
    ):
        assert daemon._is_obsidian_running()
    assert "deferring sync" in caplog.text


def test_windows_success_with_diagnostics_still_defers(daemon: SyncDaemon, caplog) -> None:
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch(
            "obsidian_sync.daemon.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 0, '"python.exe","42","Services","0","12 K"', "partial query failure"
            ),
        ),
    ):
        assert daemon._is_obsidian_running()
    assert "deferring sync" in caplog.text


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
        patch(
            "obsidian_sync.windows_service.diagnostic_path", return_value=tmp_path / "startup.log"
        ),
        patch("obsidian_sync.daemon.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True),
        pytest.raises(SystemExit) as exited,
    ):
        daemonize(config_path, verbose=True)
    assert exited.value.code == 0
    command = spawn.call_args.args[0]
    assert command == [
        sys.executable,
        "-P",
        "-m",
        "obsidian_sync",
        "--config",
        str(config_path.resolve()),
        "--verbose",
        "start",
    ]
    assert spawn.call_args.kwargs["creationflags"] == 0x08000000
    assert spawn.call_args.kwargs["stderr"].name == str(tmp_path / "startup.log")
    assert spawn.call_args.kwargs["stdout"] is spawn.call_args.kwargs["stderr"]


@pytest.mark.parametrize("contents", [b"not a pid", b"", b"\xff", b"-1"])
def test_corrupt_windows_stop_request_fails_explicitly(daemon: SyncDaemon, contents: bytes) -> None:
    daemon._stop_path.write_bytes(contents)
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        pytest.raises(LockError, match="Cannot read stop request"),
    ):
        daemon._stop_requested()


def test_windows_stop_request_io_failure_is_explicit(daemon: SyncDaemon) -> None:
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch.object(Path, "read_text", side_effect=PermissionError("denied")),
        pytest.raises(LockError, match="denied"),
    ):
        daemon._stop_requested()


def test_windows_stop_publication_is_atomic(daemon: SyncDaemon) -> None:
    Path(daemon.config.lock_path).write_text(str(os.getpid()))
    daemon._stop_path.write_text("old request")
    replace = os.replace
    observed = []

    def publish(source, target):
        observed.append((Path(source).read_text(), Path(target).read_text()))
        replace(source, target)

    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch("obsidian_sync.daemon.os.replace", side_effect=publish),
    ):
        assert daemon.stop()
    assert observed == [(str(os.getpid()), "old request")]
    assert daemon._stop_path.read_text() == str(os.getpid())


def test_windows_failed_stop_publication_preserves_previous_request(daemon: SyncDaemon) -> None:
    lock = Path(daemon.config.lock_path)
    lock.write_text(str(os.getpid()))
    daemon._stop_path.write_text("previous")
    with (
        patch("obsidian_sync.daemon.platform.system", return_value="Windows"),
        patch("obsidian_sync.daemon.os.replace", side_effect=OSError("denied")),
    ):
        assert not daemon.stop()
    assert daemon._stop_path.read_text() == "previous"
    assert sorted(path.name for path in lock.parent.iterdir()) == [
        "daemon.lock",
        "daemon.lock.stop",
    ]


def test_unix_release_never_reads_or_removes_windows_sidecar(daemon: SyncDaemon) -> None:
    daemon._stop_path.write_text(str(os.getpid()))
    with patch("obsidian_sync.daemon.platform.system", return_value="Linux"):
        daemon._acquire_lock()
        daemon._release_lock()
    assert daemon._stop_path.read_text() == str(os.getpid())


def test_runtime_exception_logs_traceback_and_releases_lock(daemon: SyncDaemon, caplog) -> None:
    with (
        patch.object(daemon, "_setup_signals"),
        patch.object(daemon, "sync_cycle", side_effect=RuntimeError("cycle exploded")),
        pytest.raises(RuntimeError, match="cycle exploded"),
    ):
        daemon.run()
    assert not Path(daemon.config.lock_path).exists()
    assert any(record.message == "Daemon failed" and record.exc_info for record in caplog.records)


@pytest.mark.parametrize("contents", [None, "vault_path: [invalid yaml"])
def test_installed_start_reports_early_config_failure(tmp_path: Path, contents: str | None) -> None:
    """An actual child must fail before lock acquisition and leave diagnostics."""
    config_path = tmp_path / "user's 日本語 config.yaml"
    if contents is not None:
        config_path.write_text(contents, encoding="utf-8")
    metadata = tmp_path / "windows-task.json"
    lock = tmp_path / "daemon.lock"
    diagnostics = tmp_path / "startup.log"
    installation_id = "fa1146bd-764a-438d-b3c7-66fba9dc35d9"
    metadata.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "installation_id": installation_id,
                "config_path": str(config_path),
                "lock_path": str(lock),
                "interpreter_path": sys.executable,
            }
        ),
        encoding="utf-8",
    )
    original_metadata = metadata.read_bytes()
    command = [
        sys.executable,
        "-P",
        "-m",
        "obsidian_sync",
        "installed-start",
        installation_id,
        "--metadata",
        str(metadata),
        "--diagnostics",
        str(diagnostics),
    ]
    result = subprocess.run(
        command, cwd=tmp_path, capture_output=True, encoding="utf-8", timeout=15
    )
    print(f"Installed startup command: {command!r}; exit={result.returncode}")
    print(f"Installed startup stdout:\n{result.stdout}\nInstalled startup stderr:\n{result.stderr}")
    assert result.returncode != 0
    diagnostic_text = diagnostics.read_text(encoding="utf-8")
    print(f"Installed startup diagnostics:\n{diagnostic_text}")
    assert "Installed daemon startup/runtime failure" in diagnostic_text
    assert "Installed config is invalid" in diagnostic_text
    assert "Traceback" in diagnostic_text
    if contents is None:
        assert "Config not found" in diagnostic_text
    else:
        assert "yaml.parser.ParserError" in diagnostic_text
    assert not lock.exists()
    assert metadata.read_bytes() == original_metadata


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
