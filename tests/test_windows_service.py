"""Windows login task setup and cooperative shutdown tests."""

import base64
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import click
import pytest
from click.testing import CliRunner

from obsidian_sync import windows_service
from obsidian_sync.cli import main
from obsidian_sync.config import SyncConfig, save_config


@pytest.fixture
def service_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    interpreter = tmp_path / "Python user's 日本語" / "python.exe"
    interpreter.parent.mkdir()
    interpreter.with_name("pythonw.exe").touch()
    monkeypatch.setattr(windows_service.sys, "executable", str(interpreter))
    config_path = tmp_path / "config user's 日本語" / "sync.yaml"
    config = SyncConfig(
        vault_path=str(tmp_path / "vault"),
        lock_path=str(tmp_path / "daemon.lock"),
    )
    config.log.path = str(tmp_path / "sync.log")
    save_config(config, config_path)
    return config_path


def test_install_captures_config_and_creates_disabled_per_user_task(
    service_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts: list[str] = []
    monkeypatch.setattr(windows_service, "_powershell", lambda script: scripts.append(script))
    windows_service.install_task(service_config)

    metadata = json.loads(windows_service._metadata_path().read_text(encoding="utf-8"))
    assert metadata["config_path"] == str(service_config)
    script = scripts[0]
    assert "WindowsIdentity]::GetCurrent().User.Value" in script
    assert "-LogonType Interactive -RunLevel Limited" in script
    assert "-User $sid" in script
    assert "$settings.Enabled = $false" in script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    assert "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries" in script
    assert "pythonw.exe" in script
    assert "-m obsidian_sync --config" in script
    assert "user''s 日本語" in script
    assert "belongs to another application" in script
    assert "Start-ScheduledTask" not in script


def test_install_missing_pythonw_never_registers_task(
    service_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Path(windows_service.sys.executable).with_name("pythonw.exe").unlink()
    register = Mock()
    monkeypatch.setattr(windows_service, "_powershell", register)
    with pytest.raises(click.ClickException, match="Windowless Python"):
        windows_service.install_task(service_config)
    register.assert_not_called()


def test_install_rejects_relative_runtime_paths(
    service_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = SyncConfig(vault_path="relative-vault")
    save_config(config, service_config)
    register = Mock()
    monkeypatch.setattr(windows_service, "_powershell", register)
    with pytest.raises(click.ClickException, match="absolute vault, log and lock"):
        windows_service.install_task(service_config)
    register.assert_not_called()


def test_powershell_preserves_unicode_and_reports_command_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "日本語\n", ""))
    monkeypatch.setattr(windows_service.subprocess, "run", run)
    assert windows_service._powershell("Write-Output '日本語'") == "日本語"
    argv = run.call_args.args[0]
    decoded = base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "Write-Output '日本語'" in decoded
    assert "-NoProfile" in argv
    assert "-NonInteractive" in argv
    assert run.call_args.kwargs["check"] is True
    assert "shell" not in run.call_args.kwargs

    run.side_effect = subprocess.CalledProcessError(1, argv, stderr="Access is denied")
    with pytest.raises(click.ClickException, match="Task Scheduler operation failed: Access"):
        windows_service._powershell("unused")


def _remember_config(config_path: Path) -> None:
    metadata = windows_service._metadata_path()
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(json.dumps({"config_path": str(config_path)}), encoding="utf-8")


def test_enable_uses_installed_task_without_rewriting_config(
    service_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remember_config(service_config)
    run = Mock()
    monkeypatch.setattr(windows_service, "_powershell", run)
    windows_service.enable_task()
    assert "Enable-ScheduledTask" in run.call_args.args[0]
    assert "Start-ScheduledTask" in run.call_args.args[0]
    assert windows_service._installed_config() == service_config


def test_disable_waits_for_cooperative_stop(
    service_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remember_config(service_config)
    lock_path = service_config.parent.parent / "daemon.lock"
    lock_path.write_text("12345")
    daemon = Mock()
    daemon.stop.return_value = True
    monkeypatch.setattr(windows_service, "SyncDaemon", lambda config: daemon)
    alive = iter([True, False])
    monkeypatch.setattr(windows_service, "_is_pid_alive", lambda pid: next(alive))
    monkeypatch.setattr(windows_service.time, "sleep", lambda seconds: None)
    run = Mock(return_value="False")
    monkeypatch.setattr(windows_service, "_powershell", run)
    windows_service.disable_task()
    daemon.stop.assert_called_once()
    assert "Disable-ScheduledTask" in run.call_args_list[0].args[0]
    assert all("Stop-ScheduledTask" not in call.args[0] for call in run.call_args_list)


def test_disable_catches_daemon_starting_before_lock_file(
    service_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remember_config(service_config)
    running = iter([True, False])
    monkeypatch.setattr(windows_service, "_task_running", lambda: next(running))
    monkeypatch.setattr(windows_service, "_powershell", Mock())
    monkeypatch.setattr(windows_service.time, "sleep", lambda seconds: None)
    windows_service.disable_task()


def test_uninstall_timeout_leaves_task_disabled_and_registration_intact(
    service_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remember_config(service_config)
    lock_path = service_config.parent.parent / "daemon.lock"
    lock_path.write_text("12345")
    daemon = Mock()
    daemon.stop.return_value = True
    monkeypatch.setattr(windows_service, "SyncDaemon", lambda config: daemon)
    monkeypatch.setattr(windows_service, "_is_pid_alive", lambda pid: True)
    times = iter([0, 31])
    monkeypatch.setattr(windows_service.time, "monotonic", lambda: next(times))
    run = Mock()
    monkeypatch.setattr(windows_service, "_powershell", run)
    with pytest.raises(click.ClickException, match="still finishing its sync"):
        windows_service.uninstall_task()
    assert windows_service._metadata_path().exists()
    assert "Disable-ScheduledTask" in run.call_args.args[0]
    assert all("Unregister-ScheduledTask" not in call.args[0] for call in run.call_args_list)


def test_uninstall_stopped_task_removes_only_registration_and_metadata(
    service_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _remember_config(service_config)
    run = Mock(return_value="False")
    monkeypatch.setattr(windows_service, "_powershell", run)
    windows_service.uninstall_task()
    assert "Unregister-ScheduledTask" in run.call_args.args[0]
    assert not windows_service._metadata_path().exists()
    assert service_config.exists()


@pytest.mark.parametrize("command", ["install", "enable", "disable", "uninstall"])
def test_cli_dispatches_windows_service_commands(
    command: str, service_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("obsidian_sync.cli.platform.system", lambda: "Windows")
    handler = Mock()
    monkeypatch.setattr(windows_service, f"{command}_task", handler)
    result = CliRunner().invoke(main, ["--config", str(service_config), command])
    assert result.exit_code == 0, result.output
    if command == "install":
        handler.assert_called_once_with(service_config)
    else:
        handler.assert_called_once_with()
