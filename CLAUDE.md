# obsidian-sync

Headless git sync daemon for Obsidian vaults.

## Commands

| Command | What |
|---------|------|
| `mise run setup` | Install dependencies |
| `mise run check` | All checks (lint + format + ty + test) |
| `mise run test` | Unit tests with coverage |
| `mise run lint` | Ruff linter |
| `mise run format` | Ruff formatter |
| `mise run ty` | Type check |
| `mise run install` | Install CLI via uv tool |

Single test: `uv run -m pytest tests/path::func`

Plugin tests and vault deployment: `npm test` and `npm run install:vault` from `plugin/`.

## Architecture

Click CLI -> daemon with PID lock + signal handling -> git ops (pull/commit/push) on a timer. Config via YAML at `~/.config/obsidian-sync/config.yaml`. Service templates for launchd (macOS) and systemd (Linux) bundled as package data.

## Key Files

| File | Purpose |
|------|---------|
| `src/obsidian_sync/cli.py` | Click CLI entry point |
| `src/obsidian_sync/daemon.py` | Sync loop, PID management, daemonize |
| `src/obsidian_sync/git_ops.py` | Git primitives (pull, push, commit) |
| `src/obsidian_sync/config.py` | YAML config with dataclass defaults |
| `src/obsidian_sync/template.py` | Commit message rendering |
| `plugin/main.js`, `plugin/lib/` | Obsidian plugin that manages the companion |

## Style

- Python 3.11+, strict typing
- Ruff for lint + format
- ty for type checking
- Click for CLI
