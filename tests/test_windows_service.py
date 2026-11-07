"""Windows installation identity, fail-closed control and failure boundaries."""

import base64
import json
import logging
import os
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import click
import pytest
from click.testing import CliRunner

from obsidian_sync import windows_service as service
from obsidian_sync.cli import main
from obsidian_sync.config import SyncConfig, save_config


class Scheduler:
    """Small scheduler boundary fake; its states are independent test inputs."""

    def __init__(self, lock: Path) -> None:
        self.lock = lock
        self.state = service.TaskState(False)
        self.scripts: list[str] = []
        self.fail = ""
        self.start_result = "Running"

    def run(self, script: str, *, timeout: float = 30) -> str:
        self.scripts.append(script)
        if self.fail and self.fail in script:
            raise click.ClickException("injected scheduler failure")
        values = asdict(self.state)
        if "Register-ScheduledTask" in script:

            def argument(flag: str) -> str:
                match = re.search(rf"{flag} '((?:''|[^'])*)'", script)
                assert match is not None
                return match[1].replace("''", "'")

            self.state = service.TaskState(
                True,
                False,
                "Disabled",
                267011,
                argument("-Execute"),
                argument("-Argument"),
                argument("-WorkingDirectory"),
            )
        elif "Start-ScheduledTask" in script:
            values.update(enabled=True, state=self.start_result, last_result=0)
            if self.start_result == "Running":
                self.lock.write_text(str(os.getpid()), encoding="ascii")
            self.state = service.TaskState(**values)
        elif "Disable-ScheduledTask" in script:
            values["enabled"] = False
            if self.state.state == "Ready":
                values["state"] = "Disabled"
            self.state = service.TaskState(**values)
        elif "Unregister-ScheduledTask" in script:
            assert self.state.quiescent
            self.state = service.TaskState(False)
        elif "ConvertTo-Json" in script:
            stop = Path(f"{self.lock}.stop")
            if stop.exists():
                assert stop.read_text(encoding="ascii") == str(os.getpid())
                self.lock.unlink(missing_ok=True)
                stop.unlink()
                values.update(state="Disabled", enabled=False)
                self.state = service.TaskState(**values)
            return json.dumps(asdict(self.state))
        return ""


@pytest.fixture
def service_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    interpreter = tmp_path / "Python user's 日本語" / "python.exe"
    interpreter.parent.mkdir()
    interpreter.with_name("pythonw.exe").touch()
    monkeypatch.setattr(service.sys, "executable", str(interpreter))
    config_path = tmp_path / "config user's 日本語" / "sync.yaml"
    config = SyncConfig(vault_path=str(tmp_path / "vault"), lock_path=str(tmp_path / "daemon.lock"))
    config.log.path = str(tmp_path / "sync.log")
    save_config(config, config_path)
    return config_path


@pytest.fixture
def scheduler(service_config: Path, monkeypatch: pytest.MonkeyPatch) -> Scheduler:
    scheduler = Scheduler(service_config.parent.parent / "daemon.lock")
    monkeypatch.setattr(service, "_powershell", scheduler.run)
    monkeypatch.setattr(service.time, "sleep", lambda seconds: None)
    return scheduler


def invoke(command: str, config_path: Path | None = None):
    args = [command] if config_path is None else ["--config", str(config_path), command]
    return CliRunner().invoke(main, args)


def test_install_captures_identity_and_disabled_per_user_task(
    service_config: Path,
    scheduler: Scheduler,
) -> None:
    service.install_task(service_config)
    metadata = json.loads(service._metadata_path().read_text(encoding="utf-8"))
    assert metadata["schema_version"] == 1
    assert metadata["config_path"] == str(service_config)
    assert metadata["lock_path"] == str(scheduler.lock)
    assert metadata["interpreter_path"] == str(
        Path(service.sys.executable).with_name("pythonw.exe")
    )
    assert len(metadata["installation_id"]) == 36
    script = next(script for script in scheduler.scripts if "Register-ScheduledTask" in script)
    assert "WindowsIdentity]::GetCurrent().User.Value" in script
    assert "-LogonType Interactive -RunLevel Limited" in script
    assert "-User $sid" in script
    assert "WindowsIdentity]::GetCurrent().Name" in script
    assert "New-ScheduledTaskPrincipal -UserId $account " in script
    assert "$settings.Enabled = $false" in script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script
    assert "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries" in script
    assert "-Hidden" not in script
    assert "-P -m obsidian_sync installed-start " + metadata["installation_id"] in script
    assert "user''s 日本語" in script
    assert "Start-ScheduledTask" not in script
    assert scheduler.state.exists and not scheduler.state.enabled
    assert service._matches(scheduler.state, service._read_installation())


def test_interpreter_spelling_does_not_resolve_virtual_environment(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Path.resolve
    monkeypatch.setattr(
        Path,
        "resolve",
        lambda self, *args, **kwargs: (
            Path("C:/other/pythonw.exe")
            if self.name == "pythonw.exe"
            else original(self, *args, **kwargs)
        ),
    )
    service.install_task(service_config)
    assert service._read_installation().interpreter_path == str(
        Path(service.sys.executable).with_name("pythonw.exe")
    )


def test_missing_interpreter_never_registers(service_config: Path, scheduler: Scheduler) -> None:
    Path(service.sys.executable).with_name("pythonw.exe").unlink()
    with pytest.raises(click.ClickException, match="Windowless Python"):
        service.install_task(service_config)
    assert not scheduler.scripts


def test_install_rejects_relative_runtime_paths(service_config: Path, scheduler: Scheduler) -> None:
    save_config(SyncConfig(vault_path="relative-vault"), service_config)
    with pytest.raises(click.ClickException, match="absolute vault, log and lock"):
        service.install_task(service_config)
    assert not scheduler.scripts


def test_powershell_unicode_errors_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, "日本語\n", ""))
    monkeypatch.setattr(service.subprocess, "run", run)
    assert service._powershell("Write-Output '日本語'", timeout=1.5) == "日本語"
    argv = run.call_args.args[0]
    assert "Write-Output '日本語'" in base64.b64decode(argv[-1]).decode("utf-16-le")
    assert "-NoProfile" in argv and "-NonInteractive" in argv
    assert run.call_args.kwargs["timeout"] == 1.5
    assert run.call_args.kwargs["check"] is True
    run.side_effect = subprocess.CalledProcessError(1, argv, stderr="Access is denied")
    with pytest.raises(click.ClickException, match="Task Scheduler operation failed: Access"):
        service._powershell("unused")


@pytest.mark.parametrize(
    "output", ["", "False", "null", "{}", '{"exists":true}', '{"exists":"false"}']
)
def test_scheduler_unexpected_output_is_error(output: str, monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=output)
    monkeypatch.setattr(service, "_powershell", run)
    with pytest.raises(click.ClickException, match="invalid state"):
        service._task_state(timeout=2)
    script = run.call_args.args[0]
    assert "Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop" in script
    assert "Where-Object { $_.TaskName -ceq $taskName }" in script
    assert "SilentlyContinue" not in script
    assert run.call_args.kwargs == {"timeout": 2}


@pytest.mark.parametrize("state", ["Running", "Queued", "Unknown", "Unexpected"])
def test_reinstall_refuses_nonquiescent_task(
    state: str,
    service_config: Path,
    scheduler: Scheduler,
) -> None:
    scheduler.state = service.TaskState(True, False, state, 0)
    with pytest.raises(click.ClickException, match="Disable before reinstalling"):
        service.install_task(service_config)
    assert not any("Register-ScheduledTask" in script for script in scheduler.scripts)


def test_registration_failure_preserves_prior_metadata(
    service_config: Path,
    scheduler: Scheduler,
) -> None:
    service.install_task(service_config)
    before = service._metadata_path().read_bytes()
    scheduler.fail = "Register-ScheduledTask"
    with pytest.raises(click.ClickException, match="injected"):
        service.install_task(service_config)
    assert service._metadata_path().read_bytes() == before
    assert scheduler.state.quiescent


def test_public_install_publication_failure_leaves_disabled_repairable_task(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("obsidian_sync.cli.platform.system", lambda: "Windows")
    replace = service.os.replace
    monkeypatch.setattr(service.os, "replace", Mock(side_effect=OSError("disk full")))
    result = invoke("install", service_config)
    assert result.exit_code == 1 and "registered disabled" in result.output
    assert scheduler.state.quiescent and scheduler.state.exists
    assert not service._metadata_path().exists()
    assert not list(service._metadata_path().parent.iterdir())
    enabled = invoke("enable")
    assert enabled.exit_code == 1 and "metadata" in enabled.output
    assert not scheduler.state.enabled
    monkeypatch.setattr(service.os, "replace", replace)
    repaired = invoke("install", service_config)
    assert repaired.exit_code == 0
    assert service._matches(scheduler.state, service._read_installation())


def test_published_installation_is_disabled_until_explicit_enable(
    service_config: Path,
    scheduler: Scheduler,
) -> None:
    service.install_task(service_config)
    assert scheduler.state.quiescent
    assert service.enable_task().startswith("started (PID ")
    assert scheduler.state.enabled


def test_failed_republication_retains_old_record_and_refuses_enable(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    original = service._metadata_path().read_bytes()
    monkeypatch.setattr(service.os, "replace", Mock(side_effect=OSError("publication failed")))
    with pytest.raises(click.ClickException, match="registered disabled"):
        service.install_task(service_config)
    assert service._metadata_path().read_bytes() == original
    assert scheduler.state.quiescent
    with pytest.raises(click.ClickException, match="do not match"):
        service.enable_task()
    assert not scheduler.state.enabled
    assert not any("Start-ScheduledTask" in script for script in scheduler.scripts)


@pytest.mark.parametrize("field", ["executable", "arguments", "working_directory"])
def test_enable_refuses_mismatched_action(
    field: str,
    service_config: Path,
    scheduler: Scheduler,
) -> None:
    service.install_task(service_config)
    values = asdict(scheduler.state)
    values[field] = "wrong"
    values["enabled"] = True
    scheduler.state = service.TaskState(**values)
    with pytest.raises(click.ClickException, match="do not match"):
        service.enable_task()
    assert not scheduler.state.enabled
    assert not any("Start-ScheduledTask" in script for script in scheduler.scripts)


@pytest.mark.parametrize("command", ["disable", "uninstall"])
def test_stop_query_failure_reports_pending_and_retains_recovery(
    command: str, service_config: Path, scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("obsidian_sync.cli.platform.system", lambda: "Windows")
    service.install_task(service_config)
    original = service._metadata_path().read_bytes()
    scheduler.fail = "ConvertTo-Json"
    result = invoke(command)
    assert result.exit_code == 1
    assert "shutdown is unverified" in result.output
    assert "Retry after it exits" in result.output
    assert "no process was forcibly terminated" in result.output
    assert "injected scheduler failure" in result.output
    assert scheduler.state.exists and not scheduler.state.enabled
    assert service._metadata_path().read_bytes() == original
    assert not any("Unregister-ScheduledTask" in script for script in scheduler.scripts)


def test_failed_start_exposes_scheduler_result(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    state = scheduler.state
    states = iter(
        [
            state,
            service.TaskState(
                **{**asdict(state), "enabled": True, "state": "Ready", "last_result": 2147942402}
            ),
        ]
    )
    monkeypatch.setattr(service, "_task_state", lambda **kwargs: next(states))
    with pytest.raises(click.ClickException, match="scheduler result=2147942402"):
        service.enable_task()
    assert service._metadata_path().exists()
    assert not scheduler.state.enabled


def test_late_observation_query_gets_a_full_timeout(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    state = scheduler.state
    running = service.TaskState(**{**asdict(state), "enabled": True, "state": "Running"})
    clock = iter([0.0, 4.9, 4.95])
    monkeypatch.setattr(service.time, "monotonic", lambda: next(clock, 4.95))
    monkeypatch.setattr(service.time, "sleep", lambda _: None)
    timeouts: list[object] = []

    def query(**kwargs: object) -> service.TaskState:
        timeouts.append(kwargs.get("timeout"))
        return state if len(timeouts) == 1 else running

    monkeypatch.setattr(service, "_task_state", query)
    monkeypatch.setattr(service, "_read_pid", lambda _: os.getpid())
    assert service.enable_task().startswith("started")
    assert timeouts[1:] == [None]


def test_start_submission_failure_preserves_matching_record(
    service_config: Path,
    scheduler: Scheduler,
) -> None:
    service.install_task(service_config)
    original = service._metadata_path().read_bytes()
    scheduler.fail = "Start-ScheduledTask"
    with pytest.raises(click.ClickException, match="injected"):
        service.enable_task()
    assert service._metadata_path().read_bytes() == original
    assert service._matches(scheduler.state, service._read_installation())


@pytest.mark.parametrize("disable_fails", [False, True])
def test_enable_failure_after_enabling_reports_compensation(
    disable_fails: bool,
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("obsidian_sync.cli.platform.system", lambda: "Windows")
    service.install_task(service_config)
    original = service._metadata_path().read_bytes()
    run = scheduler.run

    def fail_start(script: str, *, timeout: float = 30) -> str:
        if "Start-ScheduledTask" in script:
            scheduler.state = service.TaskState(**{**asdict(scheduler.state), "enabled": True})
            raise click.ClickException("injected start failure after enabling")
        if disable_fails and "Disable-ScheduledTask" in script:
            raise click.ClickException("injected disable failure")
        return run(script, timeout=timeout)

    monkeypatch.setattr(service, "_powershell", fail_start)
    result = invoke("enable")
    assert result.exit_code == 1
    assert "injected start failure after enabling" in result.output
    if disable_fails:
        assert scheduler.state.enabled
        assert "Future scheduling is unverified" in result.output
        assert "Run disable before retrying" in result.output
    else:
        assert not scheduler.state.enabled
        assert "Task disabled after startup failure" in result.output
    assert service._metadata_path().read_bytes() == original


@pytest.mark.parametrize("pending", ["Queued", "Unknown", "Unexpected", "Ready"])
def test_enable_pending_is_not_reported_started(
    pending: str,
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    scheduler.start_result = pending
    ticks = iter([0, 0, 0, 6])
    monkeypatch.setattr(service.time, "monotonic", lambda: next(ticks))
    assert service.enable_task().startswith("pending (")
    assert not scheduler.lock.exists()


@pytest.mark.parametrize("corruption", [None, "[invalid", "[]", "{}", "vault_path: 7"])
def test_installed_start_invalid_yaml_never_constructs_daemon(
    corruption: str | None,
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    record = service._read_installation()
    if corruption is None:
        service_config.unlink()
    else:
        service_config.write_text(corruption, encoding="utf-8")
    daemon = Mock()
    monkeypatch.setattr(service, "SyncDaemon", daemon)
    result = CliRunner().invoke(main, ["installed-start", record.installation_id])
    assert result.exit_code == 1
    daemon.assert_not_called()
    text = service.diagnostic_path().read_text(encoding="utf-8")
    assert "Installed daemon startup/runtime failure" in text and "Traceback" in text
    assert "Installed config is invalid" in text


@pytest.mark.parametrize("metadata", [None, "[invalid", '{"config_path":"C:/legacy.yaml"}'])
def test_early_metadata_failure_is_logged_before_daemon(
    metadata: str | None,
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    if metadata is None:
        service._metadata_path().unlink()
    else:
        service._metadata_path().write_text(metadata, encoding="utf-8")
    daemon = Mock()
    monkeypatch.setattr(service, "SyncDaemon", daemon)
    result = CliRunner().invoke(main, ["installed-start", "d339b5fa-597d-4d24-bd80-ff7ac1dffeb5"])
    assert result.exit_code == 1
    daemon.assert_not_called()
    assert "metadata is missing, invalid or legacy" in service.diagnostic_path().read_text()


def test_installed_start_rejects_wrong_id_and_edited_lock(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    record = service._read_installation()
    daemon = Mock()
    monkeypatch.setattr(service, "SyncDaemon", daemon)
    with pytest.raises(click.ClickException, match="ID does not match"):
        service.installed_start("wrong")
    cfg = SyncConfig(
        vault_path=str(service_config.parent / "vault"),
        lock_path=str(service_config.parent / "wrong.lock"),
    )
    cfg.log.path = str(service_config.parent / "log")
    save_config(cfg, service_config)
    with pytest.raises(click.ClickException, match="control-lock path changed"):
        service.installed_start(record.installation_id)
    daemon.assert_not_called()


def test_installed_runtime_exception_reaches_stable_diagnostics(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    daemon = Mock()
    daemon.run.side_effect = RuntimeError("cycle exploded")
    monkeypatch.setattr(service, "SyncDaemon", lambda config: daemon)
    with pytest.raises(RuntimeError, match="cycle exploded"):
        service.installed_start(service._read_installation().installation_id)
    logging.shutdown()
    assert "RuntimeError: cycle exploded" in service.diagnostic_path().read_text()


@pytest.mark.parametrize("edit", [None, "[invalid", "lock_path: C:/wrong.lock"])
def test_disable_uses_captured_lock_despite_yaml_changes(
    edit: str | None,
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    service.enable_task()
    if edit is None:
        service_config.unlink()
    else:
        service_config.write_text(edit)
    observed: list[str] = []

    def stop(config: SyncConfig):
        observed.append(config.lock_path)
        scheduler.lock.unlink()
        scheduler.state = service.TaskState(**{**asdict(scheduler.state), "state": "Disabled"})
        return Mock(stop=Mock(return_value=True))

    monkeypatch.setattr(service, "SyncDaemon", stop)
    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: scheduler.lock.exists())
    service.disable_task()
    assert observed == [str(scheduler.lock)]
    assert not scheduler.state.enabled
    assert not Path("C:/wrong.lock.stop").exists()


@pytest.mark.parametrize("metadata", [None, "broken", '{"config_path":"C:/legacy.yaml"}'])
def test_broken_metadata_still_disables_but_blocks_active_removal(
    metadata: str | None,
    service_config: Path,
    scheduler: Scheduler,
) -> None:
    service.install_task(service_config)
    service.enable_task()
    if metadata is None:
        service._metadata_path().unlink()
    else:
        service._metadata_path().write_text(metadata)
    with pytest.raises(click.ClickException, match="shutdown is unverified"):
        service.uninstall_task()
    assert scheduler.state.exists and not scheduler.state.enabled
    assert not any("Unregister-ScheduledTask" in script for script in scheduler.scripts)
    assert scheduler.lock.exists()


def test_disable_reads_metadata_only_after_disabling_future_starts(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    scheduler.state = service.TaskState(**{**asdict(scheduler.state), "enabled": True})
    read = service._read_installation

    def verify_order():
        assert scheduler.state.enabled is False
        return read()

    monkeypatch.setattr(service, "_read_installation", verify_order)
    service.disable_task()


@pytest.mark.parametrize("contents", [b"123?", b"\xff", b"", b"-1"])
def test_invalid_pid_file_blocks_removal_after_disabling(
    contents: bytes,
    service_config: Path,
    scheduler: Scheduler,
) -> None:
    service.install_task(service_config)
    scheduler.lock.write_bytes(contents)
    with pytest.raises(click.ClickException, match="Cannot verify daemon control file"):
        service.uninstall_task()
    assert scheduler.state.exists and scheduler.state.enabled is False
    assert service._metadata_path().exists()
    assert scheduler.lock.read_bytes() == contents


def test_disable_queries_use_remaining_stop_budget(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    ticks = iter([100, 113])
    monkeypatch.setattr(service.time, "monotonic", lambda: next(ticks))
    query = Mock(return_value=scheduler.state)
    monkeypatch.setattr(service, "_task_state", query)
    service.disable_task()
    query.assert_called_once_with(timeout=17)


def test_stopped_legacy_registration_can_be_replaced(
    service_config: Path, scheduler: Scheduler
) -> None:
    scheduler.state = service.TaskState(True, False, "Disabled", 0)
    metadata = service._metadata_path()
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"config_path": str(service_config)}))
    service.install_task(service_config)
    assert service._matches(scheduler.state, service._read_installation())


@pytest.mark.parametrize("state", ["Queued", "Unknown", "Unexpected"])
def test_uncertain_state_never_removes_registration(
    state: str,
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    scheduler.state = service.TaskState(**{**asdict(scheduler.state), "state": state})
    ticks = iter([0, 0, 0, 31])
    monkeypatch.setattr(service.time, "monotonic", lambda: next(ticks))
    with pytest.raises(click.ClickException, match="shutdown is unverified"):
        service.uninstall_task()
    assert scheduler.state.exists and not scheduler.state.enabled
    assert service._metadata_path().exists()
    assert not any("Unregister-ScheduledTask" in script for script in scheduler.scripts)


def test_scheduler_query_failure_keeps_metadata_and_registration(
    service_config: Path,
    scheduler: Scheduler,
) -> None:
    service.install_task(service_config)
    scheduler.fail = "Get-ScheduledTaskInfo"
    with pytest.raises(click.ClickException, match="injected"):
        service.uninstall_task()
    assert scheduler.state.exists and not scheduler.state.enabled
    assert service._metadata_path().exists()


def test_delayed_pid_is_requested_before_uninstall(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    scheduler.state = service.TaskState(**{**asdict(scheduler.state), "state": "Running"})
    original = service._read_pid
    reads = 0

    def read(lock: str):
        nonlocal reads
        reads += 1
        if reads == 1:
            return None
        if reads == 2:
            scheduler.lock.write_text(str(os.getpid()))
        return original(lock)

    stop = Mock()

    def request():
        scheduler.lock.unlink()
        scheduler.state = service.TaskState(**{**asdict(scheduler.state), "state": "Disabled"})
        return True

    stop.stop.side_effect = request
    monkeypatch.setattr(service, "SyncDaemon", lambda config: stop)
    monkeypatch.setattr(service, "_read_pid", read)
    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: scheduler.lock.exists())
    service.uninstall_task()
    stop.stop.assert_called_once()
    assert reads >= 2 and not scheduler.state.exists
    assert not service._metadata_path().exists()


def test_failed_stop_rechecks_benign_exit(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    scheduler.lock.write_text("12345")
    alive = iter([True, False, False])
    monkeypatch.setattr(service, "_is_pid_alive", lambda pid: next(alive))
    stop = Mock(stop=Mock(return_value=False))
    monkeypatch.setattr(service, "SyncDaemon", lambda config: stop)
    service.disable_task()
    stop.stop.assert_called_once()


def test_stop_request_failure_retains_installation(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    scheduler.lock.write_text(str(os.getpid()))
    stop = Mock(stop=Mock(return_value=False))
    monkeypatch.setattr(service, "SyncDaemon", lambda config: stop)
    with pytest.raises(click.ClickException, match="could not be asked to stop"):
        service.uninstall_task()
    assert not scheduler.state.enabled and scheduler.state.exists
    assert service._metadata_path().exists()


def test_unregister_failure_retains_metadata(service_config: Path, scheduler: Scheduler) -> None:
    service.install_task(service_config)
    scheduler.fail = "Unregister-ScheduledTask"
    with pytest.raises(click.ClickException, match="injected"):
        service.uninstall_task()
    assert scheduler.state.exists and not scheduler.state.enabled
    assert service._metadata_path().exists()


def test_metadata_removal_failure_reports_repairable_absent_task(
    service_config: Path,
    scheduler: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.install_task(service_config)
    metadata = service._metadata_path()
    original = Path.unlink

    def unlink(path: Path, *args, **kwargs):
        if path == metadata:
            raise PermissionError("metadata retained")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(click.ClickException, match="Task removed, but installation metadata"):
        service.uninstall_task()
    assert not scheduler.state.exists
    assert metadata.exists()


def test_stopped_uninstall_preserves_user_files(service_config: Path, scheduler: Scheduler) -> None:
    service.install_task(service_config)
    service.uninstall_task()
    assert not scheduler.state.exists
    assert not service._metadata_path().exists()
    assert service_config.exists()


@pytest.mark.parametrize("command", ["install", "enable", "disable", "uninstall"])
def test_cli_dispatches_windows_service_commands(
    command: str,
    service_config: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("obsidian_sync.cli.platform.system", lambda: "Windows")
    handler = Mock(return_value="pending")
    monkeypatch.setattr(service, f"{command}_task", handler)
    result = invoke(command, service_config)
    assert result.exit_code == 0, result.output
    handler.assert_called_once_with(*([service_config] if command == "install" else []))
