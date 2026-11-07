"""Per-user Windows login task and its captured cooperative control address."""

import base64
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import click
import yaml

from obsidian_sync.config import DEFAULT_CONFIG_PATH, SyncConfig, expand_paths, load_config
from obsidian_sync.daemon import SyncDaemon, _is_pid_alive, setup_logging

TASK_DESCRIPTION = "obsidian-sync per-user background sync"
TASK_NAME_PREFIX = "obsidian-sync-"
METADATA_VERSION = 1
STOP_TIMEOUT_SECONDS = 30
START_TIMEOUT_SECONDS = 5
QUIESCENT_STATES = ("Disabled", "Ready")
logger = logging.getLogger("obsidian-sync")


@dataclass(frozen=True)
class Installation:
    """Immutable identity and control address of a registered task."""

    schema_version: int
    installation_id: str
    config_path: str
    lock_path: str
    interpreter_path: str


@dataclass(frozen=True)
class TaskState:
    """Scheduler facts, including the last process launch result."""

    exists: bool
    enabled: bool = False
    state: str = "Absent"
    last_result: int | None = None
    executable: str = ""
    arguments: str = ""
    working_directory: str = ""

    @property
    def quiescent(self) -> bool:
        return not self.exists or (not self.enabled and self.state in QUIESCENT_STATES)


def _metadata_path() -> Path:
    return Path.home() / ".config" / "obsidian-sync" / "windows-task.json"


def diagnostic_path() -> Path:
    """A machine-local error sink independent of YAML and metadata contents."""
    local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "state")))
    return local / "obsidian-sync" / "windows-startup.log"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _powershell(script: str, *, timeout: float = 30) -> str:
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
            timeout=max(0.001, timeout),
            check=True,
        )
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or e.stdout or str(e)).strip()
        raise click.ClickException(f"Task Scheduler operation failed: {detail}") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise click.ClickException(f"Could not access Task Scheduler: {e}") from e
    return result.stdout.strip()


def _task_script(body: str) -> str:
    """Enumerate successfully before concluding the exact account task is absent."""
    return (
        "$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value\n"
        f"$taskName = {_quote(TASK_NAME_PREFIX)} + $sid\n"
        "$tasks = @(Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop)\n"
        "$task = $tasks | Where-Object { $_.TaskName -ceq $taskName }\n"
        f"if ($null -ne $task -and $task.Description -ne {_quote(TASK_DESCRIPTION)}) {{\n"
        "    throw 'A task with this name belongs to another application.'\n"
        "}\n" + body
    )


def _task_state(*, timeout: float = 30) -> TaskState:
    output = _powershell(
        _task_script(
            "if ($null -eq $task) { @{exists=$false} | ConvertTo-Json -Compress; return }\n"
            "$info = Get-ScheduledTaskInfo -InputObject $task -ErrorAction Stop\n"
            "$actions = @($task.Actions)\n"
            "@{exists=$true; enabled=[bool]$task.Settings.Enabled; state=[string]$task.State; "
            "last_result=[long]$info.LastTaskResult; "
            "executable= $(if ($actions.Count -eq 1) {[string]$actions[0].Execute} else {''}); "
            "arguments= $(if ($actions.Count -eq 1) {[string]$actions[0].Arguments} else {''}); "
            "working_directory= $(if ($actions.Count -eq 1) "
            "{[string]$actions[0].WorkingDirectory} else {''})} | ConvertTo-Json -Compress\n"
        ),
        timeout=timeout,
    )
    try:
        data = json.loads(output)
        if not isinstance(data, dict) or type(data.get("exists")) is not bool:
            raise ValueError("missing task existence")
        if not data["exists"]:
            return TaskState(False)
        if (
            type(data.get("enabled")) is not bool
            or type(data.get("last_result")) is not int
            or any(
                not isinstance(data.get(key), str)
                for key in ("state", "executable", "arguments", "working_directory")
            )
        ):
            raise ValueError("incomplete task state")
        return TaskState(**data)
    except (ValueError, TypeError) as e:
        raise click.ClickException(f"Task Scheduler returned an invalid state: {output!r}") from e


def _read_installation(metadata_path: Path | None = None) -> Installation:
    try:
        data = json.loads((metadata_path or _metadata_path()).read_text(encoding="utf-8"))
        record = Installation(**data)
        if type(record.schema_version) is not int or record.schema_version != METADATA_VERSION:
            raise ValueError("unsupported metadata schema")
        if str(uuid.UUID(record.installation_id)) != record.installation_id:
            raise ValueError("invalid installation ID")
        for value in (record.config_path, record.lock_path, record.interpreter_path):
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError("installed paths must be absolute")
        return record
    except (OSError, ValueError, TypeError, AttributeError) as e:
        raise click.ClickException(
            "Installation metadata is missing, invalid or legacy. Disable and reinstall."
        ) from e


def _arguments(record: Installation, metadata_path: Path | None = None) -> str:
    return subprocess.list2cmdline(
        [
            "-P",
            "-m",
            "obsidian_sync",
            "installed-start",
            record.installation_id,
            "--metadata",
            str(metadata_path or _metadata_path()),
            "--diagnostics",
            str(diagnostic_path()),
        ]
    )


def _matches(state: TaskState, record: Installation) -> bool:
    return state.exists and (
        state.executable,
        state.arguments,
        state.working_directory,
    ) == _action_values(record)


def _action_values(record: Installation) -> tuple[str, str, str]:
    return record.interpreter_path, _arguments(record), str(Path(record.config_path).parent)


def _action_mismatch(record: Installation) -> str:
    comparisons = [
        f"$task.Actions[0].{name} -cne {_quote(value)}"
        for name, value in zip(
            ("Execute", "Arguments", "WorkingDirectory"), _action_values(record), strict=True
        )
    ]
    return "@($task.Actions).Count -ne 1 -or " + " -or ".join(comparisons)


def _not_quiescent() -> str:
    states = ", ".join(_quote(state) for state in QUIESCENT_STATES)
    return f"$task.Settings.Enabled -or ([string]$task.State -notin @({states}))"


def _load_installed_config(record: Installation) -> SyncConfig:
    try:
        config = expand_paths(load_config(Path(record.config_path), required=True))
        if any(
            not Path(value).is_absolute()
            for value in (config.vault_path, config.log.path, config.lock_path)
        ):
            raise ValueError("Service config requires absolute vault, log and lock paths.")
        if config.lock_path != record.lock_path:
            raise ValueError("Installed control-lock path changed; disable and reinstall.")
        return config
    except (OSError, ValueError, TypeError, yaml.YAMLError) as e:
        raise click.ClickException(f"Installed config is invalid: {e}") from e


def installed_start(
    installation_id: str, metadata_path: Path | None = None, diagnostics: Path | None = None
) -> None:
    """Strict startup: errors have a stable sink before any installation reads."""
    setup_logging(str(diagnostics or diagnostic_path()))
    try:
        record = _read_installation(metadata_path)
        if record.installation_id != installation_id:
            raise click.ClickException("Installed startup ID does not match metadata.")
        config = _load_installed_config(record)
        log_file = Path(config.log.path)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logging.getLogger().addHandler(handler)
        SyncDaemon(config).run()
    except Exception:
        logger.exception("Installed daemon startup/runtime failure")
        raise


def _publish_installation(record: Installation) -> None:
    metadata = _metadata_path()
    metadata.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=metadata.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(record), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, metadata)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_pid(lock_path: str) -> int | None:
    try:
        pid = int(Path(lock_path).read_text(encoding="ascii").strip())
        if pid <= 0:
            raise ValueError("nonpositive PID")
        return pid
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError) as e:
        raise click.ClickException(f"Cannot verify daemon control file {lock_path}: {e}") from e


def install_task(config_path: Path | None) -> None:
    """Register disabled, then atomically publish its matching control record."""
    config_path = (config_path or DEFAULT_CONFIG_PATH).expanduser().absolute()
    try:
        config = expand_paths(load_config(config_path, required=True))
    except (OSError, ValueError, TypeError, yaml.YAMLError) as e:
        raise click.ClickException(f"Invalid service config: {e}") from e
    if any(
        not Path(value).is_absolute()
        for value in (config.vault_path, config.log.path, config.lock_path)
    ):
        raise click.ClickException("Service config requires absolute vault, log and lock paths.")
    interpreter = Path(sys.executable).with_name("pythonw.exe").absolute()
    if not interpreter.is_file():
        raise click.ClickException(f"Windowless Python interpreter not found: {interpreter}")
    state = _task_state()
    if not state.quiescent:
        raise click.ClickException(
            "Service is not disabled and stopped. Disable before reinstalling."
        )
    if state.exists:
        try:
            old = _read_installation()
        except click.ClickException:
            old = None
        if old is not None:
            pid = _read_pid(old.lock_path)
            if pid is not None and _is_pid_alive(pid):
                raise click.ClickException("Daemon is still running. Disable before reinstalling.")
    record = Installation(
        METADATA_VERSION, str(uuid.uuid4()), str(config_path), config.lock_path, str(interpreter)
    )
    _powershell(
        _task_script(
            f"if ($null -ne $task -and ({_not_quiescent()})) {{\n"
            "    throw 'Service is not disabled and stopped. Disable before reinstalling.'\n}\n"
            f"$action = New-ScheduledTaskAction -Execute {_quote(str(interpreter))} "
            f"-Argument {_quote(_arguments(record))} "
            f"-WorkingDirectory {_quote(str(config_path.parent))}\n"
            "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $sid\n"
            # Joes, whose user and computer share a name, rejects the SID principal with
            # (17,8):UserId. The qualified name is stored as the same SID on other hosts.
            "$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name\n"
            "$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive "
            "-RunLevel Limited\n"
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) "
            "-MultipleInstances IgnoreNew\n$settings.Enabled = $false\n"
            "$definition = New-ScheduledTask -Action $action -Trigger $trigger "
            "-Principal $principal -Settings $settings "
            f"-Description {_quote(TASK_DESCRIPTION)}\n"
            "Register-ScheduledTask -TaskName $taskName -TaskPath '\\' "
            "-InputObject $definition -Force | Out-Null\n"
        )
    )
    try:
        _publish_installation(record)
    except OSError as e:
        raise click.ClickException(
            "Task registered disabled, but metadata publication failed. "
            "The installation is repairable: rerun install. " + str(e)
        ) from e


def _disable_registration() -> None:
    _powershell(
        _task_script(
            "if ($null -ne $task) {\n"
            "    Disable-ScheduledTask -TaskName $taskName -TaskPath '\\' | Out-Null\n}\n"
        )
    )


def enable_task() -> str:
    """Start only matching installations and observe started/failed/pending."""
    record = _read_installation()
    _load_installed_config(record)
    state = _task_state()
    if not _matches(state, record):
        _disable_registration()
        raise click.ClickException("Task and installation metadata do not match; reinstall.")
    try:
        return _start_and_observe(record, state)
    except click.ClickException as error:
        try:
            _disable_registration()
        except click.ClickException as cleanup_error:
            raise click.ClickException(
                f"Startup failed: {error}. Future scheduling is unverified because disabling "
                f"also failed: {cleanup_error}. Run disable before retrying. "
                f"Diagnostics: {diagnostic_path()}"
            ) from error
        raise click.ClickException(
            f"Task disabled after startup failure: {error}. "
            "An active sync, if any, was not forcibly stopped."
        ) from error


def _start_and_observe(record: Installation, state: TaskState) -> str:
    """Submit startup and observe it; the caller disables future runs on failure."""
    _powershell(
        _task_script(
            "if ($null -eq $task) { throw 'Service not installed. Run install first.' }\n"
            f"if ({_action_mismatch(record)}) "
            "{ throw 'Task and installation metadata do not match; reinstall.' }\n"
            "Enable-ScheduledTask -TaskName $taskName -TaskPath '\\' | Out-Null\n"
            "Start-ScheduledTask -TaskName $taskName -TaskPath '\\'\n"
        )
    )
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return f"pending (last scheduler state={state.state}, result={state.last_result})"
        # The window bounds observation, not each query: a scheduler query given only
        # the leftover fraction of a second times out and reads as a failed launch.
        state = _task_state()
        if not _matches(state, record):
            raise click.ClickException("Task changed during startup.")
        pid = _read_pid(record.lock_path)
        if state.state == "Running" and pid is not None and _is_pid_alive(pid):
            return f"started (PID {pid}; scheduler result={state.last_result})"
        if state.state in QUIESCENT_STATES and state.last_result not in (0, 267011):
            raise click.ClickException(
                f"Task launch failed (scheduler result={state.last_result}). "
                f"Diagnostics: {diagnostic_path()}"
            )
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def disable_task() -> None:
    """Disable first; shutdown uses the captured address, never mutable YAML."""
    _disable_registration()
    deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
    pending_message = (
        "Task disabled, but shutdown is unverified or the daemon is still finishing "
        "its sync. Retry after it exits; no process was forcibly terminated."
    )
    try:
        record = _read_installation()
    except click.ClickException:
        record = None
    requested_pid = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise click.ClickException(pending_message)
        try:
            state = _task_state(timeout=remaining)
        except click.ClickException as e:
            raise click.ClickException(f"{pending_message} Scheduler query failed: {e}") from e
        if not state.exists and record is None:
            return
        if record is None:
            if state.quiescent:
                return
            raise click.ClickException(
                "Task disabled, but shutdown is unverified: metadata is missing, invalid or "
                "legacy and the task is not positively stopped. Wait for exit before reinstalling."
            )
        pid = _read_pid(record.lock_path)
        live = pid is not None and _is_pid_alive(pid)
        if live and pid is not None and pid != requested_pid:
            if state.exists and not _matches(state, record):
                raise click.ClickException(
                    "Task disabled, but shutdown is unverified: action mismatch."
                )
            daemon = SyncDaemon(SyncConfig(lock_path=record.lock_path))
            if not daemon.stop() and _is_pid_alive(pid):
                raise click.ClickException(
                    "Task disabled, but the daemon could not be asked to stop."
                )
            requested_pid = pid
            live = _is_pid_alive(pid)
        if not live and state.quiescent:
            return
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def uninstall_task() -> None:
    """Remove only a disabled, positively quiescent registration."""
    disable_task()
    _powershell(
        _task_script(
            "if ($null -ne $task) {\n"
            f"    if ({_not_quiescent()}) "
            "{ throw 'Task shutdown is unverified; registration retained.' }\n"
            "    Unregister-ScheduledTask -TaskName $taskName -TaskPath '\\' -Confirm:$false\n}\n"
        )
    )
    try:
        _metadata_path().unlink(missing_ok=True)
    except OSError as e:
        raise click.ClickException(
            f"Task removed, but installation metadata could not be removed: {e}"
        ) from e
