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

## License

MIT
