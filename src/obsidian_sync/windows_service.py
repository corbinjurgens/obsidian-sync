"""Per-user Windows login task for the sync daemon."""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import click

from obsidian_sync.config import DEFAULT_CONFIG_PATH, expand_paths, load_config
from obsidian_sync.daemon import SyncDaemon, _is_pid_alive

TASK_DESCRIPTION = "obsidian-sync per-user background sync"
TASK_NAME_PREFIX = "obsidian-sync-"
STOP_TIMEOUT_SECONDS = 30


def _metadata_path() -> Path:
    return Path.home() / ".config" / "obsidian-sync" / "windows-task.json"


def _quote(value: str) -> str:
    """Quote a literal PowerShell string without interpreting its contents."""
    return "'" + value.replace("'", "''") + "'"


def _powershell(script: str) -> str:
    prefix = (
        "$ErrorActionPreference = 'Stop'\n"
        "$ProgressPreference = 'SilentlyContinue'\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
    )
    encoded = base64.b64encode((prefix + script).encode("utf-16-le")).decode("ascii")
    executable = (
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    try:
        result = subprocess.run(
            [str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=30,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise click.ClickException(f"Task Scheduler operation failed: {detail}") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise click.ClickException(f"Could not access Task Scheduler: {e}") from e
    return result.stdout.strip()


def _task_script(body: str) -> str:
    """Find only this account's task and refuse to modify an unrelated task."""
    return (
        "$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value\n"
        f"$taskName = {_quote(TASK_NAME_PREFIX)} + $sid\n"
        "$task = Get-ScheduledTask -TaskName $taskName -TaskPath '\\' "
        "-ErrorAction SilentlyContinue\n"
        f"if ($null -ne $task -and $task.Description -ne {_quote(TASK_DESCRIPTION)}) {{\n"
        "    throw 'A task with this name belongs to another application.'\n"
        "}\n" + body
    )


def _installed_config() -> Path:
    try:
        data = json.loads(_metadata_path().read_text(encoding="utf-8"))
        config_path = Path(data["config_path"])
        if not config_path.is_absolute():
            raise ValueError("installed config path is not absolute")
        return config_path
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise click.ClickException("Service not installed. Run 'install' first.") from e


def install_task(config_path: Path | None) -> None:
    """Register a disabled login task using the current Python environment."""
    config_path = (config_path or DEFAULT_CONFIG_PATH).expanduser().resolve()
    if not config_path.is_file():
        raise click.ClickException(f"Config not found: {config_path}. Run 'config --init' first.")
    # Resolve relative vault/log/lock paths once, rather than relative to
    # Task Scheduler's working directory after the next login.
    config = expand_paths(load_config(config_path))
    for field in (config.vault_path, config.log.path, config.lock_path):
        if not Path(field).is_absolute():
            raise click.ClickException(
                "Service config requires absolute vault, log and lock paths."
            )
    interpreter = Path(sys.executable).with_name("pythonw.exe").resolve()
    if not interpreter.is_file():
        raise click.ClickException(f"Windowless Python interpreter not found: {interpreter}")
    arguments = subprocess.list2cmdline(
        ["-m", "obsidian_sync", "--config", str(config_path), "start"]
    )
    script = _task_script(
        "if ($null -ne $task -and $task.State -eq 'Running') {\n"
        "    throw 'Service is running. Disable it before reinstalling.'\n"
        "}\n"
        f"$action = New-ScheduledTaskAction -Execute {_quote(str(interpreter))} "
        f"-Argument {_quote(arguments)} -WorkingDirectory {_quote(str(config_path.parent))}\n"
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid\n"
        "$principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive "
        "-RunLevel Limited\n"
        "$settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-MultipleInstances IgnoreNew\n"
        "$settings.Enabled = $false\n"
        "$definition = New-ScheduledTask -Action $action -Trigger $trigger "
        "-Principal $principal -Settings $settings "
        f"-Description {_quote(TASK_DESCRIPTION)}\n"
        "Register-ScheduledTask -TaskName $taskName -TaskPath '\\' "
        "-InputObject $definition -Force | Out-Null\n"
    )
    _powershell(script)
    metadata = _metadata_path()
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(json.dumps({"config_path": str(config_path)}), encoding="utf-8")


def enable_task() -> None:
    """Enable and immediately start the installed task."""
    _installed_config()
    _powershell(
        _task_script(
            "if ($null -eq $task) { throw 'Service not installed. Run install first.' }\n"
            "Enable-ScheduledTask -TaskName $taskName -TaskPath '\\' | Out-Null\n"
            "Start-ScheduledTask -TaskName $taskName -TaskPath '\\'\n"
        )
    )


def _task_running() -> bool:
    return _powershell(_task_script("$null -ne $task -and $task.State -eq 'Running'\n")) == "True"


def disable_task() -> None:
    """Disable future starts and let any active sync finish before stopping."""
    config = expand_paths(load_config(_installed_config()))
    _powershell(
        _task_script(
            "if ($null -ne $task) {\n"
            "    Disable-ScheduledTask -TaskName $taskName -TaskPath '\\' | Out-Null\n"
            "}\n"
        )
    )
    daemon = SyncDaemon(config)
    lock_path = Path(config.lock_path)
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    requested_pid = None
    while True:
        try:
            pid = int(lock_path.read_text().strip())
        except (OSError, ValueError):
            pid = None
        live_pid = pid is not None and _is_pid_alive(pid)
        if live_pid and pid != requested_pid:
            if not daemon.stop():
                raise click.ClickException(
                    "Task disabled, but the daemon could not be asked to stop."
                )
            requested_pid = pid
        if not live_pid and not _task_running():
            return
        if time.monotonic() >= deadline:
            raise click.ClickException(
                "Task disabled, but the daemon is still finishing its sync. "
                "Wait for it to stop and retry; no process was forcibly terminated."
            )
        time.sleep(0.2)


def uninstall_task() -> None:
    """Remove the account's task only after its daemon has stopped."""
    if not _metadata_path().exists():
        # Without the installed config, an active task cannot be stopped safely.
        _powershell(
            _task_script(
                "if ($null -ne $task) {\n"
                "    throw 'Installed config is missing. Reinstall before uninstalling.'\n"
                "}\n"
            )
        )
        return
    disable_task()
    _powershell(
        _task_script(
            "if ($null -ne $task) {\n"
            "    Unregister-ScheduledTask -TaskName $taskName -TaskPath '\\' -Confirm:$false\n"
            "}\n"
        )
    )
    _metadata_path().unlink()
