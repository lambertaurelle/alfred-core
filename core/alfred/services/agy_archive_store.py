"""Native Antigravity .pbtxt annotation writer, SQLite summary sync, and archive store."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alfred.repositories.json_repository import atomic_write_text
from alfred.services.agy_triage_parser import (
    COGNITIVE_TRIAGE_SCHEMA as COGNITIVE_TRIAGE_SCHEMA,
)
from alfred.services.agy_triage_parser import (
    ERROR_ENVELOPE_STATUSES as ERROR_ENVELOPE_STATUSES,
)
from alfred.services.agy_triage_parser import (
    build_cognitive_triage_prompt as build_cognitive_triage_prompt,
)
from alfred.services.agy_triage_parser import (
    extract_conversation_id as extract_conversation_id,
)
from alfred.services.agy_triage_parser import (
    is_error_envelope as is_error_envelope,
)
from alfred.services.agy_triage_parser import (
    parse_calendar_action as parse_calendar_action,
)
from alfred.services.agy_triage_parser import (
    parse_json_object as parse_json_object,
)
from alfred.services.agy_triage_parser import (
    unwrap_triage_payload as unwrap_triage_payload,
)


def sanitize_annotation_title(title: str) -> str:
    """Normalize whitespace and strip control characters from an annotation title.

    Args:
        title: Raw human-readable conversation title.
    """
    collapsed = " ".join(re.sub(r"[\r\n\t]+", " ", title).split()).strip()
    return (collapsed or "Alfred Session")[:140]


def format_annotations_pbtxt(
    title: str,
    archived: bool = True,
    timestamp_iso: str | None = None,
) -> str:
    """Format an Antigravity ConversationAnnotations textproto (.pbtxt) payload.

    Args:
        title: Conversation title displayed in Antigravity Web UI.
        archived: Whether the conversation should appear under 'Archived Only'.
        timestamp_iso: Optional ISO-8601 archival timestamp; defaults to current UTC.
    """
    dt = datetime.now(UTC)
    if timestamp_iso:
        with contextlib.suppress(ValueError):
            parsed_dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
            dt = parsed_dt if parsed_dt.tzinfo is not None else parsed_dt.replace(tzinfo=UTC)
    sec, nanos = int(dt.timestamp()), int(dt.microsecond * 1000)
    escaped = sanitize_annotation_title(title).replace("\\", "\\\\").replace('"', '\\"')
    arch_flag = "true" if archived else "false"
    return (
        f'title:"{escaped}"  archived:{arch_flag}  '
        f"archival_status_timestamp:{{seconds:{sec}  nanos:{nanos}}}  "
        f"last_user_view_time:{{seconds:{sec}  nanos:{nanos}}}"
    )


class AgyArchiveStore:
    """Persist Antigravity annotations (.pbtxt), clean titles, and prune orphan CLI artifacts."""

    def __init__(self, agy_data_dir: Path) -> None:
        """Initialize the archive store rooted at ~/.gemini/antigravity-cli.

        Args:
            agy_data_dir: Antigravity CLI data directory containing annotations/ & conversations/.
        """
        self._agy_data_dir = agy_data_dir

    def set_conversation_title(
        self,
        conv_id: str,
        title: str,
        archived: bool = False,
        timestamp_iso: str | None = None,
    ) -> Path | None:
        """Write annotations/<conv_id>.pbtxt and sync SQLite summary title (active or archived).

        Args:
            conv_id: Target Antigravity conversation UUID.
            title: Clean human-readable sidebar title.
            archived: True to archive conversation, False to keep active in sidebar.
            timestamp_iso: Optional explicit ISO-8601 timestamp.
        """
        if not self._is_real_conv_id(conv_id):
            return None
        conv_db = self._agy_data_dir / "conversations" / f"{conv_id}.db"
        if not conv_db.is_file():
            return None
        clean_title = sanitize_annotation_title(title)
        annot_dir = self._agy_data_dir / "annotations"
        annot_dir.mkdir(parents=True, exist_ok=True)
        pbtxt_path = annot_dir / f"{conv_id}.pbtxt"
        atomic_write_text(
            pbtxt_path,
            format_annotations_pbtxt(clean_title, archived=archived, timestamp_iso=timestamp_iso),
        )
        self._update_summary_db_title(conv_id, clean_title, archived=archived)
        with contextlib.suppress(OSError):
            os.utime(conv_db, None)
        return pbtxt_path

    def archive_conversation(
        self,
        conv_id: str,
        title: str,
        archived_at_iso: str | None = None,
    ) -> Path | None:
        """Write archived annotations/<conv_id>.pbtxt and sync SQLite summary title.

        Args:
            conv_id: Target Antigravity conversation UUID.
            title: Display title for the archived conversation.
            archived_at_iso: Optional ISO-8601 archival timestamp.
        """
        return self.set_conversation_title(
            conv_id, title=title, archived=True, timestamp_iso=archived_at_iso
        )

    def repair_blank_or_boilerplate_titles(
        self,
        fallback_titles: dict[str, str] | None = None,
    ) -> list[str]:
        """Repair blank or boilerplate CLI prompt titles across conversation_summaries.db.

        Args:
            fallback_titles: Optional mapping of session_id -> clean title from StateStore.
        """
        summary_db = self._agy_data_dir / "conversation_summaries.db"
        if not summary_db.is_file():
            return []
        known = fallback_titles or {}
        repaired: list[str] = []
        try:
            conn = sqlite3.connect(summary_db, timeout=5.0)
            try:
                rows = conn.execute(
                    "SELECT conversation_id, project_id, title FROM conversation_summaries"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        for raw_cid, raw_proj, raw_title in rows:
            cid = str(raw_cid or "")
            cur_title = str(raw_title or "").strip()
            needs_fix = (
                not cur_title
                or cur_title.startswith("Workspace:")
                or cur_title.startswith("Stage-1 research is complete")
            )
            if cid and needs_fix:
                replacement = known.get(cid) or f"[{raw_proj or 'alfred'}] Session {cid[:8]}"
                if self.set_conversation_title(cid, replacement, archived=False) is not None:
                    repaired.append(cid)
        return repaired

    def purge_transient_conversation(self, conv_id: str) -> None:
        """Remove an intermediate classifier conversation replaced by a dedicated Gate session.

        Args:
            conv_id: Transient classifier conversation UUID.
        """
        if not conv_id or Path(conv_id).name != conv_id or conv_id in {".", ".."}:
            return
        for suffix in (".db", ".db-wal", ".db-shm"):
            (self._agy_data_dir / "conversations" / f"{conv_id}{suffix}").unlink(missing_ok=True)
        (self._agy_data_dir / "annotations" / f"{conv_id}.pbtxt").unlink(missing_ok=True)
        shutil.rmtree(self._agy_data_dir / "brain" / conv_id, ignore_errors=True)
        self._delete_summary_rows([conv_id])

    def prune_orphan_default_cli_project(self, projects_dir: Path | None = None) -> int:
        """Delete orphan default-cli-project summary rows and empty default-cli-project.json.

        Args:
            projects_dir: Optional ~/.gemini/config/projects directory path.
        """
        removed = 0
        summary_db = self._agy_data_dir / "conversation_summaries.db"
        live_default_exists = False
        if summary_db.is_file():
            try:
                conn = sqlite3.connect(summary_db, timeout=5.0)
                try:
                    rows = conn.execute(
                        "SELECT conversation_id FROM conversation_summaries "
                        "WHERE project_id = 'default-cli-project'"
                    ).fetchall()
                finally:
                    conn.close()
                orphan_ids = [
                    str(cid)
                    for (cid,) in rows
                    if cid and not (self._agy_data_dir / "conversations" / f"{cid}.db").is_file()
                ]
                live_default_exists = any(
                    cid and (self._agy_data_dir / "conversations" / f"{cid}.db").is_file()
                    for (cid,) in rows
                )
                if orphan_ids:
                    self._delete_summary_rows(orphan_ids)
                    removed += len(orphan_ids)
            except sqlite3.Error:
                pass
        if not live_default_exists and projects_dir is not None:
            manifest = projects_dir / "default-cli-project.json"
            if manifest.is_file():
                manifest.unlink(missing_ok=True)
                removed += 1
        return removed

    def _is_real_conv_id(self, conv_id: str) -> bool:
        """Return True when conv_id is a safe non-synthetic Antigravity conversation ID."""
        return (
            bool(conv_id)
            and not conv_id.startswith("agy_")
            and Path(conv_id).name == conv_id
            and conv_id not in {".", ".."}
        )

    def _update_summary_db_title(
        self, conv_id: str, clean_title: str, archived: bool = True
    ) -> None:
        """Update title and FTS archived flag in conversation_summaries.db when present."""
        summary_db = self._agy_data_dir / "conversation_summaries.db"
        if not summary_db.is_file():
            return
        try:
            conn = sqlite3.connect(summary_db, timeout=5.0)
            try:
                conn.execute(
                    "UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
                    (clean_title, conv_id),
                )
                if self._has_fts_table(conn):
                    conn.execute(
                        "UPDATE conversations_fts SET archived = ?, title = ? "
                        "WHERE conversation_id = ?",
                        (1 if archived else 0, clean_title, conv_id),
                    )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            return

    def _delete_summary_rows(self, conv_ids: list[str]) -> None:
        """Delete conversation IDs from conversation_summaries and conversations_fts."""
        summary_db = self._agy_data_dir / "conversation_summaries.db"
        if not conv_ids or not summary_db.is_file():
            return
        try:
            conn = sqlite3.connect(summary_db, timeout=5.0)
            try:
                has_fts = self._has_fts_table(conn)
                for cid in conv_ids:
                    conn.execute(
                        "DELETE FROM conversation_summaries WHERE conversation_id = ?", (cid,)
                    )
                    if has_fts:
                        conn.execute(
                            "DELETE FROM conversations_fts WHERE conversation_id = ?", (cid,)
                        )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            return

    def _has_fts_table(self, conn: sqlite3.Connection) -> bool:
        """Check whether conversations_fts exists in the open SQLite connection."""
        hit = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'conversations_fts'"
        ).fetchone()
        return hit is not None
