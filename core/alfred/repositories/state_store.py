"""Typed facade over all local atomic state and cache repositories."""

from __future__ import annotations

import fcntl
import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from alfred.domain.calendar_models import HoldAllowlistState
from alfred.domain.drive_watcher_models import DriveWatcherLedger
from alfred.domain.models import (
    ActiveConversationsState,
    AlfredPaths,
    DriveMetadataIndex,
    DriveReadCache,
    EvolutionProposalsLedger,
    GoogleSyncBookmarks,
    KnowledgeAuditEvent,
    ProcessedItemEvent,
)
from alfred.domain.routine_models import RoutineExecutionState
from alfred.domain.security_models import SignatureAuditEvent, SignatureRequestsState
from alfred.repositories.json_repository import JsonlEventLog, JsonStateRepository

_DRIVE_SYNC_LOCK_GUARD = threading.Lock()
_DRIVE_SYNC_RLOCKS: dict[Path, threading.RLock] = {}
_DRIVE_SYNC_HANDLES: dict[tuple[Path, int], tuple[IO[str], int]] = {}


class StateStore:
    """Centralized strongly-typed access to Alfred's local JSON and JSONL state files."""

    def __init__(self, paths: AlfredPaths) -> None:
        """Wire all typed repositories inside instance/data/state and instance/data/cache.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
        """
        self._paths = paths
        self.bookmarks = JsonStateRepository(
            paths.state_dir / "google_sync_bookmarks.json",
            GoogleSyncBookmarks,
        )
        self.conversations = JsonStateRepository(
            paths.state_dir / "active_conversations.json",
            ActiveConversationsState,
        )
        self.processed_items = JsonlEventLog(
            paths.state_dir / "processed_items_history.jsonl",
            ProcessedItemEvent,
        )
        self.knowledge_audit = JsonlEventLog(
            paths.state_dir / "knowledge_modifications_audit.jsonl",
            KnowledgeAuditEvent,
        )
        self.evolution_ledger = JsonStateRepository(
            paths.state_dir / "evolution_proposals_ledger.json",
            EvolutionProposalsLedger,
        )
        self.routines = JsonStateRepository(
            paths.state_dir / "routine_state.json",
            RoutineExecutionState,
        )
        self.hold_allowlist = JsonStateRepository(
            paths.state_dir / "calendar_hold_allowlist.json",
            HoldAllowlistState,
        )
        self.signature_requests = JsonStateRepository(
            paths.state_dir / "signature_requests.json",
            SignatureRequestsState,
        )
        self.signature_audit = JsonlEventLog(
            paths.state_dir / "signature_audit.jsonl",
            SignatureAuditEvent,
        )
        self.drive_index = JsonStateRepository(
            paths.drive_index_path,
            DriveMetadataIndex,
        )
        self.drive_read_cache = JsonStateRepository(
            paths.drive_read_cache_path,
            DriveReadCache,
        )
        self.drive_watcher_ledger = JsonStateRepository(
            paths.state_dir / "processed_drive_events.json",
            DriveWatcherLedger,
        )
        self.drive_governance_audit = JsonlEventLog(
            paths.state_dir / "drive_governance_audit.jsonl",
            ProcessedItemEvent,
        )

    @contextmanager
    def acquire_routine_lock(self, lock_name: str) -> Iterator[bool]:
        """Acquire a non-blocking exclusive flock for a periodic routine scope.

        Args:
            lock_name: Alphanumeric lock identifier (e.g. 'alfred_inbox').

        Yields:
            True when the lock was acquired exclusively, False if already held.
        """
        locks_dir = self._paths.state_dir / "locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = locks_dir / f"{lock_name}.lock"
        handle = lock_file.open("a+", encoding="utf-8")
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

    @contextmanager
    def acquire_drive_sync_lock(self) -> Iterator[None]:
        """Acquire a reentrant thread-and-process exclusive lock for Drive index sync."""
        locks_dir = self._paths.state_dir / "locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        lock_file = (locks_dir / "alfred_drive_sync.lock").resolve()
        with _DRIVE_SYNC_LOCK_GUARD:
            rlock = _DRIVE_SYNC_RLOCKS.setdefault(lock_file, threading.RLock())
        rlock.acquire()
        key = (lock_file, threading.get_ident())
        try:
            with _DRIVE_SYNC_LOCK_GUARD:
                entry = _DRIVE_SYNC_HANDLES.get(key)
            if entry is None:
                handle = lock_file.open("a+", encoding="utf-8")
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except Exception:
                    handle.close()
                    raise
                with _DRIVE_SYNC_LOCK_GUARD:
                    _DRIVE_SYNC_HANDLES[key] = (handle, 1)
            else:
                with _DRIVE_SYNC_LOCK_GUARD:
                    _DRIVE_SYNC_HANDLES[key] = (entry[0], entry[1] + 1)
            try:
                yield
            finally:
                closing_handle: IO[str] | None = None
                with _DRIVE_SYNC_LOCK_GUARD:
                    cur_handle, cur_depth = _DRIVE_SYNC_HANDLES[key]
                    if cur_depth > 1:
                        _DRIVE_SYNC_HANDLES[key] = (cur_handle, cur_depth - 1)
                    else:
                        _DRIVE_SYNC_HANDLES.pop(key, None)
                        closing_handle = cur_handle
                if closing_handle is not None:
                    try:
                        fcntl.flock(closing_handle.fileno(), fcntl.LOCK_UN)
                    finally:
                        closing_handle.close()
        finally:
            rlock.release()

    def has_processed_item(self, item_type: str, item_id: str) -> bool:
        """Check whether an incoming item ID has already been recorded in the JSONL history.

        Args:
            item_type: Source channel identifier (e.g. 'gmail').
            item_id: Unique external item identifier.

        Returns:
            True if the item has already been processed, False otherwise.
        """
        return any(
            ev.item_type == item_type and ev.item_id == item_id
            for ev in self.processed_items.read_all()
        )

    def record_processed_item(
        self,
        item_type: str,
        item_id: str,
        payload_for_hash: str,
        outcome: str,
        details: str = "",
    ) -> ProcessedItemEvent:
        """Append a processed item record with a deterministic SHA-256 digest.

        Args:
            item_type: Source channel identifier (e.g. 'gmail').
            item_id: Unique external item identifier.
            payload_for_hash: Canonical payload string hashed for auditability.
            outcome: Outcome classification string.
            details: Optional human-readable audit note.

        Returns:
            Persisted ProcessedItemEvent instance.
        """
        digest = hashlib.sha256(payload_for_hash.encode("utf-8")).hexdigest()
        event = ProcessedItemEvent(
            item_type=item_type,
            item_id=item_id,
            sha256=digest,
            outcome=outcome,
            details=details,
        )
        self.processed_items.append(event)
        return event
