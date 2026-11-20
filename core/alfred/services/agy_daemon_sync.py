"""Idle-safe Antigravity remote-control daemon reconciler for CLI-spawned conversations."""

from __future__ import annotations

import contextlib
import fcntl
import math
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from alfred.domain.models import utc_now_iso
from alfred.repositories.json_repository import JsonStateRepository

DEFAULT_SERVICE_NAME = "antigravity-cli-daemon.service"
SystemctlRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class DaemonSyncStatus(StrEnum):
    """Outcome statuses for Antigravity remote-control daemon reconciliation."""

    NO_PENDING_CHANGES = "NO_PENDING_CHANGES"
    SYNCED = "SYNCED"
    DEFERRED_ACTIVE_TURN = "DEFERRED_ACTIVE_TURN"
    SKIPPED_DAEMON_INACTIVE = "SKIPPED_DAEMON_INACTIVE"
    DISABLED = "DISABLED"
    RESTART_FAILED = "RESTART_FAILED"


class DaemonSyncPendingState(BaseModel):
    """Persistent dirty-flag ledger tracking CLI conversations awaiting Web UI sync."""

    pending: bool = False
    conversation_ids: list[str] = Field(default_factory=list)
    last_reason: str = ""
    updated_at: str = ""
    last_synced_at: str = ""


class DaemonSyncResult(BaseModel):
    """Result value object returned by AgyDaemonSyncService.sync_if_needed()."""

    status: DaemonSyncStatus
    synced_ids: list[str] = Field(default_factory=list)
    details: str = ""


def is_real_agy_conversation_id(conv_id: str) -> bool:
    """Return True when conv_id is a safe non-synthetic Antigravity conversation UUID.

    Args:
        conv_id: Candidate conversation identifier.

    Returns:
        True for real Antigravity conversation IDs, False for synthetic offline 'agy_*' IDs.
    """
    cleaned = conv_id.strip()
    return (
        bool(cleaned)
        and not cleaned.startswith("agy_")
        and Path(cleaned).name == cleaned
        and cleaned not in {".", ".."}
    )


class AgyDaemonSyncService:
    """Synchronize CLI-created Antigravity conversations into antigravity-cli-daemon safely."""

    def __init__(
        self,
        state_dir: Path,
        agy_data_dir: Path,
        service_name: str = DEFAULT_SERVICE_NAME,
        enabled: bool = True,
        idle_wal_grace_sec: int = 15,
        systemctl_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        stale_running_grace_sec: int = 720,
        alfred_root: Path | None = None,
    ) -> None:
        """Initialize the idle-safe Antigravity daemon synchronization service.

        Args:
            state_dir: Local Alfred state directory storing agy_daemon_sync_pending.json.
            agy_data_dir: Antigravity CLI data directory (~/.gemini/antigravity-cli).
            service_name: Systemd --user service unit name for remote-control serve.
            enabled: Whether automatic daemon reconciliation is enabled.
            idle_wal_grace_sec: Minimum quiet window in seconds on foreign .db-wal files.
            systemctl_runner: Optional subprocess override for deterministic unit testing.
            stale_running_grace_sec: Grace window before ignoring orphaned RUNNING rows on disk.
            alfred_root: Optional ALFRED_ROOT_DIR path used when spawning deferred workers.
        """
        self._state_dir = state_dir
        self._repo = JsonStateRepository(
            state_dir / "agy_daemon_sync_pending.json",
            DaemonSyncPendingState,
        )
        self._agy_data_dir = agy_data_dir
        self._service_name = service_name.strip() or DEFAULT_SERVICE_NAME
        self._enabled = enabled
        self._wal_grace_sec = max(0, int(idle_wal_grace_sec))
        self._systemctl_runner = systemctl_runner
        self._stale_running_grace_sec = max(0, int(stale_running_grace_sec))
        self._alfred_root = alfred_root

    def _load_pending_safe(self) -> DaemonSyncPendingState:
        """Load DaemonSyncPendingState safely, resetting corrupt JSON files cleanly."""
        try:
            return self._repo.load()
        except (OSError, ValueError):
            default_state = DaemonSyncPendingState()
            with contextlib.suppress(OSError):
                self._repo.save(default_state)
            return default_state

    def _update_pending_safe(
        self, mutator: Callable[[DaemonSyncPendingState], DaemonSyncPendingState]
    ) -> None:
        """Run an atomic repo update, recovering gracefully if the pending JSON is corrupt."""
        try:
            self._repo.update(mutator)
        except (OSError, ValueError):
            self._repo.save(DaemonSyncPendingState())
            self._repo.update(mutator)

    def load_state(self) -> DaemonSyncPendingState:
        """Load the current persisted DaemonSyncPendingState from disk.

        Returns:
            Validated DaemonSyncPendingState snapshot.
        """
        return self._load_pending_safe()

    def mark_dirty(self, conversation_id: str, reason: str = "cli_mutation") -> bool:
        """Record a real CLI-created or newly archived conversation ID for daemon sync.

        Args:
            conversation_id: Antigravity conversation UUID created or archived on disk.
            reason: Short diagnostic tag describing why sync is needed.

        Returns:
            True when conversation_id was real and recorded, False when synthetic/ignored.
        """
        if not is_real_agy_conversation_id(conversation_id):
            return False
        cid = conversation_id.strip()
        stamp = utc_now_iso()
        self._update_pending_safe(
            lambda cur: cur.model_copy(
                update={
                    "pending": True,
                    "conversation_ids": (
                        cur.conversation_ids
                        if cid in cur.conversation_ids
                        else [*cur.conversation_ids, cid]
                    ),
                    "last_reason": reason,
                    "updated_at": stamp,
                }
            )
        )
        return True

    def is_daemon_idle(self, exclude_ids: set[str] | None = None) -> tuple[bool, str]:
        """Verify via SQLite status and .db-wal mtimes that no Web turn is currently running.

        Args:
            exclude_ids: Conversation UUIDs just written by the current CLI batch.

        Returns:
            Tuple of (is_idle_bool, diagnostic_reason).
        """
        excluded = exclude_ids or set()
        busy_id = self._find_running_conversation_in_sqlite(excluded)
        if busy_id:
            return (False, f"Active conversation turn in SQLite: {busy_id}")
        wal_id = self._find_recent_foreign_wal(excluded)
        if wal_id:
            return (False, f"Recent .db-wal write within {self._wal_grace_sec}s: {wal_id}")
        return (True, "idle")

    @contextlib.contextmanager
    def _acquire_deferred_lock(self) -> Iterator[bool]:
        """Acquire non-blocking exclusive flock on agy_deferred_sync.lock."""
        lock_path = self._state_dir / "agy_deferred_sync.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        acquired = False
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                acquired = False
            yield acquired
        finally:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def sync_if_needed(
        self,
        force: bool = False,
        runner_override: SystemctlRunner | None = None,
    ) -> DaemonSyncResult:
        """Restart antigravity-cli-daemon when dirty and idle so Web UI sees CLI sessions.

        Args:
            force: When True, reconcile even if pending is False (still respects active turns).
            runner_override: Optional systemctl runner override (e.g. from RoutineService).

        Returns:
            DaemonSyncResult describing the reconciliation action taken.
        """
        with self._acquire_deferred_lock() as acquired:
            if not acquired:
                return DaemonSyncResult(
                    status=DaemonSyncStatus.DEFERRED_ACTIVE_TURN,
                    details="Deferred daemon sync worker already holds agy_deferred_sync.lock.",
                )
            return self._sync_if_needed_unlocked(force=force, runner_override=runner_override)

    def _sync_if_needed_unlocked(
        self,
        force: bool = False,
        runner_override: SystemctlRunner | None = None,
    ) -> DaemonSyncResult:
        """Execute daemon reconciliation assuming agy_deferred_sync.lock is held."""
        if not self._enabled and not force:
            return DaemonSyncResult(
                status=DaemonSyncStatus.DISABLED,
                details="antigravity.sync_daemon_on_changes is disabled.",
            )
        current = self._load_pending_safe()
        if not current.pending and not force:
            return DaemonSyncResult(
                status=DaemonSyncStatus.NO_PENDING_CHANGES,
                details="0 pending CLI conversation changes.",
            )
        runner = runner_override or self._systemctl_runner
        if self._is_isolated_sandbox_without_runner(runner):
            return DaemonSyncResult(
                status=DaemonSyncStatus.SKIPPED_DAEMON_INACTIVE,
                details="Sandbox environment without systemctl_runner.",
            )
        if not self._is_service_active(runner):
            return DaemonSyncResult(
                status=DaemonSyncStatus.SKIPPED_DAEMON_INACTIVE,
                details=f"{self._service_name} is not active.",
            )
        idle, reason = self.is_daemon_idle(exclude_ids=set(current.conversation_ids))
        if not idle:
            return DaemonSyncResult(status=DaemonSyncStatus.DEFERRED_ACTIVE_TURN, details=reason)
        return self._restart_and_clear(list(current.conversation_ids), runner)

    def wait_until_idle_and_sync(
        self,
        timeout_sec: float = 90.0,
        poll_interval_sec: float = 2.0,
        force: bool = False,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> DaemonSyncResult:
        """Poll until active Antigravity turns become idle (up to timeout_sec) and sync daemon.

        Args:
            timeout_sec: Maximum wait duration in seconds before returning deferred status.
            poll_interval_sec: Sleep interval in seconds between idle checks.
            force: When True, reconcile even when pending flag is False.
            sleep_fn: Injectable sleep function for deterministic testing.

        Returns:
            Final DaemonSyncResult from sync_if_needed() or DEFERRED_ACTIVE_TURN if flock held.
        """
        if not math.isfinite(timeout_sec) or timeout_sec < 0:
            raise ValueError(f"timeout_sec must be finite and >= 0, got {timeout_sec}.")
        if not math.isfinite(poll_interval_sec) or poll_interval_sec <= 0:
            raise ValueError(f"poll_interval_sec must be finite and > 0, got {poll_interval_sec}.")
        with self._acquire_deferred_lock() as acquired:
            if not acquired:
                return DaemonSyncResult(
                    status=DaemonSyncStatus.DEFERRED_ACTIVE_TURN,
                    details="Deferred daemon sync worker already holds agy_deferred_sync.lock.",
                )
            deadline = time.monotonic() + float(timeout_sec)
            interval = float(poll_interval_sec)
            while True:
                res = self._sync_if_needed_unlocked(force=force)
                if res.status != DaemonSyncStatus.DEFERRED_ACTIVE_TURN:
                    return res
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return res
                sleep_fn(min(interval, remaining))

    def spawn_deferred_sync_worker(
        self,
        alfred_root: Path | None = None,
        timeout_sec: float = 90.0,
        poll_interval_sec: float = 2.0,
        popen_fn: Callable[..., Any] | None = None,
    ) -> bool:
        """Spawn a detached background worker running 'sync-agy-ui --wait-idle' when deferred.

        Args:
            alfred_root: Optional ALFRED_ROOT_DIR override locating .venv/bin/alfred.
            timeout_sec: Maximum wait timeout passed to the worker CLI.
            poll_interval_sec: Poll interval passed to the worker CLI.
            popen_fn: Optional subprocess.Popen replacement for deterministic unit testing.

        Returns:
            True when the detached worker was spawned, False in test sandboxes or on OSError.
        """
        default_host_dir = (Path.home() / ".gemini" / "antigravity-cli").resolve()
        in_sandbox = bool(os.environ.get("PYTEST_CURRENT_TEST")) or (
            self._agy_data_dir.resolve() != default_host_dir
        )
        if popen_fn is None and in_sandbox:
            return False
        root = (
            alfred_root
            or self._alfred_root
            or Path(os.environ.get("ALFRED_ROOT_DIR", "/root/Alfred")).resolve()
        ).resolve()
        alfred_bin = root / ".venv" / "bin" / "alfred"
        prefix = [str(alfred_bin)] if alfred_bin.exists() else [sys.executable, "-m", "alfred.main"]
        cmd = [
            *prefix, "sync-agy-ui", "--wait-idle",
            "--timeout", str(timeout_sec), "--poll-interval", str(poll_interval_sec),
        ]
        env = dict(os.environ)
        env["ALFRED_ROOT_DIR"] = str(root)
        core_dir = str((root / "core").resolve())
        pp_parts = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
        if core_dir not in pp_parts:
            pp_parts.insert(0, core_dir)
        env["PYTHONPATH"] = os.pathsep.join(pp_parts)
        try:
            launcher = popen_fn or subprocess.Popen
            launcher(
                cmd, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            return True
        except OSError:
            return False

    def _restart_and_clear(
        self,
        target_ids: list[str],
        runner: SystemctlRunner | None,
    ) -> DaemonSyncResult:
        """Restart the systemd user daemon, confirm active state, and clear synced IDs."""
        restart_res = self._run_systemctl(
            ["systemctl", "--user", "restart", self._service_name], runner
        )
        if (
            restart_res is None
            or restart_res.returncode != 0
            or not self._is_service_active(runner)
        ):
            err = (restart_res.stderr.strip() if restart_res is not None else "exec error") or (
                f"{self._service_name} failed to return to active state"
            )
            return DaemonSyncResult(status=DaemonSyncStatus.RESTART_FAILED, details=err)
        stamp = utc_now_iso()
        target_set = set(target_ids)

        def _clear(cur: DaemonSyncPendingState) -> DaemonSyncPendingState:
            """Remove synced target_ids while preserving newly interleaved dirty IDs."""
            rem = [cid for cid in cur.conversation_ids if cid not in target_set]
            return cur.model_copy(
                update={"pending": bool(rem), "conversation_ids": rem, "last_synced_at": stamp}
            )

        self._update_pending_safe(_clear)
        return DaemonSyncResult(
            status=DaemonSyncStatus.SYNCED,
            synced_ids=target_ids,
            details=f"Reconciled {len(target_ids)} conversation(s) via {self._service_name}.",
        )

    def _is_isolated_sandbox_without_runner(self, runner: SystemctlRunner | None) -> bool:
        """Prevent offline or pytest sandboxes from restarting the host's live systemd daemon."""
        if runner is not None:
            return False
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return True
        default_host_dir = (Path.home() / ".gemini" / "antigravity-cli").resolve()
        return self._agy_data_dir.resolve() != default_host_dir

    def _is_service_active(self, runner: SystemctlRunner | None) -> bool:
        """Check whether the configured systemd --user remote-control daemon is active."""
        res = self._run_systemctl(["systemctl", "--user", "is-active", self._service_name], runner)
        return bool(res is not None and res.returncode == 0 and res.stdout.strip() == "active")

    def _find_running_conversation_in_sqlite(self, excluded: set[str]) -> str | None:
        """Return the ID of any non-excluded conversation currently running in SQLite."""
        summary_db = self._agy_data_dir / "conversation_summaries.db"
        if not summary_db.is_file():
            return None
        try:
            conn = sqlite3.connect(f"file:{summary_db}?mode=ro", uri=True, timeout=0.3)
            try:
                pragma = "PRAGMA table_info(conversation_summaries)"
                cols = {str(row[1]) for row in conn.execute(pragma).fetchall()}
                if "status" not in cols and "not_fully_idle" not in cols:
                    return None
                clauses: list[str] = []
                if "not_fully_idle" in cols:
                    clauses.append("not_fully_idle != 0")
                if "status" in cols:
                    clauses.append("status = 'CASCADE_RUN_STATUS_RUNNING'")
                sql = (
                    "SELECT conversation_id FROM conversation_summaries "
                    f"WHERE {' OR '.join(clauses)}"
                )
                rows = conn.execute(sql).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            return "__sqlite_busy__"
        except sqlite3.Error:
            return None
        for (cid,) in rows:
            s_cid = str(cid or "").strip()
            if s_cid and s_cid not in excluded and not self._is_stale_running_conversation(s_cid):
                return s_cid
        return None

    def _is_stale_running_conversation(self, conv_id: str) -> bool:
        """Return True only when valid artifacts exist and all mtimes predate stale grace."""
        if self._stale_running_grace_sec <= 0 or not is_real_agy_conversation_id(conv_id):
            return False
        mtimes: list[float] = []
        for cand in (
            self._agy_data_dir / "conversations" / f"{conv_id}.db",
            self._agy_data_dir / "conversations" / f"{conv_id}.db-wal",
        ):
            with contextlib.suppress(OSError):
                if cand.exists():
                    mtimes.append(cand.stat().st_mtime)
        brain_dir = self._agy_data_dir / "brain" / conv_id
        with contextlib.suppress(OSError):
            if brain_dir.exists():
                mtimes.append(brain_dir.stat().st_mtime)
                if brain_dir.is_dir():
                    for child in brain_dir.rglob("*"):
                        with contextlib.suppress(OSError):
                            mtimes.append(child.stat().st_mtime)
        if not mtimes:
            return False
        cutoff = time.time() - float(self._stale_running_grace_sec)
        return all(mt < cutoff for mt in mtimes)

    def _find_recent_foreign_wal(self, excluded: set[str]) -> str | None:
        """Return conversation ID if a non-excluded .db-wal file was modified within grace."""
        if self._wal_grace_sec <= 0:
            return None
        conv_dir = self._agy_data_dir / "conversations"
        if not conv_dir.is_dir():
            return None
        cutoff = time.time() - float(self._wal_grace_sec)
        for wal_file in conv_dir.glob("*.db-wal"):
            cid = wal_file.name.removesuffix(".db-wal")
            if cid in excluded:
                continue
            try:
                if wal_file.stat().st_mtime >= cutoff and wal_file.stat().st_size > 0:
                    return cid
            except OSError:
                continue
        return None

    def _run_systemctl(
        self,
        cmd: list[str],
        runner: SystemctlRunner | None,
    ) -> subprocess.CompletedProcess[str] | None:
        """Execute a systemctl --user command via injected runner or subprocess."""
        try:
            if runner is not None:
                return runner(cmd)
            return subprocess.run(
                cmd, stdin=subprocess.DEVNULL, capture_output=True,
                text=True, check=False, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
