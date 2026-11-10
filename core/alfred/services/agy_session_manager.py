"""Antigravity Web UI session manager and SILENT_NO_ACTION auto-archiver."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from alfred.domain.models import (
    ActiveSessionRecord,
    AlfredPaths,
    AlfredSettings,
    utc_now_iso,
)
from alfred.repositories.json_repository import atomic_write_text
from alfred.repositories.state_store import StateStore


class AgySessionManager:
    """Manage Antigravity Web conversation lifecycle, Gate plans, and silent auto-archiving."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
        archive_hook: Callable[[str], None] | None = None,
    ) -> None:
        """Initialize the Antigravity Web session manager.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings.
            state_store: Atomic local state store.
            archive_hook: Optional callback invoked when archiving an Antigravity Web session.
        """
        self._paths = paths
        self._settings = settings
        self._state_store = state_store
        self._archive_hook = archive_hook
        self._plans_dir = self._paths.state_dir / "plans"
        self._plans_dir.mkdir(parents=True, exist_ok=True)

    def create_session(
        self,
        workspace: str,
        source_message_id: str = "",
        implementation_plan_md: str | None = None,
    ) -> ActiveSessionRecord:
        """Spawn a tracked Antigravity Web conversation and optionally materialize its Gate plan.

        Args:
            workspace: Target workspace slug ('alfred-inbox', 'alfred-calendar', etc.).
            source_message_id: Optional triggering message or event identifier.
            implementation_plan_md: Optional Markdown content written to implementation_plan.md.

        Returns:
            Created ActiveSessionRecord with ACTIVE status.
        """
        session_id = f"agy_{workspace.replace('-', '_')}_{uuid.uuid4().hex[:10]}"
        plan_path_str = ""
        if implementation_plan_md is not None:
            session_dir = self._plans_dir / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            plan_file = session_dir / "implementation_plan.md"
            atomic_write_text(plan_file, implementation_plan_md)
            plan_path_str = str(plan_file)

        record = ActiveSessionRecord(
            session_id=session_id,
            workspace=workspace,
            status="ACTIVE",
            source_message_id=source_message_id,
            plan_path=plan_path_str,
        )
        self._state_store.conversations.update(
            lambda cur: cur.model_copy(update={"sessions": {**cur.sessions, session_id: record}})
        )
        return record

    def write_session_plan(self, session_id: str, implementation_plan_md: str) -> Path:
        """Write or update implementation_plan.md for an active Antigravity Web session.

        Args:
            session_id: Active Antigravity session identifier.
            implementation_plan_md: Markdown Gate document content.

        Returns:
            Resolved Path to the written implementation_plan.md file.
        """
        session_dir = self._plans_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        plan_file = session_dir / "implementation_plan.md"
        atomic_write_text(plan_file, implementation_plan_md)
        self._state_store.conversations.update(
            lambda cur: (
                cur.model_copy(
                    update={
                        "sessions": {
                            **cur.sessions,
                            session_id: cur.sessions[session_id].model_copy(
                                update={"plan_path": str(plan_file)}
                            ),
                        }
                    }
                )
                if session_id in cur.sessions
                else cur
            )
        )
        return plan_file

    def auto_archive_silent_session(self, session_id: str, reason: str) -> ActiveSessionRecord:
        """Immediately auto-archive a SILENT_NO_ACTION conversation with 0 UI clutter.

        Args:
            session_id: Target Antigravity session identifier.
            reason: Concise classification rationale recorded in state.

        Returns:
            Updated ActiveSessionRecord marked with status 'ARCHIVED'.

        Raises:
            KeyError: If session_id is unknown.
        """
        state = self._state_store.conversations.load()
        if session_id not in state.sessions:
            raise KeyError(f"Unknown Antigravity session: {session_id}")

        archived = state.sessions[session_id].model_copy(
            update={
                "status": "ARCHIVED",
                "outcome": f"SILENT_NO_ACTION: {reason}",
                "archived_at": utc_now_iso(),
            }
        )
        self._state_store.conversations.update(
            lambda cur: cur.model_copy(update={"sessions": {**cur.sessions, session_id: archived}})
        )
        if self._archive_hook is not None:
            self._archive_hook(session_id)
        return archived
