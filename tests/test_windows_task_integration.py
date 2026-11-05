"""Opt-in Task Scheduler acceptance using disposable tasks, vaults and remotes."""

import csv
import io
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

import click
import psutil
import pytest

from obsidian_sync import windows_service
from obsidian_sync.config import SyncConfig, save_config
from obsidian_sync.daemon import SyncDaemon, _is_pid_alive

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("OBSIDIAN_SYNC_TEST_TASKS") != "1",
    reason="Set OBSIDIAN_SYNC_TEST_TASKS=1 on Windows to test a disposable login task",
)


@pytest.fixture
def scheduled_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate every scheduler mutation and retain raw diagnostics on failures."""
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
    inventory_script = (
        "@(Get-ScheduledTask -TaskPath '\\' -ErrorAction Stop | "
        "Where-Object { $_.TaskName -like 'obsidian-sync-*' } | "
        "Sort-Object TaskName | ForEach-Object { "
        "Export-ScheduledTask -TaskName $_.TaskName -TaskPath '\\' }) | "
        "ConvertTo-Json -Compress\n"
    )
    previous_tasks = windows_service._powershell(inventory_script)
    try:
        yield tmp_path
    finally:
        # The blocked-push test releases its gate before this fixture runs.
        try:
            windows_service.uninstall_task()
        finally:
            for path in (tmp_path / "startup.log", tmp_path / "sync.log"):
                print(
                    f"Fixture diagnostic {path}:\n{path.read_text(encoding='utf-8') if path.exists() else '(absent)'}"
                )
        assert not windows_service._metadata_path().exists()
        assert not windows_service._task_state().exists
        assert windows_service._powershell(inventory_script) == previous_tasks
        print(
            "Cleanup verified: fixture registration/metadata absent; other sync task definitions unchanged"
        )


def _config(tmp_path: Path, vault: Path) -> tuple[SyncConfig, Path]:
    config = SyncConfig(vault_path=str(vault))
    config.lock_path = str(tmp_path / "daemon.lock")
    config.log.path = str(tmp_path / "sync.log")
    config_path = tmp_path / "user's 日本語 config" / "sync config.yaml"
    save_config(config, config_path)
    return config, config_path


def _wait_for(predicate, message: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.1)


def _git(directory: Path, *arguments: str) -> str:
    command = ["git", *arguments]
    result = subprocess.run(
        command, cwd=directory, capture_output=True, encoding="utf-8", timeout=30
    )
    print(f"Git command: {command!r}; cwd={directory}; exit={result.returncode}")
    print(f"Git stdout:\n{result.stdout}\nGit stderr:\n{result.stderr}")
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_task_scheduler_lifecycle(scheduled_task: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tmp_path = scheduled_task
    config, config_path = _config(tmp_path, tmp_path / "missing-vault")
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


def test_scheduled_startup_failure_is_observed(
    scheduled_task: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, config_path = _config(scheduled_task, scheduled_task / "missing-vault")
    windows_service.install_task(config_path)
    metadata = windows_service._metadata_path().read_bytes()
    powershell = windows_service._powershell
    submitted = False

    def remove_config_before_launch(script: str, *, timeout: float = 30) -> str:
        nonlocal submitted
        if "Start-ScheduledTask -TaskName" in script:
            # Pass parent preflight, then exercise real installed-start failure.
            config_path.unlink()
            submitted = True
            print(
                f"Startup fault: removed fixture config before scheduler submission: {config_path}"
            )
        return powershell(script, timeout=timeout)

    monkeypatch.setattr(windows_service, "_powershell", remove_config_before_launch)
    monkeypatch.setattr(windows_service, "START_TIMEOUT_SECONDS", 15)
    with pytest.raises(click.ClickException, match="Task launch failed") as failed:
        windows_service.enable_task()
    print(f"Observed scheduled startup failure: {failed.value}")
    assert submitted
    assert "Task disabled after startup failure" in str(failed.value)
    state = windows_service._task_state()
    print(f"Failed launch state: {state!r}")
    assert state.exists and not state.enabled and state.quiescent
    assert state.last_result not in (None, 0, 267011)
    assert windows_service._metadata_path().read_bytes() == metadata
    assert not Path(config.lock_path).exists()
    diagnostics = windows_service.diagnostic_path().read_text(encoding="utf-8")
    assert "Installed daemon startup/runtime failure" in diagnostics
    assert "Config not found" in diagnostics


def test_scheduled_push_finishes_before_cooperative_uninstall(
    scheduled_task: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path = scheduled_task
    vault = tmp_path / "vault"
    remote = tmp_path / "remote.git"
    vault.mkdir()
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=notes")
    _git(vault, "init", "--initial-branch=notes")
    for key, value in (
        ("user.name", "Scheduler fixture"),
        ("user.email", "scheduler-fixture@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
        ("core.hooksPath", str(vault / ".git" / "hooks")),
    ):
        _git(vault, "config", key, value)
    _git(vault, "remote", "add", "backup", str(remote))
    (vault / "seed.md").write_text("Initial fixture note\n", encoding="utf-8")
    _git(vault, "add", "seed.md")
    _git(vault, "commit", "-m", "Fixture seed")
    _git(vault, "push", "-u", "backup", "notes")
    original_tip = _git(remote, "rev-parse", "refs/heads/notes")
    entered = tmp_path / "push-entered"
    release = tmp_path / "push-release"
    receipt = tmp_path / "received-refs"
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\nset -eu\n"
        f"cat >> {shlex.quote(receipt.as_posix())}\n"
        f": > {shlex.quote(entered.as_posix())}\n"
        "attempt=0\n"
        f"while [ ! -f {shlex.quote(release.as_posix())} ]; do\n"
        "  attempt=$((attempt + 1))\n"
        "  if [ \"$attempt\" -ge 1800 ]; then echo 'Fixture gate expired' >&2; exit 1; fi\n"
        "  sleep 0.1\ndone\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)
    _git(remote, "config", "core.hooksPath", str(remote / "hooks"))
    config, config_path = _config(tmp_path, vault)
    config.sync.remote = "backup"
    config.sync.branch = "notes"
    config.sync.interval_seconds = 1
    save_config(config, config_path)
    note = "日本語 scheduled note with an apostrophe: user's data\n"
    (vault / "note.md").write_text(note, encoding="utf-8", newline="\n")

    # Real Obsidian stays open. Only process detection is replaced in this
    # disposable external worker; installed-start, daemon, Git and stop are real.
    worker = tmp_path / "fixture-worker.py"
    worker.write_text(
        "from obsidian_sync.cli import main\n"
        "from obsidian_sync.daemon import SyncDaemon\n"
        "SyncDaemon._is_obsidian_running = lambda self: False\n"
        "main()\n",
        encoding="utf-8",
    )

    def fixture_arguments(record, metadata_path=None):
        return subprocess.list2cmdline(
            [
                "-P",
                str(worker),
                "installed-start",
                record.installation_id,
                "--metadata",
                str(metadata_path or windows_service._metadata_path()),
                "--diagnostics",
                str(windows_service.diagnostic_path()),
            ]
        )

    monkeypatch.setattr(windows_service, "_arguments", fixture_arguments)
    process = None
    try:
        windows_service.install_task(config_path)
        metadata = windows_service._metadata_path().read_bytes()
        print(f"Scheduled Git startup observation: {windows_service.enable_task()}")
        _wait_for(entered.exists, "Real receive hook did not reach the blocked push")
        lock = Path(config.lock_path)
        process = psutil.Process(int(lock.read_text(encoding="ascii")))
        push_processes = [
            child
            for child in process.children(recursive=True)
            if "git" in child.name().casefold() and "push" in child.cmdline()
        ]
        assert push_processes, "No real Git push child was observed at the receive gate"
        for child in push_processes:
            print(f"Blocked Git child: pid={child.pid}; command={child.cmdline()!r}")
        pending_tip = _git(vault, "rev-parse", "HEAD")
        assert pending_tip != original_tip
        assert _git(remote, "rev-parse", "refs/heads/notes") == original_tip

        for operation in (windows_service.disable_task, windows_service.uninstall_task):
            started = time.monotonic()
            with pytest.raises(click.ClickException) as pending:
                operation()
            elapsed = time.monotonic() - started
            print(f"Pending {operation.__name__} after {elapsed:.3f}s: {pending.value}")
            assert "shutdown is unverified" in str(pending.value)
            assert "Retry after it exits" in str(pending.value)
            assert "no process was forcibly terminated" in str(pending.value)
            assert elapsed >= windows_service.STOP_TIMEOUT_SECONDS
            state = windows_service._task_state()
            print(f"Pending shutdown state: {state!r}")
            assert state.exists and not state.enabled and state.state == "Running"
            assert windows_service._metadata_path().read_bytes() == metadata
            assert process.is_running()
            assert all(child.is_running() for child in push_processes)
            assert lock.read_text(encoding="ascii").strip() == str(process.pid)
            assert lock.with_suffix(".lock.stop").read_text(encoding="ascii").strip() == str(
                process.pid
            )
            assert not release.exists()
            assert _git(remote, "rev-parse", "refs/heads/notes") == original_tip

        # A further cycle would commit this file. It must remain untouched.
        (vault / "after-stop.md").write_text("Must remain uncommitted\n", encoding="utf-8")
        release.touch()
        print(f"Released own receive gate: {release}")
        process.wait(timeout=30)
        assert not lock.exists()
        assert not lock.with_suffix(".lock.stop").exists()
        assert _git(remote, "rev-parse", "refs/heads/notes") == pending_tip
        assert _git(remote, "show", "refs/heads/notes:note.md") == note.strip()
        blob_command = ["git", "show", "refs/heads/notes:note.md"]
        blob = subprocess.run(blob_command, cwd=remote, capture_output=True, timeout=30)
        print(f"Git blob command: {blob_command!r}; cwd={remote}; exit={blob.returncode}")
        print(f"Git blob stdout={blob.stdout!r}; stderr={blob.stderr!r}")
        assert blob.returncode == 0
        assert blob.stdout == note.encode("utf-8")
        assert (
            _git(remote, "ls-tree", "-r", "--name-only", "refs/heads/notes") == "note.md\nseed.md"
        )
        assert _git(vault, "status", "--porcelain") == "?? after-stop.md"
        assert _git(vault, "rev-list", "--count", "HEAD") == "2"
        assert receipt.read_text(encoding="utf-8").splitlines() == [
            f"{original_tip} {pending_tip} refs/heads/notes"
        ]
        windows_service.uninstall_task()
        assert not windows_service._task_state().exists
        assert not windows_service._metadata_path().exists()
        print(
            "Real pending push delivered exact note/commit; daemon exited without another cycle; uninstall retry succeeded"
        )
    finally:
        release.touch()
        print(f"Finally released own receive gate: {release}")
        if process is not None and process.is_running():
            process.wait(timeout=30)
        if receipt.exists():
            print(f"Receive hook receipt:\n{receipt.read_text(encoding='utf-8')}")
