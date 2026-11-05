# CLI Reference

All commands accept `--config PATH` for a non-default config file and `--verbose` for debug output.

## Commands

### `obsidian-sync start`

Start the sync loop.

On Windows, `--daemon` starts a hidden child process instead of forking. For
automatic startup at login, use `install` followed by `enable`.
Detached launch submission does not verify startup or sync health. Startup output
goes to `%LOCALAPPDATA%\obsidian-sync\windows-startup.log`.

```bash
obsidian-sync start           # foreground
obsidian-sync start --daemon  # background
```

### `obsidian-sync stop`

Stop a running daemon.

Windows uses a cooperative stop request and finishes the active sync before
exiting. Use `status` to check when it has stopped.

```bash
obsidian-sync stop
```

### `obsidian-sync status`

Show daemon status, config summary, and last log entry.

```bash
obsidian-sync status
```

### `obsidian-sync sync`

Run one sync cycle (stage, commit, pull, push), then exit.

```bash
obsidian-sync sync
```

### `obsidian-sync config`

Show, create, or edit configuration.

```bash
obsidian-sync config          # show current config
obsidian-sync config --init   # create config interactively
obsidian-sync config --edit   # open in $EDITOR
```

### `obsidian-sync log`

Show sync log entries.

```bash
obsidian-sync log             # last 50 lines
obsidian-sync log -n 100      # last 100 lines
obsidian-sync log --tail      # follow log output
```

### `obsidian-sync install`

Install as a login service (launchd on macOS, systemd on Linux, Task Scheduler
on Windows). Windows registration is per user, starts disabled, and captures
the absolute `--config` path and the current environment's `pythonw.exe`.
Runtime paths in the Windows config must be absolute after expanding `~`.
The installation also captures a schema version, installation ID and expanded
control-lock path in `~/.config/obsidian-sync/windows-task.json`. Python's `-P`
option preserves safe module lookup while keeping the selected virtual environment.
Scheduled startup requires matching metadata and valid YAML with an explicit
`vault_path`; it never substitutes default configuration when either is broken.
Changes to the captured `lock_path` require disable and reinstall. Other sync
settings remain editable in YAML.

An existing task must be disabled and positively stopped before replacement.
Registration starts disabled, followed by atomic metadata publication. A metadata
write failure leaves a disabled registration that `install` can repair.

### `obsidian-sync uninstall`

Remove the system service.

On Windows, waits for cooperative shutdown before removing the task and its
installation metadata. Config, logs and vault contents are kept.
Queued, unknown and unreadable scheduler states prevent removal. A failed
unregister operation retains metadata for retry.

### `obsidian-sync enable`

Enable service to start on login.

On Windows, also starts it immediately. Uses the config captured by `install`.
The task's description and exact action must match the installation before it is
enabled. A bounded observation reports started, failed (including the scheduler
launch result), or pending. Started confirms a live process, not successful sync.
Inspect the sync log and the stable startup diagnostic log for failures.

### `obsidian-sync disable`

Disable service from starting on login.

On Windows, also requests cooperative shutdown and waits up to 30 seconds.
A timeout leaves the task disabled without forcibly terminating Git; retry
after the active sync finishes. Uses the config captured by `install`.
Future scheduling is disabled before metadata is read, and cooperative shutdown
uses the captured control address without parsing YAML. Broken or legacy metadata
cannot redirect shutdown through an edited config: an active instance instead
reports shutdown as unverified. A stopped legacy task can be reinstalled.
Use management commands sequentially.
