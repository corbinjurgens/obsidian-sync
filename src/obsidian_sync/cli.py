"""Click CLI for obsidian-sync."""

import importlib.resources
import json
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from obsidian_sync import windows_service
from obsidian_sync.config import (
    DEFAULT_CONFIG_PATH,
    SyncConfig,
    default_vault_path,
    expand_paths,
    load_config,
    save_config,
)
from obsidian_sync.daemon import LockError, SyncDaemon, _is_pid_alive, daemonize, setup_logging

console = Console()

LAUNCHD_LABEL = "com.alexfurrier.obsidian-sync"
LAUNCHD_PLIST_NAME = f"{LAUNCHD_LABEL}.plist"
LEGACY_LAUNCHD_PLIST_NAME = "io.github.obsidian-sync.plist"
SYSTEMD_UNIT_NAME = "obsidian-sync.service"
SYNC_SUCCESS_LINE = re.compile(r"^(\S+ [^\s,]+)\S* \[INFO\] (?:Synced \d+ file|Working tree clean)")


def _daemon_status(lock_path: Path) -> tuple[str, str | None]:
    """Return daemon status and PID from the lock file when available."""
    if not lock_path.exists():
        return "Stopped", None

    try:
        pid = lock_path.read_text().strip()
    except OSError:
        return "Unknown", None

    if not pid:
        return "Unknown", None

    try:
        if _is_pid_alive(int(pid)):
            return "Running", pid
    except ValueError:
        return "Invalid lock", pid

    return "Stale lock", pid


def _service_status() -> dict[str, bool | str | None]:
    """Report the login service without changing it; None marks an unknown fact."""
    system = platform.system()
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / LAUNCHD_PLIST_NAME
        loaded = subprocess.run(
            ["launchctl", "list", LAUNCHD_LABEL], capture_output=True, check=False
        )
        return {"installed": plist.exists(), "enabled": loaded.returncode == 0, "detail": "launchd"}
    if system == "Linux":
        unit = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
        enabled = subprocess.run(
            ["systemctl", "--user", "is-enabled", SYSTEMD_UNIT_NAME],
            capture_output=True,
            check=False,
        )
        return {"installed": unit.exists(), "enabled": enabled.returncode == 0, "detail": "systemd"}
    if system == "Windows":
        try:
            task = windows_service._task_state()
        except click.ClickException as e:
            return {"installed": None, "enabled": None, "detail": e.message}
        return {"installed": task.exists, "enabled": task.enabled, "detail": task.state}
    return {"installed": None, "enabled": None, "detail": f"Unsupported platform: {system}"}


def _last_sync(log_lines: list[str]) -> str | None:
    """Return the timestamp of the newest successful cycle in the log."""
    for line in reversed(log_lines):
        match = SYNC_SUCCESS_LINE.match(line)
        if match:
            return match.group(1)
    return None


def _load_and_expand(config_path: Path | None) -> SyncConfig:
    """Load config and expand all paths."""
    config = load_config(config_path)
    return expand_paths(config)


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=False, path_type=Path),
    default=None,
    help="Path to config file.",
)
@click.option("--verbose", is_flag=True, default=False, help="Enable verbose output.")
@click.pass_context
def main(ctx: click.Context, config_path: Path | None, verbose: bool) -> None:
    """Git sync daemon for Obsidian vaults."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["verbose"] = verbose


@main.command()
@click.option("--daemon", "run_daemon", is_flag=True, default=False, help="Run in background.")
@click.pass_context
def start(ctx: click.Context, run_daemon: bool) -> None:
    """Start the sync loop."""
    config = _load_and_expand(ctx.obj["config_path"])
    setup_logging(config.log.path, verbose=ctx.obj["verbose"])

    if run_daemon:
        console.print(f"Starting daemon (vault: {config.vault_path})")
        daemonize(ctx.obj["config_path"], verbose=ctx.obj["verbose"])

    d = SyncDaemon(config)
    try:
        d.run()
    except LockError as e:
        raise click.ClickException(str(e)) from e


@main.command("installed-start", hidden=True)
@click.argument("installation_id")
@click.option("--metadata", type=click.Path(path_type=Path), default=None, hidden=True)
@click.option("--diagnostics", type=click.Path(path_type=Path), default=None, hidden=True)
def installed_start(installation_id: str, metadata: Path | None, diagnostics: Path | None) -> None:
    """Start only the installation captured by the Windows login task."""
    windows_service.installed_start(installation_id, metadata, diagnostics)


@main.command()
@click.pass_context
def stop(ctx: click.Context) -> None:
    """Stop a running daemon."""
    config = _load_and_expand(ctx.obj["config_path"])
    lock_path = Path(config.lock_path)

    if not lock_path.exists():
        raise click.ClickException("No daemon is running (lock file not found)")

    d = SyncDaemon(config)
    if d.stop():
        console.print("[green]Shutdown requested; daemon will stop after the current sync[/green]")
    else:
        raise click.ClickException("Failed to stop daemon")


@main.command()
@click.option("--json", "as_json", is_flag=True, default=False, help="Print JSON for tools.")
@click.pass_context
def status(ctx: click.Context, as_json: bool) -> None:
    """Show daemon status."""
    config = _load_and_expand(ctx.obj["config_path"])
    lock_path = Path(config.lock_path)
    log_path = Path(config.log.path)
    log_lines: list[str] = []
    if log_path.exists():
        log_lines = log_path.read_text(encoding="utf-8").strip().splitlines()

    if as_json:
        daemon_status, pid = _daemon_status(lock_path)
        report = {
            "daemon": daemon_status,
            "pid": pid,
            "service": _service_status(),
            "vault": config.vault_path,
            "last_sync": _last_sync(log_lines),
            "last_log": log_lines[-1] if log_lines else None,
        }
        click.echo(json.dumps(report))
        return

    table = Table(title="obsidian-sync status", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")

    daemon_status, pid = _daemon_status(lock_path)
    if daemon_status == "Running":
        table.add_row("Status", "[green]Running[/green]")
    elif daemon_status == "Stopped":
        table.add_row("Status", "[dim]Stopped[/dim]")
    elif daemon_status == "Stale lock":
        table.add_row("Status", "[yellow]Stale lock[/yellow]")
    elif daemon_status == "Invalid lock":
        table.add_row("Status", "[yellow]Invalid lock[/yellow]")
    else:
        table.add_row("Status", "[yellow]Unknown[/yellow]")

    if pid is not None:
        table.add_row("PID", pid)

    table.add_row("Vault", config.vault_path)
    table.add_row("Interval", f"{config.sync.interval_seconds}s")
    table.add_row("Strategy", config.sync.pull_strategy)
    table.add_row("Remote", f"{config.sync.remote}/{config.sync.branch}")

    if log_lines:
        table.add_row("Last log", log_lines[-1])

    console.print(table)


@main.command()
@click.pass_context
def sync(ctx: click.Context) -> None:
    """Run one sync cycle, then exit."""
    config = _load_and_expand(ctx.obj["config_path"])
    setup_logging(config.log.path, verbose=ctx.obj["verbose"])
    d = SyncDaemon(config)
    result = d.run_once()

    if result.error:
        raise click.ClickException(result.message)
    elif result.deferred:
        console.print(f"[yellow]{result.message}[/yellow]")
    elif result.files_changed > 0:
        console.print(f"[green]{result.message}[/green]")
    else:
        console.print(f"[dim]{result.message}[/dim]")


@main.command()
@click.option("--tail", is_flag=True, default=False, help="Follow log output.")
@click.option("--since", default=None, help="Show entries since duration (e.g., 1h, 30m).")
@click.option("-n", "lines", default=50, help="Number of lines to show.")
@click.pass_context
def log(ctx: click.Context, tail: bool, since: str | None, lines: int) -> None:
    """Show sync log entries."""
    config = _load_and_expand(ctx.obj["config_path"])
    log_path = Path(config.log.path)

    if not log_path.exists():
        raise click.ClickException(f"Log file not found: {log_path}")

    if tail:
        with log_path.open(encoding="utf-8") as stream:
            for line in stream.readlines()[-lines:]:
                console.print(line.rstrip("\n"))
            while True:
                line = stream.readline()
                if line:
                    console.print(line.rstrip("\n"))
                else:
                    time.sleep(0.2)
    else:
        all_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        for line in all_lines[-lines:]:
            console.print(line)


@main.command()
@click.option("--init", "do_init", is_flag=True, default=False, help="Create default config.")
@click.option("--edit", "do_edit", is_flag=True, default=False, help="Open config in $EDITOR.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Print JSON for tools.")
@click.pass_context
def config(ctx: click.Context, do_init: bool, do_edit: bool, as_json: bool) -> None:
    """Show or initialize configuration."""
    config_path = ctx.obj["config_path"] or DEFAULT_CONFIG_PATH.expanduser()

    if do_init:
        if config_path.exists():
            raise click.ClickException(f"Config already exists: {config_path}")
        vault = click.prompt("Vault path", default=default_vault_path())
        interval = click.prompt("Sync interval (seconds)", default=300, type=int)
        cfg = SyncConfig(vault_path=vault)
        cfg.sync.interval_seconds = interval
        save_config(cfg, config_path)
        console.print(f"[green]Config created: {config_path}[/green]")
        return

    if do_edit:
        editor = os.environ.get("EDITOR", "notepad" if platform.system() == "Windows" else "vim")
        if not config_path.exists():
            msg = f"Config not found: {config_path}. Run 'config --init' first."
            raise click.ClickException(msg)
        subprocess.run([editor, str(config_path)], check=False)
        return

    cfg = load_config(ctx.obj["config_path"])
    if as_json:
        click.echo(json.dumps(asdict(cfg)))
        return

    table = Table(title="Configuration", show_header=False)
    table.add_column("Key", style="bold")
    table.add_column("Value")
    table.add_row("vault_path", cfg.vault_path)
    table.add_row("sync.interval_seconds", str(cfg.sync.interval_seconds))
    table.add_row("sync.pull_strategy", cfg.sync.pull_strategy)
    table.add_row("sync.remote", cfg.sync.remote)
    table.add_row("sync.branch", cfg.sync.branch)
    table.add_row("sync.backoff_initial_seconds", str(cfg.sync.backoff_initial_seconds))
    table.add_row("sync.backoff_multiplier", str(cfg.sync.backoff_multiplier))
    table.add_row("sync.backoff_max_seconds", str(cfg.sync.backoff_max_seconds))
    table.add_row("commit.template", cfg.commit.template)
    table.add_row("commit.date_format", cfg.commit.date_format)
    table.add_row("commit.list_files_in_body", str(cfg.commit.list_files_in_body))
    table.add_row("log.path", cfg.log.path)
    table.add_row("log.max_size_mb", str(cfg.log.max_size_mb))
    table.add_row("lock_path", cfg.lock_path)
    console.print(table)


@main.command()
@click.pass_context
def install(ctx: click.Context) -> None:
    """Install a login service (launchd, systemd or Windows Task Scheduler)."""
    system = platform.system()

    if system == "Darwin":
        _install_launchd()
    elif system == "Linux":
        _install_systemd()
    elif system == "Windows":
        windows_service.install_task(ctx.obj["config_path"])
        console.print("[green]Installed: Windows login task (disabled)[/green]")
        console.print("Run 'obsidian-sync enable' to start on login")
    else:
        raise click.ClickException(f"Unsupported platform: {system}")


@main.command()
@click.pass_context
def uninstall(ctx: click.Context) -> None:
    """Remove the system service."""
    system = platform.system()

    if system == "Darwin":
        _uninstall_launchd()
    elif system == "Linux":
        _uninstall_systemd()
    elif system == "Windows":
        windows_service.uninstall_task()
        console.print("[green]Service removed (Task Scheduler)[/green]")
    else:
        raise click.ClickException(f"Unsupported platform: {system}")


@main.command()
@click.pass_context
def enable(ctx: click.Context) -> None:
    """Enable service to start on login."""
    system = platform.system()

    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / LAUNCHD_PLIST_NAME
        if not plist.exists():
            raise click.ClickException("Service not installed. Run 'install' first.")
        subprocess.run(["launchctl", "load", str(plist)], check=True)
        console.print("[green]Service enabled (launchd)[/green]")
    elif system == "Linux":
        subprocess.run(["systemctl", "--user", "enable", SYSTEMD_UNIT_NAME], check=True)
        subprocess.run(["systemctl", "--user", "start", SYSTEMD_UNIT_NAME], check=True)
        console.print("[green]Service enabled (systemd)[/green]")
    elif system == "Windows":
        outcome = windows_service.enable_task()
        console.print(f"Service enabled (Task Scheduler): {outcome}")
        console.print("Startup observation does not establish sync health; check the logs.")
    else:
        raise click.ClickException(f"Unsupported platform: {system}")


@main.command()
@click.pass_context
def disable(ctx: click.Context) -> None:
    """Disable service from starting on login."""
    system = platform.system()

    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / LAUNCHD_PLIST_NAME
        if plist.exists():
            subprocess.run(["launchctl", "unload", str(plist)], check=False)
        console.print("[green]Service disabled (launchd)[/green]")
    elif system == "Linux":
        subprocess.run(["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME], check=False)
        subprocess.run(["systemctl", "--user", "disable", SYSTEMD_UNIT_NAME], check=False)
        console.print("[green]Service disabled (systemd)[/green]")
    elif system == "Windows":
        windows_service.disable_task()
        console.print("[green]Service disabled (Task Scheduler)[/green]")
    else:
        raise click.ClickException(f"Unsupported platform: {system}")


def _get_template(filename: str) -> Path:
    """Get a service template file from the bundled package data.

    Falls back to ~/.config/obsidian-sync/ if the bundled template is not found.
    """
    # Check user config dir first (allows overrides)
    home_config = Path.home() / ".config" / "obsidian-sync" / filename
    if home_config.exists():
        return home_config

    # Use bundled templates from the package
    templates = importlib.resources.files("obsidian_sync.templates")
    template_ref = templates.joinpath(filename)
    # as_file() handles both installed wheels and editable installs
    with importlib.resources.as_file(template_ref) as template_path:
        if template_path.exists():
            return template_path

    raise FileNotFoundError(f"Service template not found: {filename}")


def _remove_legacy_launchd() -> None:
    """Remove the old io.github.obsidian-sync plist if present."""
    legacy = Path.home() / "Library" / "LaunchAgents" / LEGACY_LAUNCHD_PLIST_NAME
    if legacy.exists():
        subprocess.run(["launchctl", "unload", str(legacy)], check=False)
        legacy.unlink()
        console.print(f"[yellow]Removed legacy service: {LEGACY_LAUNCHD_PLIST_NAME}[/yellow]")


def _install_launchd() -> None:
    """Install macOS LaunchAgent."""
    _remove_legacy_launchd()
    try:
        source = _get_template(LAUNCHD_PLIST_NAME)
    except FileNotFoundError as e:
        raise click.ClickException(f"Plist template not found: {LAUNCHD_PLIST_NAME}") from e
    dest_dir = Path.home() / "Library" / "LaunchAgents"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / LAUNCHD_PLIST_NAME
    rendered = source.read_text().replace("%HOME%", str(Path.home()))
    dest.write_text(rendered)
    shutil.copystat(source, dest)
    console.print(f"[green]Installed: {dest}[/green]")
    console.print("Run 'obsidian-sync enable' to start on login")


def _uninstall_launchd() -> None:
    """Remove macOS LaunchAgent."""
    _remove_legacy_launchd()
    plist = Path.home() / "Library" / "LaunchAgents" / LAUNCHD_PLIST_NAME
    if plist.exists():
        subprocess.run(["launchctl", "unload", str(plist)], check=False)
        plist.unlink()
        console.print("[green]Service removed[/green]")
    else:
        console.print("[dim]Service not installed[/dim]")


def _install_systemd() -> None:
    """Install Linux systemd user unit."""
    try:
        source = _get_template(SYSTEMD_UNIT_NAME)
    except FileNotFoundError as e:
        raise click.ClickException(f"Systemd unit not found: {SYSTEMD_UNIT_NAME}") from e
    dest_dir = Path.home() / ".config" / "systemd" / "user"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / SYSTEMD_UNIT_NAME
    shutil.copy2(source, dest)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    console.print(f"[green]Installed: {dest}[/green]")
    console.print("Run 'obsidian-sync enable' to start on login")


def _uninstall_systemd() -> None:
    """Remove Linux systemd user unit."""
    unit = Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT_NAME
    if unit.exists():
        subprocess.run(["systemctl", "--user", "stop", SYSTEMD_UNIT_NAME], check=False)
        subprocess.run(["systemctl", "--user", "disable", SYSTEMD_UNIT_NAME], check=False)
        unit.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        console.print("[green]Service removed[/green]")
    else:
        console.print("[dim]Service not installed[/dim]")
