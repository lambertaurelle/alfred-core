"""Idle-safe Antigravity remote-control daemon reconciler for CLI-spawned conversations."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

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
    ) -> None:
        """Initialize the idle-safe Antigravity daemon synchronization service.

        Args:
            state_dir: Local Alfred state directory storing agy_daemon_sync_pending.json.
            agy_data_dir: Antigravity CLI data directory (~/.gemini/antigravity-cli).
            service_name: Systemd --user service unit name for remote-control serve.
            enabled: Whether automatic daemon reconciliation is enabled.
            idle_wal_grace_sec: Minimum quiet window in seconds on foreign .db-wal files.
            systemctl_runner: Optional subprocess override for deterministic unit testing.
        """
        self._repo = JsonStateRepository(
            state_dir / "agy_daemon_sync_pending.json",
            DaemonSyncPendingState,
        )
        self._agy_data_dir = agy_data_dir
        self._service_name = service_name.strip() or DEFAULT_SERVICE_NAME
        self._enabled = enabled
        self._wal_grace_sec = max(0, int(idle_wal_grace_sec))
        self._systemctl_runner = systemctl_runner

    def load_state(self) -> DaemonSyncPendingState:
        """Load the current persisted DaemonSyncPendingState from disk.

        Returns:
            Validated DaemonSyncPendingState snapshot.
        """
        return self._repo.load()

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
        self._repo.update(
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
        if not self._enabled and not force:
            return DaemonSyncResult(
                status=DaemonSyncStatus.DISABLED,
                details="antigravity.sync_daemon_on_changes is disabled.",
            )
        current = self._repo.load()
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
        pending_set = set(current.conversation_ids)
        idle, reason = self.is_daemon_idle(exclude_ids=pending_set)
        if not idle:
            return DaemonSyncResult(
                status=DaemonSyncStatus.DEFERRED_ACTIVE_TURN,
                details=reason,
            )
        return self._restart_and_clear(list(current.conversation_ids), runner)

    def _restart_and_clear(
        self,
        target_ids: list[str],
        runner: SystemctlRunner | None,
    ) -> DaemonSyncResult:
        """Restart the systemd user daemon, confirm active state, and clear pending flag."""
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
        self._repo.update(
            lambda cur: cur.model_copy(
                update={
                    "pending": False,
                    "conversation_ids": [
                        cid for cid in cur.conversation_ids if cid not in set(target_ids)
                    ],
                    "last_synced_at": stamp,
                }
            )
            if set(cur.conversation_ids) != set(target_ids)
            else cur.model_copy(
                update={"pending": False, "conversation_ids": [], "last_synced_at": stamp}
            )
        )
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
            conn = sqlite3.connect(summary_db, timeout=3.0)
            try:
                cols = {
                    str(row[1])
                    for row in conn.execute(
                        "PRAGMA table_info(conversation_summaries)"
                    ).fetchall()
                }
                if "status" not in cols and "not_fully_idle" not in cols:
                    return None
                clauses: list[str] = []
                if "not_fully_idle" in cols:
                    clauses.append("not_fully_idle != 0")
                if "status" in cols:
                    clauses.append("status = 'CASCADE_RUN_STATUS_RUNNING'")
                query = (
                    "SELECT conversation_id FROM conversation_summaries "
                    f"WHERE {' OR '.join(clauses)}"
                )
                rows = conn.execute(query).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        for (cid,) in rows:
            s_cid = str(cid or "").strip()
            if s_cid and s_cid not in excluded:
                return s_cid
        return None

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
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
