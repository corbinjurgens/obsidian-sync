# CLI Reference

All commands accept `--config PATH` for a non-default config file and `--verbose` for debug output.

## Commands

### `obsidian-sync start`

Start the sync loop.

On Windows, `--daemon` starts a hidden child process instead of forking. For
automatic startup at login, use `install` followed by `enable`.

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

### `obsidian-sync uninstall`

Remove the system service.

On Windows, waits for cooperative shutdown before removing the task and its
installation metadata. Config, logs and vault contents are kept.

### `obsidian-sync enable`

Enable service to start on login.

On Windows, also starts it immediately. Uses the config captured by `install`.

### `obsidian-sync disable`

Disable service from starting on login.

On Windows, also requests cooperative shutdown and waits up to 30 seconds.
A timeout leaves the task disabled without forcibly terminating Git; retry
after the active sync finishes. Uses the config captured by `install`.
