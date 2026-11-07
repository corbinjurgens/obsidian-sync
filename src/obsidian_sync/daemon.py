"""Sync daemon with loop, lock file, signal handling, and Obsidian detection."""

import csv
import io
import logging
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from obsidian_sync.config import DEFAULT_CONFIG_PATH, SyncConfig, expand_paths
from obsidian_sync.git_ops import (
    GitError,
    GitInterventionError,
    PullConflictError,
    PushError,
    add_all,
    commit,
    ensure_sync_ready,
    get_changed_files,
    is_ahead,
    is_dirty,
    pull,
    push,
)
from obsidian_sync.template import CommitContext, render_commit_message

logger = logging.getLogger("obsidian-sync")

OBSIDIAN_PROCESS_NAME_MACOS = "Obsidian"
OBSIDIAN_PROCESS_NAME_LINUX = "obsidian"


@dataclass
class SyncResult:
    """Result of a single sync cycle."""

    synced: bool
    files_changed: int
    message: str
    deferred: bool = False
    error: str | None = None
    intervention_required: bool = False


class LockError(Exception):
    """Raised when the daemon lock cannot be acquired."""


class SyncDaemon:
    """Headless git sync daemon for Obsidian vaults."""

    def __init__(self, config: SyncConfig) -> None:
        self.config = expand_paths(config)
        self._running = False
        self._lock_acquired = False

    def run(self) -> None:
        """Run the sync loop until stopped by signal."""
        self._setup_signals()
        self._acquire_lock()
        self._running = True
        logger.info(
            "Daemon started (vault=%s, interval=%ds)",
            self.config.vault_path,
            self.config.sync.interval_seconds,
        )
        interval = self.config.sync.interval_seconds
        max_retry_delay = max(interval, self.config.sync.backoff_max_seconds)
        first_retry_delay = min(
            self.config.sync.backoff_initial_seconds or interval, max_retry_delay
        )
        retry_delay = first_retry_delay
        try:
            while self._running and not self._stop_requested():
                result = self.sync_cycle()
                if result.deferred:
                    delay = interval
                elif result.synced:
                    retry_delay = first_retry_delay
                    delay = interval
                else:
                    delay = retry_delay
                    retry_delay = min(
                        int(retry_delay * self.config.sync.backoff_multiplier), max_retry_delay
                    )
                logger.info("Next sync in %d seconds", delay)
                self._sleep(delay)
        except Exception:
            logger.exception("Daemon failed")
            raise
        finally:
            self._release_lock()
            self._running = False
            logger.info("Daemon stopped")

    def run_once(self) -> SyncResult:
        """Run a single sync cycle, then return."""
        return self.sync_cycle()

    def sync_cycle(self) -> SyncResult:
        """Execute one sync cycle.

        For rebase/merge strategies: commit → pull → push.  Committing first
        lets ``git pull --rebase`` cleanly replay the local commit on top of
        remote changes.  Without committing first, pull fails with "cannot
        pull with rebase: You have unstaged changes" when dirty files overlap
        with remote.

        For ff-only strategy: pull → commit → push.  Creating a local commit
        before pull would make histories diverge, causing ff-only to fail
        whenever the remote also has new commits.
        """
        vault = Path(self.config.vault_path)
        if not vault.exists():
            msg = f"Vault path does not exist: {vault}"
            logger.error(msg)
            return SyncResult(synced=False, files_changed=0, message=msg, error=msg)

        if self._is_obsidian_running():
            msg = "Deferring to Obsidian (app is running)"
            logger.info(msg)
            return SyncResult(synced=False, files_changed=0, message=msg, deferred=True)

        try:
            ensure_sync_ready(vault)
        except GitInterventionError as e:
            msg = f"Sync refused; manual intervention required: {e}"
            logger.error(msg)
            return SyncResult(
                synced=False, files_changed=0, message=msg, error=msg, intervention_required=True
            )

        # ff-only cannot have local commits before pull (would break fast-forward),
        # so it uses the original pull-first order.
        try:
            if self.config.sync.pull_strategy == "ff-only":
                return self._sync_pull_first(vault)
            return self._sync_commit_first(vault)
        except GitError as e:
            msg = f"Sync failed (will retry): {e}"
            logger.warning(msg)
            return SyncResult(synced=False, files_changed=0, message=msg, error=msg)

    def _sync_commit_first(self, vault: Path) -> SyncResult:
        """Commit → pull → push.  Used for rebase and merge strategies."""
        # Phase 1: Commit local changes (if any)
        committed = False
        changed_files: list[str] = []
        if is_dirty(vault):
            changed_files = get_changed_files(vault)
            add_all(vault)
            commit_msg, body = self._build_commit(changed_files)
            commit(vault, commit_msg, body=body)
            logger.info("Committed: %s", commit_msg)
            committed = True

        # Phase 2: Pull remote changes (rebase replays local commit on top)
        try:
            pull_result = pull(
                vault,
                self.config.sync.remote,
                self.config.sync.branch,
                self.config.sync.pull_strategy,
            )
            logger.info("Pull: %s", pull_result.message)
        except PullConflictError as e:
            msg = f"Pull conflict: {e}"
            logger.error(msg)
            return SyncResult(
                synced=False, files_changed=0, message=msg, error=msg, intervention_required=True
            )
        except GitError as e:
            msg = f"Pull failed (will retry): {e}"
            logger.warning(msg)
            return SyncResult(synced=False, files_changed=0, message=msg, error=msg)

        # Phase 3: Push if local is ahead of remote (covers both fresh commits
        # and previously-committed-but-not-pushed changes from failed pulls)
        # Explicit pull always writes FETCH_HEAD, even with narrow/custom fetch refspecs.
        needs_push = committed or is_ahead(vault, ref="FETCH_HEAD")
        if not needs_push:
            msg = "Working tree clean, nothing to sync"
            logger.info(msg)
            return SyncResult(synced=True, files_changed=0, message=msg)

        try:
            push(vault, self.config.sync.remote, self.config.sync.branch)
            logger.info("Pushed to %s/%s", self.config.sync.remote, self.config.sync.branch)
        except PushError as e:
            msg = f"Push failed: {e}"
            logger.error(msg)
            return SyncResult(
                synced=False, files_changed=len(changed_files), message=msg, error=msg
            )

        msg = f"Synced {len(changed_files)} file(s)"
        logger.info(msg)
        return SyncResult(synced=True, files_changed=len(changed_files), message=msg)

    def _sync_pull_first(self, vault: Path) -> SyncResult:
        """Pull → commit → push.  Used for ff-only strategy."""
        try:
            pull_result = pull(
                vault,
                self.config.sync.remote,
                self.config.sync.branch,
                self.config.sync.pull_strategy,
            )
            logger.info("Pull: %s", pull_result.message)
        except PullConflictError as e:
            msg = f"Pull conflict: {e}"
            logger.error(msg)
            return SyncResult(
                synced=False, files_changed=0, message=msg, error=msg, intervention_required=True
            )
        except GitError as e:
            msg = f"Pull failed (will retry): {e}"
            logger.warning(msg)
            return SyncResult(synced=False, files_changed=0, message=msg, error=msg)

        committed = False
        changed_files: list[str] = []
        if is_dirty(vault):
            changed_files = get_changed_files(vault)
            add_all(vault)
            commit_msg, body = self._build_commit(changed_files)
            commit(vault, commit_msg, body=body)
            logger.info("Committed: %s", commit_msg)
            committed = True

        needs_push = committed or is_ahead(vault, ref="FETCH_HEAD")
        if not needs_push:
            msg = "Working tree clean, nothing to sync"
            logger.info(msg)
            return SyncResult(synced=True, files_changed=0, message=msg)

        try:
            push(vault, self.config.sync.remote, self.config.sync.branch)
            logger.info("Pushed to %s/%s", self.config.sync.remote, self.config.sync.branch)
        except PushError as e:
            msg = f"Push failed: {e}"
            logger.error(msg)
            return SyncResult(
                synced=False, files_changed=len(changed_files), message=msg, error=msg
            )

        msg = f"Synced {len(changed_files)} file(s)"
        logger.info(msg)
        return SyncResult(synced=True, files_changed=len(changed_files), message=msg)

    def _build_commit(self, changed_files: list[str]) -> tuple[str, str | None]:
        """Build commit message and body from changed files."""
        context = CommitContext(
            changed_files=changed_files,
            date_format=self.config.commit.date_format,
        )
        commit_msg = render_commit_message(self.config.commit.template, context)
        body = None
        if self.config.commit.list_files_in_body and changed_files:
            body = "\n".join(changed_files)
        return commit_msg, body

    def _is_obsidian_running(self) -> bool:
        """Check if Obsidian is currently running."""
        if platform.system() == "Windows":
            try:
                # tasklist can name protected system processes that psutil cannot.
                # Enumerate all images to avoid localized "no matching tasks" text.
                executable = (
                    Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tasklist.exe"
                )
                result = subprocess.run(
                    [str(executable), "/FO", "CSV", "/NH"],
                    capture_output=True,
                    encoding="oem",
                    errors="strict",
                    check=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=10,
                )
                if result.stderr.strip():
                    raise ValueError(f"tasklist diagnostics: {result.stderr.strip()}")
                rows = [row for row in csv.reader(io.StringIO(result.stdout), strict=True) if row]
                if not rows or any(
                    len(row) != 5
                    or not row[0].strip()
                    or not row[1].isascii()
                    or not row[1].isdigit()
                    for row in rows
                ):
                    raise ValueError("tasklist returned an incomplete process snapshot")
                return any(row[0].casefold() == "obsidian.exe" for row in rows)
            except (OSError, subprocess.SubprocessError, UnicodeError, ValueError, csv.Error) as e:
                logger.warning("Cannot check Obsidian processes; deferring sync: %s", e)
                return True
        if platform.system() == "Darwin":
            name = OBSIDIAN_PROCESS_NAME_MACOS
        else:
            name = OBSIDIAN_PROCESS_NAME_LINUX
        try:
            result = subprocess.run(
                ["pgrep", "-x", name],
                capture_output=True,
                text=True,
            )
            return result.returncode == 0
        except FileNotFoundError:
            return False

    def _acquire_lock(self) -> None:
        """Acquire the PID-based lock file. Raises LockError if already held."""
        lock_path = Path(self.config.lock_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        if lock_path.exists():
            try:
                existing_pid = int(lock_path.read_text().strip())
                if _is_pid_alive(existing_pid):
                    raise LockError(f"Another daemon is running (PID {existing_pid})")
                logger.warning("Removing stale lock file (PID %d no longer running)", existing_pid)
            except (ValueError, OSError):
                logger.warning("Removing invalid lock file")

        lock_path.write_text(str(os.getpid()))
        self._lock_acquired = True

    def _release_lock(self) -> None:
        """Release the lock file if we hold it."""
        if not self._lock_acquired:
            return
        lock_path = Path(self.config.lock_path)
        try:
            if lock_path.exists():
                stored_pid = int(lock_path.read_text().strip())
                if stored_pid == os.getpid():
                    lock_path.unlink()
        except (ValueError, OSError) as e:
            logger.warning("Error releasing lock: %s", e)
        if platform.system() == "Windows":
            try:
                if self._stop_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                    self._stop_path.unlink()
            except FileNotFoundError:
                pass
            except (OSError, UnicodeError) as e:
                logger.warning("Error removing stop request: %s", e)
        self._lock_acquired = False

    @property
    def _stop_path(self) -> Path:
        """Machine-local request file for cooperative shutdown on Windows."""
        return Path(f"{self.config.lock_path}.stop")

    def _stop_requested(self) -> bool:
        if platform.system() != "Windows":
            return False
        try:
            pid = int(self._stop_path.read_text(encoding="ascii").strip())
            if pid <= 0:
                raise ValueError("nonpositive PID")
            return pid == os.getpid()
        except FileNotFoundError:
            return False
        except (OSError, ValueError, UnicodeError) as e:
            raise LockError(f"Cannot read stop request {self._stop_path}: {e}") from e

    def _setup_signals(self) -> None:
        """Register signal handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, _frame: object) -> None:
        """Handle shutdown signals gracefully."""
        sig_name = signal.Signals(signum).name
        logger.info("Received %s, shutting down after current cycle", sig_name)
        self._running = False

    def _sleep(self, seconds: int) -> None:
        """Sleep in short intervals to allow signal handling."""
        end_time = time.monotonic() + seconds
        while self._running and not self._stop_requested() and time.monotonic() < end_time:
            time.sleep(min(1.0, end_time - time.monotonic()))

    def stop(self) -> bool:
        """Request shutdown after the current sync cycle finishes."""
        lock_path = Path(self.config.lock_path)
        if not lock_path.exists():
            return False
        try:
            pid = int(lock_path.read_text().strip())
            if not _is_pid_alive(pid):
                return False
            if platform.system() == "Windows":
                temporary: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", encoding="ascii", dir=lock_path.parent, delete=False
                    ) as stream:
                        temporary = Path(stream.name)
                        stream.write(str(pid))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self._stop_path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
                return True
            os.kill(pid, signal.SIGTERM)
            return True
        except (ValueError, OSError, UnicodeError) as e:
            logger.warning("Cannot request daemon shutdown: %s", e)
            return False


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    return pid > 0 and psutil.pid_exists(pid)


def setup_logging(log_path: str, verbose: bool = False) -> None:
    """Configure logging for the daemon."""
    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if verbose:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def daemonize(config_path: Path | None = None, verbose: bool = False) -> None:
    """Detach from the terminal using a hidden child on Windows or a Unix fork."""
    if sys.platform == "win32":
        command = [
            sys.executable,
            "-P",
            "-m",
            "obsidian_sync",
            "--config",
            str((config_path or DEFAULT_CONFIG_PATH.expanduser()).resolve()),
        ]
        if verbose:
            command.append("--verbose")
        command.append("start")
        from obsidian_sync.windows_service import diagnostic_path

        error_path = diagnostic_path()
        error_path.parent.mkdir(parents=True, exist_ok=True)
        with error_path.open("ab") as error_stream:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=error_stream,
                stderr=error_stream,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        sys.exit(0)
    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    os.setsid()
    pid = os.fork()
    if pid > 0:
        sys.exit(0)
    sys.stdin.close()
