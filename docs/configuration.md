# Configuration

Config file: `~/.config/obsidian-sync/config.yaml`

Create interactively with `obsidian-sync config --init`, or write manually.

## Full example

```yaml
vault_path: ~/obsidian-vault
sync:
  interval_seconds: 60
  pull_strategy: rebase
  remote: origin
  branch: main
  backoff_initial_seconds: 0
  backoff_multiplier: 2.0
  backoff_max_seconds: 900
commit:
  template: "{{date}} — Auto-sync completed [host={{hostname}}] [files={{numFiles}}] [changed={{files}}]"
  date_format: "%Y-%m-%d %H:%M:%S"
  list_files_in_body: true
log:
  path: ~/.local/state/obsidian-sync/sync.log
  max_size_mb: 10
lock_path: ~/.local/state/obsidian-sync/daemon.lock
```

## Options

| Setting | Default | Description |
|---------|---------|-------------|
| `vault_path` | `~/obsidian-vault` | Path to your Obsidian vault git repo |
| `sync.interval_seconds` | `60` | Seconds between sync cycles |
| `sync.pull_strategy` | `rebase` | Git pull strategy: `rebase`, `merge`, or `ff-only` |
| `sync.remote` | `origin` | Git remote name |
| `sync.branch` | `main` | Git branch name |
| `sync.backoff_initial_seconds` | `0` | First retry delay after a failed cycle; `0` uses `sync.interval_seconds` |
| `sync.backoff_multiplier` | `2.0` | Factor applied to the delay after each consecutive failure; `1` keeps it constant |
| `sync.backoff_max_seconds` | `900` | Longest retry delay; the sync interval applies when it is longer |
| `commit.template` | *(see above)* | Commit message template |
| `commit.date_format` | `%Y-%m-%d %H:%M:%S` | strftime format for `{{date}}` |
| `commit.list_files_in_body` | `true` | Include changed file list in commit body |
| `log.path` | `~/.local/state/obsidian-sync/sync.log` | Log file location |
| `log.max_size_mb` | `10` | Max log size before rotation |
| `lock_path` | `~/.local/state/obsidian-sync/daemon.lock` | PID lock file location |

## Template variables

The commit template supports these variables:

| Variable | Value |
|----------|-------|
| `{{date}}` | Current date/time formatted with `date_format` |
| `{{hostname}}` | Machine hostname |
| `{{numFiles}}` | Number of changed files |
| `{{files}}` | Comma-separated list of changed file paths (abbreviated) |
