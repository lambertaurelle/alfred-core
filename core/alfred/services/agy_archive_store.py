"""Native Antigravity .pbtxt annotation writer, SQLite summary sync, and triage schema."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from alfred.domain.calendar_models import (
    CalendarActionIntent,
    CalendarSlotProposal,
    CalendarTriageAction,
)
from alfred.domain.models import EmailMessage, InboxOutputType, KnowledgeDocument
from alfred.repositories.json_repository import atomic_write_text

ERROR_ENVELOPE_STATUSES = frozenset(
    {"ERROR", "FAILED", "DEADLINE_EXCEEDED", "CANCELLED", "TIMEOUT"}
)

COGNITIVE_TRIAGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "output_type": {
            "type": "string",
            "enum": [
                InboxOutputType.SILENT_NO_ACTION.value,
                InboxOutputType.DRAFT_REPLY.value,
                InboxOutputType.USER_ACTION_REQUIRED.value,
            ],
        },
        "reason": {"type": "string"},
        "draft_body": {"type": "string"},
        "calendar_action": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        CalendarActionIntent.NONE.value,
                        CalendarActionIntent.CREATE_HOLD.value,
                        CalendarActionIntent.CONFIRM_EVENT.value,
                    ],
                },
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "start_iso": {"type": "string"},
                            "end_iso": {"type": "string"},
                            "attendees": {"type": "array", "items": {"type": "string"}},
                            "send_invites": {"type": "boolean"},
                            "location": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["summary", "start_iso", "end_iso"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["intent", "slots"],
            "additionalProperties": False,
        },
    },
    "required": ["output_type", "reason"],
    "additionalProperties": False,
}


def build_cognitive_triage_prompt(
    msg: EmailMessage,
    docs: list[KnowledgeDocument],
    user_addresses: frozenset[str] = frozenset(),
    calendar_context: str = "",
) -> str:
    """Build a Tier-2 and Calendar grounded cognitive triage prompt for agy --print.

    Args:
        msg: Normalized incoming Gmail message.
        docs: Loaded Tier-2 reference KnowledgeDocument list.
        user_addresses: Optional authenticated owner email addresses.
        calendar_context: Optional 14-day primary calendar and [HOLD] snapshot.

    Returns:
        Prompt string instructing agy to classify the email and extract calendar actions.
    """
    doc_blocks = [
        f"### {d.file_name} ({d.frontmatter.title})\n{d.frontmatter.summary}\n{d.body}"
        for d in docs
    ]
    ref_context = "\n\n".join(doc_blocks) if doc_blocks else "(Aucune fiche correspondante)"
    identity_line = (
        f"Adresses de l'utilisateur propriétaire (ne jamais créer de DRAFT_REPLY vers soi) : "
        f"{', '.join(sorted(user_addresses))}\n"
        if user_addresses
        else ""
    )
    cal_block = (
        f"\nContexte Agenda Principal (14j) :\n{calendar_context}\n" if calendar_context else ""
    )
    return (
        "Mode classifieur JSON (pas d'artefact implementation_plan.md, zéro appel d'outil). "
        "Classe cet e-mail entrant dans l'une des 3 sorties exclusives d'Alfred V7.1 Lean "
        "(SILENT_NO_ACTION, DRAFT_REPLY, USER_ACTION_REQUIRED). "
        "Si DRAFT_REPLY organise un RDV/événement ou propose des créneaux libres, renseigne "
        "calendar_action.intent='CREATE_HOLD' avec les créneaux proposés dans slots. "
        "Si l'e-mail confirme/valide un créneau ou meeting convenu, renseigne "
        "calendar_action.intent='CONFIRM_EVENT' avec le créneau validé et les participants "
        "dans slots (même si aucune réponse mail n'est nécessaire -> SILENT_NO_ACTION).\n\n"
        f"{identity_line}"
        f"Expéditeur: {msg.sender}\nDestinataires: {msg.recipient}\nObjet: {msg.subject}\n"
        f"Extrait: {msg.snippet}\nCorps:\n{msg.body_text[:2000]}\n"
        f"{cal_block}\n"
        f"Fiches de référence Tier-2:\n{ref_context}"
    )


def is_error_envelope(payload: dict[str, object]) -> bool:
    """Return True when an agy JSON envelope reports an execution error status.

    Args:
        payload: Parsed JSON dictionary from agy stdout.

    Returns:
        True when status indicates an execution error.
    """
    status = payload.get("status")
    return isinstance(status, str) and status.strip().upper() in ERROR_ENVELOPE_STATUSES


def parse_json_object(raw_stdout: str) -> dict[str, object] | None:
    """Parse a single- or multi-line JSON object from stdout, tolerating log lines.

    Args:
        raw_stdout: Raw stdout string emitted by agy CLI.

    Returns:
        Parsed JSON dictionary or None if no valid JSON object was found.
    """
    stripped = raw_stdout.strip()
    if not stripped:
        return None
    try:
        direct = json.loads(stripped)
        if isinstance(direct, dict):
            return {str(k): v for k, v in direct.items()}
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    offsets = [
        0,
        *(i + 1 for i, ch in enumerate(stripped) if ch == "\n" and i + 1 < len(stripped)),
    ]
    for start in reversed(offsets):
        sub = stripped[start:].lstrip(" \t")
        if not sub.startswith("{"):
            continue
        try:
            parsed, end_idx = decoder.raw_decode(sub)
        except json.JSONDecodeError:
            continue
        rem = sub[end_idx:].lstrip(" \t")
        if (
            isinstance(parsed, dict)
            and (not rem or rem.startswith("\n"))
            and not rem.lstrip().startswith(("}", "]", ","))
        ):
            return {str(k): v for k, v in parsed.items()}
    return None


def extract_conversation_id(payload: dict[str, object]) -> str | None:
    """Extract a path-safe conversation ID from a non-error agy JSON response.

    Args:
        payload: Parsed JSON dictionary from agy CLI.

    Returns:
        Safe conversation ID string or None.
    """
    if is_error_envelope(payload):
        return None
    containers: list[dict[str, object]] = [payload]
    for wrap_key in ("result", "data"):
        nested = payload.get(wrap_key)
        if isinstance(nested, dict):
            nested_dict = {str(k): v for k, v in nested.items()}
            if not is_error_envelope(nested_dict):
                containers.append(nested_dict)
    for obj in containers:
        for key in ("conversation_id", "conversationId", "session_id", "sessionId", "id"):
            val = obj.get(key)
            if isinstance(val, str):
                cleaned = val.strip()
                if cleaned and Path(cleaned).name == cleaned and cleaned not in {".", ".."}:
                    return cleaned
    return None


def unwrap_triage_payload(payload: dict[str, object]) -> dict[str, object]:
    """Unwrap nested structured output envelopes returned by CLI JSON formatters.

    Args:
        payload: Outer JSON response dictionary.

    Returns:
        Inner dictionary containing 'output_type' when found, otherwise payload.
    """
    if "output_type" in payload:
        return payload
    for key in ("structured_output", "result", "data", "output", "response"):
        nested = payload.get(key)
        if isinstance(nested, dict) and "output_type" in nested:
            return {str(k): v for k, v in nested.items()}
        if isinstance(nested, str):
            parsed = parse_json_object(nested)
            if parsed is not None and "output_type" in parsed:
                return parsed
    return payload


def parse_calendar_action(unwrapped: dict[str, object]) -> CalendarTriageAction:
    """Parse an optional calendar_action block from a cognitive triage payload.

    Args:
        unwrapped: Unwrapped triage dictionary potentially containing 'calendar_action'.

    Returns:
        Validated CalendarTriageAction (defaults to NONE on missing/invalid data).
    """
    raw = unwrapped.get("calendar_action")
    if not isinstance(raw, dict):
        return CalendarTriageAction()
    try:
        intent = CalendarActionIntent(str(raw.get("intent", "NONE")))
    except ValueError:
        return CalendarTriageAction()
    raw_slots = raw.get("slots")
    slots: list[CalendarSlotProposal] = []
    if isinstance(raw_slots, list):
        for item in raw_slots:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()
            start_iso = str(item.get("start_iso") or "").strip()
            end_iso = str(item.get("end_iso") or "").strip()
            if not (summary and start_iso and end_iso):
                continue
            raw_att = item.get("attendees")
            att = (
                [str(a).strip().lower() for a in raw_att if str(a).strip()]
                if isinstance(raw_att, list)
                else []
            )
            slots.append(
                CalendarSlotProposal(
                    summary=summary,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    attendees=att,
                    send_invites=bool(item.get("send_invites", True)),
                    location=str(item.get("location") or "").strip(),
                    description=str(item.get("description") or "").strip(),
                )
            )
    return CalendarTriageAction(intent=intent, slots=slots) if slots else CalendarTriageAction()


def sanitize_annotation_title(title: str) -> str:
    """Normalize whitespace and strip control characters from an annotation title.

    Args:
        title: Raw human-readable conversation title.

    Returns:
        Single-line normalized title truncated to 140 characters.
    """
    collapsed = " ".join(re.sub(r"[\r\n\t]+", " ", title).split()).strip()
    return (collapsed or "Alfred Trace")[:140]


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

    Returns:
        Protobuf text-format string matching Antigravity's AnnotationsManager.
    """
    dt = datetime.now(UTC)
    if timestamp_iso:
        try:
            parsed_dt = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
            dt = parsed_dt if parsed_dt.tzinfo is not None else parsed_dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    sec, nanos = int(dt.timestamp()), int(dt.microsecond * 1000)
    escaped = sanitize_annotation_title(title).replace("\\", "\\\\").replace('"', '\\"')
    arch_flag = "true" if archived else "false"
    return (
        f'title:"{escaped}"  archived:{arch_flag}  '
        f"archival_status_timestamp:{{seconds:{sec}  nanos:{nanos}}}  "
        f"last_user_view_time:{{seconds:{sec}  nanos:{nanos}}}"
    )


class AgyArchiveStore:
    """Persist Antigravity annotations (.pbtxt) and prune transient or orphan CLI artifacts."""

    def __init__(self, agy_data_dir: Path) -> None:
        """Initialize the archive store rooted at ~/.gemini/antigravity-cli.

        Args:
            agy_data_dir: Antigravity CLI data directory containing annotations/ & conversations/.
        """
        self._agy_data_dir = agy_data_dir

    def archive_conversation(
        self,
        conv_id: str,
        title: str,
        archived_at_iso: str | None = None,
    ) -> Path | None:
        """Write annotations/<conv_id>.pbtxt, update SQLite title, and touch trajectory mtime.

        Args:
            conv_id: Target Antigravity conversation UUID.
            title: Display title for the archived conversation.
            archived_at_iso: Optional ISO-8601 archival timestamp.

        Returns:
            Written .pbtxt Path, or None when conv_id is unsafe/synthetic.
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
            format_annotations_pbtxt(clean_title, archived=True, timestamp_iso=archived_at_iso),
        )
        self._update_summary_db_title(conv_id, clean_title)
        with contextlib.suppress(OSError):
            os.utime(conv_db, None)
        return pbtxt_path

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

        Returns:
            Count of removed orphan summary rows plus deleted manifest file.
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

    def _update_summary_db_title(self, conv_id: str, clean_title: str) -> None:
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
                        "UPDATE conversations_fts SET archived = 1, title = ? "
                        "WHERE conversation_id = ?",
                        (clean_title, conv_id),
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
