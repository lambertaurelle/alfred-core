"""Typed facade over all local atomic state and cache repositories."""

from __future__ import annotations

import fcntl
import hashlib
from collections.abc import Iterator
from contextlib import contextmanager

from alfred.domain.calendar_models import HoldAllowlistState
from alfred.domain.models import (
    ActiveConversationsState,
    AlfredPaths,
    DriveMetadataIndex,
    DriveReadCache,
    EvolutionProposalsLedger,
    GoogleSyncBookmarks,
    KnowledgeAuditEvent,
    NounouContract,
    ProcessedItemEvent,
)
from alfred.domain.routine_models import RoutineExecutionState
from alfred.repositories.json_repository import JsonlEventLog, JsonStateRepository


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
        self.nounou_contract = JsonStateRepository(
            paths.state_dir / "nounou_contract.json",
            NounouContract,
        )
        self.routines = JsonStateRepository(
            paths.state_dir / "routine_state.json",
            RoutineExecutionState,
        )
        self.hold_allowlist = JsonStateRepository(
            paths.state_dir / "calendar_hold_allowlist.json",
            HoldAllowlistState,
        )
        self.drive_index = JsonStateRepository(
            paths.drive_index_path,
            DriveMetadataIndex,
        )
        self.drive_read_cache = JsonStateRepository(
            paths.drive_read_cache_path,
            DriveReadCache,
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
