# Changelog

Keep dated entries as verification history. Record implementation separately from deployment and unexecuted checks.

## Unreleased

No further implementation changes recorded.

## 2026-11-07

Implementation range: the three commits after `6bd3dcd74ad49cba71278ab285efd5b4e6f80912`.

### What changed

- Removed the wheel `force-include` that duplicated the packaged templates, so plain builds work with current Hatchling.
- Made the PID and CLI route tests independent of the host platform. The check that PID probing sends no signal is now Windows-only.
- Registered the Windows task principal by qualified account name instead of SID. The logon trigger and task name keep the SID.

### What worked

On Windows with Python 3.13.15, the full suite passed with 333 tests and the opt-in scheduler tests skipped. The separate non-elevated scheduler run passed its 3 tests. An unconstrained wheel and scratch `uv tool install` contain both service templates. A probe task registered with the qualified principal exported the same principal SID and trigger account as one registered by SID.

### What did not or remains open

- The principal repair is unverified on Joes, the only host that showed the rejection. The same `(17,8):UserId` error was reproduced elsewhere only by naming the computer as the principal.
- The macOS suite has not been re-run. The six recorded failures were only emulated on Windows.
- `test_windows_background_lifecycle` missed its 10-second lock wait once in a slow full-suite run, then passed alone three times and in the final full run.

## 2026-11-06

Implementation range: `a7fedfe342721e676fa5d82b8e9b9ad31a2a55c6..634838b6a2d53ed87d58f19b13c956139bd8415e`. This range covers obsidian-sync repository changes; vault tracking notes are separate evidence.

### What changed

- Bound installed Windows startup and control to a versioned identity, strict configuration and captured lock address. Added stable diagnostics and conservative scheduler-state handling.
- Refused unfinished Git operations before mutation, honored the selected pull strategy and retried retained commits against the fetched branch tip, including narrow fetch configurations.
- Kept Git failures inside the daemon loop with quiet exponential backoff, capped at 15 minutes or the configured interval when longer. Manual resolution uses the existing Obsidian Git workflow.
- Added real scheduled Git shutdown tests and a Python 3.11/3.13 Windows/Linux/macOS CI matrix.

### What worked

On Windows with Python 3.13.15, the full suite passed with 332 tests and the opt-in scheduler tests skipped. The separate non-elevated scheduler run passed its lifecycle, startup-failure and blocked-push tests. An active push finished after cooperative shutdown was requested, delivered the exact note and commit, and exited before another cycle. Other task definitions were unchanged.

### What did not or remains open

Remote CI runtime was not observed after push. Actual next-login triggering, authenticated Git transport, a specially named real interpreter installation and an actually Obsidian-free desktop require separate evidence. The Git scheduler fixture overrides only process detection while the real app stays open. Real-vault deployment is separate; the existing single-writer and app/plugin startup-overlap restrictions remain.

Accepted recovery boundaries: if startup and compensating disable both fail, scheduling is reported as unverified with metadata retained. If uninstall removes the task but metadata deletion fails, cleanup reports failure and allows retry. Neither outcome silently claims complete cleanup or forcibly terminates Git.

### Verification

- `uv run pytest tests -q -n 4`: 332 passed, 3 opt-in skipped.
- `OBSIDIAN_SYNC_TEST_TASKS=1 uv run pytest tests/test_windows_task_integration.py -q -s -ra`: 3 passed.
- Source Ruff lint/format, ty, strict MkDocs build and CLI help checks passed.
- Independent whole-diff review confirmed the supplied behavioral claims. The user explicitly accepted the additional uninstall metadata-cleanup outcome.
- Source snapshot revalidation confirmed the checked inputs were unchanged at the implementation commit. The changelog commit is separate bookkeeping outside the implementation range.
