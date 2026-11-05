# obsidian-sync

Headless git sync daemon that keeps your Obsidian vault backed up to a git remote when the app is closed. Matches the Obsidian Git plugin's strategy (rebase, pull-before-push, hostname-tagged commits) and automatically defers when Obsidian is running to avoid conflicts.

## Installation

```bash
# With uv (recommended)
uv tool install obsidian-sync

# With pipx
pipx install obsidian-sync
```

Requires Python 3.11+ and `git` on your PATH.

## Quick Start

Start with one manual sync before installing a daemon or login service:

```bash
# Create a config file interactively.
# Writes ~/.config/obsidian-sync/config.yaml
obsidian-sync config --init

# Run a single sync cycle, then exit
obsidian-sync sync

# Check daemon status and the last log entry
obsidian-sync status
```

If the one-off sync looks good, run the loop in the foreground while testing:

```bash
obsidian-sync start
```

Then choose background mode or service setup:

```bash
obsidian-sync start --daemon
```

## Commands

| Workflow | Command |
|----------|---------|
| Create or inspect config | `obsidian-sync config --init`, `obsidian-sync config`, `obsidian-sync config --edit` |
| Verify a one-off sync | `obsidian-sync sync`, then `obsidian-sync status` |
| Run the loop yourself | `obsidian-sync start` or `obsidian-sync start --daemon` |
| Manage the daemon/service | `obsidian-sync stop`, `install`, `enable`, `disable`, `uninstall`, `log` |

All commands accept `--config PATH` to use a non-default config file and `--verbose` for debug output. See the [CLI reference](docs/reference/cli.md) for the full command table.

## Service Setup

### Windows (Task Scheduler)

```powershell
obsidian-sync install   # registers a disabled task for the current user
obsidian-sync enable    # starts now and at subsequent logins
```

The task runs without a console window, using `pythonw.exe` from the Python
environment that installed it. It uses your logged-in account and Git credentials;
no administrator account or stored Windows password is required. Git must be on
your user PATH and authenticated before enabling the task. It runs while you are
logged in, including when Obsidian is closed, but cannot sync while the machine
is asleep or you are logged out.

The configuration stays at `~/.config/obsidian-sync/config.yaml` by default.
Vault, log and lock paths must be absolute after expanding `~`. A custom config
is captured when installing:

```powershell
obsidian-sync --config 'C:\Users\me\sync config.yaml' install
obsidian-sync enable
```

There is one installed task per Windows account. `enable`, `disable` and
`uninstall` manage the configuration selected at installation, even when
`--config` is omitted. Other commands, including `status`, `stop` and `log`, need
the same `--config` when you use a custom file.

To remove:

```powershell
obsidian-sync disable
obsidian-sync uninstall
```

Disabling prevents future starts and requests shutdown after the current sync.
If it takes longer than 30 seconds, the command reports that the task is disabled
but shutdown is still pending. Let it finish and retry; Git is not forcibly
terminated. Uninstall leaves your vault, config and logs intact.

After moving or replacing the installed Python environment, disable the task,
then run `install` and `enable` again to update its interpreter path.

### macOS (launchd)

```bash
obsidian-sync install   # copies plist to ~/Library/LaunchAgents/
obsidian-sync enable    # loads the service
```

To remove:

```bash
obsidian-sync disable
obsidian-sync uninstall
```

### Linux (systemd)

```bash
obsidian-sync install   # copies unit to ~/.config/systemd/user/
obsidian-sync enable    # enables and starts the service
```

To remove:

```bash
obsidian-sync disable
obsidian-sync uninstall
```

## Configuration

`obsidian-sync config --init` creates `~/.config/obsidian-sync/config.yaml` interactively. The key values are the vault path, sync interval, pull strategy, remote, branch, commit template, log path, and lock path.

See [Configuration](docs/configuration.md) for the full example, option table, and commit-template variables.

## Behavior

- **Auto-commit**: Stages all changes and commits with a timestamped, hostname-tagged message
- **Pull before push**: Pulls with rebase (by default) to keep history linear
- **Obsidian detection**: Automatically defers syncing while Obsidian is open to avoid conflicts with the Obsidian Git plugin
- **Lock file**: Uses a PID lock to prevent duplicate daemons
- **Conflict handling**: Stops the daemon on merge conflicts (manual resolution required)

Obsidian detection is app-wide and checked before each sync cycle. It does not
coordinate an atomic handoff with the Obsidian Git plugin: opening Obsidian while
a cycle is already running can overlap their Git operations. If you require a
guaranteed handoff, stop the daemon and wait for it to exit before opening
Obsidian. Match `sync.pull_strategy` to the strategy you use in the plugin.

## Development

```powershell
uv sync --all-extras
uv run pytest tests
uv run ruff check src
uv run ruff format --check src
uv run ty check src
```

To smoke-test a real Windows login task, opt in explicitly. This creates and
removes a uniquely named test task, with all configuration and logs in pytest's
temporary directory. It does not use your vault or installed task.

```powershell
$env:OBSIDIAN_SYNC_TEST_TASKS = '1'
uv run pytest tests/test_windows_task_integration.py
Remove-Item Env:OBSIDIAN_SYNC_TEST_TASKS
```

## License

MIT
