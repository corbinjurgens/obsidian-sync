# Execution plan: Obsidian sync Windows lifecycle cleanup

## Goal
Make the existing Windows port ready for a controlled real-vault trial: installed commands address the correct daemon despite configuration damage, hidden failures are observable, and Git cannot publish unresolved conflict content or silently disregard its selected strategy. Keep the existing daemon and per-user Task Scheduler architecture. This plan has not been executed.

## Change scope
Intent: Close verified lifecycle and data-preservation defects found while reviewing Windows support.
Scope: Bounded improvement to Windows installation/control, with focused corrections in the existing Git engine.
Basis: The user chose the upstream daemon and requested an independent assessment. This is the proposed cleanup derived from the checked response, not authorization to deploy.
Compatibility: Preserve Python >=3.11, foreground commands, macOS/Linux service owners, configured vault/remote/branch/strategy, note contents, and user-account Git credentials. Foreground missing-config fallback stays; installed startup becomes strict.
Non-goals: Plugin forks, an atomic Obsidian handoff, a Windows system service, another sync backend, auto-restart, a watchdog, and support for concurrent management commands or independent Git writers.

## Context the executor needs
Reviewed source: `a50e2f3804eee98d794d357173dd337d58d4a5c0` on `main`. Upstream base: `864658b39744c760f8f79ca41a7e9ececc4f89d1`. Inspect current instructions/status and subsequent changes before editing. Preserve unrelated work and do not create a branch automatically.

`windows_service.py` records only `config_path`. `disable_task()` reloads YAML before disabling scheduling. Missing YAML falls back to defaults, malformed YAML aborts control, and editing its lock path redirects the actual stop request. Failure after task registration can leave missing metadata that blocks disable. `_task_running()` recognizes only `Running`; lookup errors are suppressed.

`daemon.py` owns the PID file, detection, stop sidecar and hidden child. Escaping exceptions lack cause logging. psutil per-process attribute denial can return `name=None` without reaching the outer exception handler.

Disposable probes reproduced these findings at the reviewed source. Real Git probes also confirmed that a second merge-sync after conflict pushes conflict markers, clean `ff-only` cycles skip a failed push retry, and Git defaults override selected merge mode. The identical Git probes reproduced against archived upstream source, establishing that those defects predate the port.

Evidence/adjudication: the owning Obsidian project's `Investigations/2026-11-05 Windows support review.md` and `Investigations/2026-11-05 Windows review evidence/`. Original response: `.claude/research-prompts/2026-11-05-windows-support-review/response.md`. Evidence probes assert that defects exist; implement regression tests asserting corrected behavior.

Windows Python 3.13.15 and `pythonw.exe` are available. Earlier successful launch tests used missing vaults, so they did not establish scheduled Git behavior. The current shell was verified non-elevated. Actual next-login startup, authenticated transport and Unix runtime execution require separate evidence. At planning time the project has no blocked/deferred/shelved records; its app/plugin overlap risk remains open.

## Decisions (settled — do not revisit)
- Keep Task Scheduler, the separate Windows module, psutil and cooperative stop requests. Their ownership matches the existing per-user hidden-process requirements.
- Use the installing Python environment and user credentials. Store control state and diagnostics outside the synced vault.
- Preserve active Git work during stop/disable/uninstall. Timeout means shutdown pending, with recovery information retained.
- Preserve app-wide Obsidian detection and its documented startup window. The plugin does not participate in the daemon's lock.

## Decisions made while writing this plan
- Include the reproduced upstream conflict/strategy corrections before a real-vault trial because they affect note preservation and selected behavior.
- Bind installed control through one versioned record and a matching installation ID in the task action. Avoid a journal and a duplicate copy of all YAML settings.
- Keep management sequential and retain a single-writer operating restriction. The existing PID file is not a concurrency guarantee.
- Treat unreadable process identity conservatively, with a non-elevated acceptance check that normal operation does not permanently defer.

## Tasks

### Task 1 — Bind installed Windows control and failure reporting
**Files:** `src/obsidian_sync/windows_service.py`, `src/obsidian_sync/cli.py`, `src/obsidian_sync/daemon.py`, `src/obsidian_sync/config.py`, `tests/test_windows_service.py`, `tests/test_windows_daemon.py`, `tests/test_windows_task_integration.py`, `tests/test_cli.py`, `README.md`, `docs/reference/cli.md`.
**Depends on:** none
**Complexity:** judgment
**Do:**

1. Extend installation metadata with a schema version, installation ID, absolute selected config path, expanded control-lock path and absolute interpreter path. Preserve virtual-environment interpreter spelling instead of resolving a symlink to another interpreter. Derive one stable machine-local diagnostic path independently of mutable YAML and metadata parsing.
2. Add an installed-start CLI entry point carrying the installation ID, invoked with Python `-P`. Initialize diagnostics before parsing installed/config data. Require matching valid metadata and an existing valid YAML. Reject changes to the captured control-lock path until reinstall. Keep ordinary sync settings in YAML; never fall back to another vault during installed startup.
3. Return explicit task existence, enabled status, operational state and launch result. Successfully enumerate the task root and filter the exact SID-derived name; query failure remains an error. Treat `Queued`, `Unknown` and unexpected output as pending/error. Check owned description and matching installation action. Remove the task's `Hidden` setting; `pythonw.exe` remains windowless.
4. Disable future scheduling before reading metadata/configuration for shutdown. Use the captured control address without reloading YAML. Broken or legacy metadata must still allow disabling future starts; report shutdown unverified when an active instance cannot safely be addressed. Require absent registration or disabled, positively quiescent registration before replacing/removing it. Recheck benign process exit after a failed stop request.
5. Register disabled, then atomically publish matching metadata. Refuse enable on task/record mismatch. Metadata failure must leave a disabled, repairable installation, without a journal. Replace stopped legacy installations; do not guess an active legacy instance's control path from edited YAML.
6. Log escaping daemon exceptions with traceback context and early installed-start failures to stable diagnostics. Expose scheduler launch results when interpreter/package startup fails. Use bounded startup observation to report started, failed or pending distinctly. Add `-P` and an error sink to detached Windows launch; submitting a launch is not evidence of sync health.
7. Handle sidecar parsing/I/O failures explicitly, publish complete requests atomically, keep Windows sidecar cleanup Windows-only, and pass remaining stop time into scheduler queries. Never force-kill Git after timeout. Distinguish disappeared processes from denied names: skip disappeared processes; diagnose and defer unresolved identity. Verify the latter policy on a non-elevated desktop.
8. Add regression tests covering missing/malformed/edited YAML, partial installation, broken/legacy metadata, task-state/query failures, mismatched records, delayed PID publication followed by a stop request, early failures and sidecar corruption. Update lifecycle documentation with this task.

**Verify:** Run `uv run pytest tests/test_windows_service.py tests/test_windows_daemon.py tests/test_windows_task_integration.py tests/test_cli.py tests/test_config.py` using external temporary paths. Assert no fallback Git action, wrong-path stop, removal under uncertain state, or enabled inconsistent installation. Repeat the opt-in disposable scheduler test from a recorded non-elevated shell. Ruff and ty must pass. Keep the real vault out of the tests.

### Task 2 — Preserve unresolved Git operations across restarts
**Files:** `src/obsidian_sync/git_ops.py`, `src/obsidian_sync/daemon.py`, `tests/test_git_ops.py`, `tests/test_daemon.py`, `README.md`.
**Depends on:** 1
**Complexity:** judgment
**Do:** Add a preflight before staging/commit in both strategies. Refuse unmerged index entries and active merge, rebase, cherry-pick, revert and sequencer state, using Git-resolved paths including worktrees. A failed query must refuse mutation. Report manual intervention and stop the loop; never abort, reset or finish the operation automatically. Add real-Git regressions comparing files, index, HEAD and remote before/after a second invocation against unresolved state, including a staged but unfinished operation. Keep this responsibility in Git/daemon helpers.
**Verify:** `uv run pytest tests/test_git_ops.py tests/test_daemon.py`. The second invocation performs no staging, commit or push, leaves unresolved content/index unchanged, and reports intervention. Query failure also performs no mutation. Normal dirty and clean repositories still sync.

### Task 3 — Honor selected Git strategy and retry unsent commits
**Files:** `src/obsidian_sync/git_ops.py`, `src/obsidian_sync/daemon.py`, `tests/test_git_ops.py`, `tests/test_daemon.py`, `tests/test_smoke.py`, `README.md`.
**Depends on:** 2
**Complexity:** judgment
**Do:** Explicitly select merge with `--no-rebase --ff`, including divergent history despite opposing `pull.rebase`/`pull.ff` defaults. Keep `ff-only` explicit. Push outstanding commits in the pull-first path even without fresh edits, reusing the existing ahead/push primitives and preserving pull-before-push. Add real-repository tests for opposing defaults and failed-push retry that compare local/remote tips. Preserve the selected strategy and remote.
**Verify:** `uv run pytest tests/test_git_ops.py tests/test_daemon.py tests/test_smoke.py tests/test_e2e.py`. Divergent selected-merge history produces a merge commit despite `pull.rebase=true` and `pull.ff=only`. A clean retry sends the previously failed commit; it cannot report success while leaving that reproduced commit stranded locally.

### Task 4 — Verify real scheduled Git and platform compatibility
**Files:** `tests/test_windows_task_integration.py`, `tests/test_windows_daemon.py`, `.github/workflows/tests.yml`, `README.md`, `docs/reference/cli.md`.
**Depends on:** 3
**Complexity:** judgment
**Do:** Extend opt-in scheduled tests to use a disposable vault and bare remote. Gate a real push with a controllable receive hook. Request disable during the push; timeout must preserve the disabled registration and metadata while Git stays alive. Release the gate, verify remote delivery and daemon exit, then retry uninstall. Use a unique test task and release its own gate in finally before cleanup. Exercise early startup failure through real launch reporting. Add runtime CI coverage for Python 3.11 and 3.13 on Windows, Linux and macOS. Keep task registration opt-in. Update acceptance documentation with actual evidence and explicit unexecuted checks.
**Verify:** In a recorded non-elevated Windows shell set `OBSIDIAN_SYNC_TEST_TASKS=1` and run scheduled tests. Demonstrate real push completion despite pending shutdown, no subsequent cycle, and removal of only the disposable task. Run the complete suite, Ruff, ty and strict docs build. Require successful stated-platform CI runs before claiming Unix runtime coverage. Actual next-login triggering, authenticated transport and a real Unicode/apostrophe interpreter installation each need separate evidence; manual scheduled start does not establish them.

## Verification (whole-plan)
Local prerequisites: persistent Python environment with `pythonw.exe`, Git on user PATH, Windows PowerShell/ScheduledTasks, and a writable explicit temporary directory outside repositories and synced notes. Check these first; use `uv sync --all-extras` for repository dependencies. Never test against the owner's vault.

Run `uv run pytest tests` with external `--basetemp` and pytest cache. Set `COVERAGE_FILE` and report destinations outside the clone when requesting coverage. Run `uv run ruff check src`, `uv run ruff format --check src`, `uv run ty check src`, and `uv run mkdocs build --strict --site-dir` with an absolute temporary output directory. Passing means successful process exit and task-specific behavioral assertions; do not freeze a predicted test total.

Unix runtime CI, actual login, authenticated Git and a specially named real interpreter require their stated environments. Record missing evidence explicitly. Type checks, mocked quoting and a manually started task do not replace those checks. Real-vault deployment is a separate action.

## Do not
Do not refactor beyond what the tasks specify. Do not sync the real vault, force-stop Git, discard/abort unresolved state, change credentials/strategy, add auto-restart, or expand into plugin coordination. Do not create branches automatically. Stage explicit changed paths only and commit coherent units in their owning ordinary repository. Respect the vault's automatic recording policy.

## If stuck
Retry setup only after identifying the missing executable/path. Preserve a disposable task's unique name, config and control paths if cleanup cannot finish; report it and never touch another session's task. If unknown identities permanently defer normal desktop sync, establish a permission-aware policy with executed evidence before closing task 1. Uncertain scheduler state leaves scheduling disabled and shutdown pending. Keep unavailable external acceptance evidence open in project records. After commits, report local/origin identifiers.

## Note lifecycle

This file is working state in a repository, not documentation.

- When an item lands, append ` — done <sha>` to its heading, where
  `<sha>` is the commit that landed it. An item consciously dropped gets
  ` — dropped` and a record published with
  `project-note deferred --title "..." --description "..."`.
- When every item carries one of those marks, delete this file in the
  commit that lands the last item. Nothing in it is kept: the commits and
  the run archive are the evidence, and
  `project-note done --title "..." --description "..."` records the completion.
- In an execute run the orchestrator does this after each unit lands,
  not the unit. A plain session does it as each item lands.
