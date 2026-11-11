"""Antigravity Web UI session manager, live agy CLI bridge, and silent auto-archiver."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

from alfred.domain.models import (
    ActiveSessionRecord,
    AlfredPaths,
    AlfredSettings,
    EmailMessage,
    InboxOutputType,
    KnowledgeDocument,
    utc_now_iso,
)
from alfred.repositories.json_repository import atomic_write_text
from alfred.repositories.state_store import StateStore

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
    },
    "required": ["output_type", "reason"],
    "additionalProperties": False,
}


def _is_error_envelope(payload: dict[str, object]) -> bool:
    """Return True when an agy JSON envelope reports an execution error status."""
    status = payload.get("status")
    return isinstance(status, str) and status.strip().upper() in ERROR_ENVELOPE_STATUSES


def _parse_json_object(raw_stdout: str) -> dict[str, object] | None:
    """Parse a single- or multi-line JSON object from stdout, tolerating log lines."""
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
    offsets = [0, *(i + 1 for i, ch in enumerate(stripped) if ch == "\n" and i + 1 < len(stripped))]
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


def _extract_conversation_id(payload: dict[str, object]) -> str | None:
    """Extract a path-safe conversation ID from a non-error agy JSON response."""
    if _is_error_envelope(payload):
        return None
    containers: list[dict[str, object]] = [payload]
    for wrap_key in ("result", "data"):
        nested = payload.get(wrap_key)
        if isinstance(nested, dict):
            nested_dict = {str(k): v for k, v in nested.items()}
            if not _is_error_envelope(nested_dict):
                containers.append(nested_dict)
    for obj in containers:
        for key in ("conversation_id", "conversationId", "session_id", "sessionId", "id"):
            val = obj.get(key)
            if isinstance(val, str):
                cleaned = val.strip()
                if cleaned and Path(cleaned).name == cleaned and cleaned not in {".", ".."}:
                    return cleaned
    return None


def _unwrap_triage_payload(payload: dict[str, object]) -> dict[str, object]:
    """Unwrap nested structured output envelopes returned by CLI JSON formatters."""
    if "output_type" in payload:
        return payload
    for key in ("structured_output", "result", "data", "output", "response"):
        nested = payload.get(key)
        if isinstance(nested, dict) and "output_type" in nested:
            return {str(k): v for k, v in nested.items()}
        if isinstance(nested, str):
            parsed = _parse_json_object(nested)
            if parsed is not None and "output_type" in parsed:
                return parsed
    return payload


class AgySessionManager:
    """Manage Antigravity Web conversation lifecycle, Gate plans, and silent auto-archiving."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
        archive_hook: Callable[[str], None] | None = None,
        runner_fn: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        agy_locator_fn: Callable[[], str | None] | None = None,
    ) -> None:
        """Initialize the Antigravity Web session manager.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings.
            state_store: Atomic local state store.
            archive_hook: Optional callback invoked when archiving an Antigravity Web session.
            runner_fn: Optional subprocess runner override for deterministic offline tests.
            agy_locator_fn: Optional binary locator override returning agy executable path.
        """
        self._paths = paths
        self._settings = settings
        self._state_store = state_store
        self._archive_hook = archive_hook
        self._runner_fn = runner_fn
        self._agy_locator_fn = agy_locator_fn
        self._plans_dir = self._paths.state_dir / "plans"
        self._plans_dir.mkdir(parents=True, exist_ok=True)

    def create_session(
        self,
        workspace: str,
        source_message_id: str = "",
        implementation_plan_md: str | None = None,
        spawn_live: bool = False,
    ) -> ActiveSessionRecord:
        """Spawn a tracked Antigravity Web conversation and optionally materialize its Gate plan.

        Args:
            workspace: Target workspace slug ('alfred-inbox', 'alfred-calendar', etc.).
            source_message_id: Optional triggering message or event identifier.
            implementation_plan_md: Optional Markdown content written to implementation_plan.md.
            spawn_live: When True and a plan is given, spawn a live agy Web Gate conversation.

        Returns:
            Created ActiveSessionRecord with ACTIVE status.
        """
        live_id: str | None = None
        if spawn_live and implementation_plan_md is not None:
            live_id = self._spawn_live_plan_session(
                workspace=workspace,
                source_message_id=source_message_id,
                implementation_plan_md=implementation_plan_md,
            )

        session_id = live_id or f"agy_{workspace.replace('-', '_')}_{uuid.uuid4().hex[:10]}"
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

    def classify_email_with_agy(
        self,
        msg: EmailMessage,
        docs: list[KnowledgeDocument],
    ) -> tuple[InboxOutputType, str, str | None]:
        """Classify an ambiguous email and draft a reply via agy CLI grounded in Tier-2 .md docs.

        Args:
            msg: Normalized incoming Gmail message.
            docs: Loaded Tier-2 reference KnowledgeDocument list for factual grounding.

        Returns:
            Tuple of (InboxOutputType, rationale string, optional draft reply body).
        """
        agy_bin = self._locate_agy_binary()
        if agy_bin is None:
            return (
                InboxOutputType.DRAFT_REPLY,
                "Direct human inquiry requiring a reply draft (agy unavailable)",
                None,
            )
        schema_json = json.dumps(COGNITIVE_TRIAGE_SCHEMA, ensure_ascii=False)
        prompt = self._build_cognitive_triage_prompt(msg, docs)
        cmd = [
            agy_bin,
            "--output-format",
            "json",
            "--json-schema",
            schema_json,
            "--print",
            prompt,
        ]
        completed = self._run_agy_command(cmd)
        if completed is None or completed.returncode != 0:
            return (
                InboxOutputType.DRAFT_REPLY,
                "Direct human inquiry requiring a reply draft (agy fallback)",
                None,
            )
        parsed = _parse_json_object(completed.stdout)
        if parsed is None or _is_error_envelope(parsed):
            return (
                InboxOutputType.DRAFT_REPLY,
                "Direct human inquiry requiring a reply draft (invalid agy JSON)",
                None,
            )
        unwrapped = _unwrap_triage_payload(parsed)
        try:
            output_type = InboxOutputType(str(unwrapped.get("output_type", "")))
        except ValueError:
            return (
                InboxOutputType.DRAFT_REPLY,
                "Direct human inquiry requiring a reply draft (unknown agy output_type)",
                None,
            )
        reason = str(unwrapped.get("reason") or "Classified via agy cognitive triage")
        raw_draft = unwrapped.get("draft_body")
        draft_body = raw_draft.strip() if isinstance(raw_draft, str) and raw_draft.strip() else None
        return (output_type, reason, draft_body)

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

    def locate_agy_binary(self) -> str | None:
        """Resolve an executable agy CLI binary path or None when unavailable.

        Returns:
            Executable path string or None.
        """
        return self._locate_agy_binary()

    def _locate_agy_binary(self) -> str | None:
        """Resolve an executable agy CLI binary via override, PATH, or /root/.local/bin/agy."""
        if self._agy_locator_fn is not None:
            return self._agy_locator_fn()
        configured = self._settings.agy_binary.strip()
        if not configured:
            return None
        which_hit = shutil.which(configured)
        if which_hit:
            return which_hit
        if "/" in configured:
            candidate = Path(configured)
            return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
        if configured == "agy":
            default_bin = Path("/root/.local/bin/agy")
            if default_bin.is_file() and os.access(default_bin, os.X_OK):
                return str(default_bin)
        return None

    def _run_agy_command(self, cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
        """Execute an agy CLI command via injected runner or subprocess with safe error handling."""
        try:
            if self._runner_fn is not None:
                return self._runner_fn(cmd)
            return subprocess.run(
                cmd,
                cwd=self._paths.root_dir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _spawn_live_plan_session(
        self,
        workspace: str,
        source_message_id: str,
        implementation_plan_md: str,
    ) -> str | None:
        """Spawn a live Antigravity Web Gate session via agy CLI and return conversation ID."""
        agy_bin = self._locate_agy_binary()
        if agy_bin is None:
            return None
        prompt = (
            f"Workspace: {workspace} | Source: {source_message_id}\n"
            "Stage-1 research is complete; do not read workspace files or run shell/search "
            "commands. Materialize the blocking Gate artifact `implementation_plan.md` with "
            "`UserFacing: true` and `RequestFeedback: true` using the following exact Markdown, "
            "then stop immediately:\n\n"
            f"{implementation_plan_md}"
        )
        target_project = self._settings.resolve_workspace_project(workspace)
        cmd = [
            agy_bin,
            "--project",
            target_project,
            "--add-dir",
            str(self._paths.root_dir),
            "--agent",
            "plan-synthesizer",
            "--mode",
            "plan",
            "--output-format",
            "json",
            "-p",
            prompt,
        ]
        completed = self._run_agy_command(cmd)
        if completed is None or completed.returncode != 0:
            return None
        parsed = _parse_json_object(completed.stdout)
        return _extract_conversation_id(parsed) if parsed is not None else None

    def _build_cognitive_triage_prompt(
        self,
        msg: EmailMessage,
        docs: list[KnowledgeDocument],
    ) -> str:
        """Build a Tier-2 grounded cognitive triage prompt for agy --print."""
        doc_blocks = [
            f"### {d.file_name} ({d.frontmatter.title})\n{d.frontmatter.summary}\n{d.body}"
            for d in docs
        ]
        ref_context = "\n\n".join(doc_blocks) if doc_blocks else "(Aucune fiche correspondante)"
        return (
            "Mode classifieur JSON interne (pas d'artefact implementation_plan.md, zéro appel "
            "d'outil ni lecture/écriture de fichier). Classe cet e-mail entrant dans l'une des "
            "3 sorties exclusives d'Alfred V7.1 Lean (SILENT_NO_ACTION, DRAFT_REPLY, "
            "USER_ACTION_REQUIRED). Si DRAFT_REPLY, rédige un brouillon concis et poli en "
            "français dans draft_body.\n\n"
            f"Expéditeur: {msg.sender}\nObjet: {msg.subject}\n"
            f"Extrait: {msg.snippet}\nCorps:\n{msg.body_text[:2000]}\n\n"
            f"Fiches de référence Tier-2:\n{ref_context}"
        )
