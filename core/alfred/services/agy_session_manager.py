"""Antigravity Web UI session manager, model-tiered agy CLI bridge, and title sync."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

from alfred.domain.calendar_models import CalendarTriageAction
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
from alfred.services.agy_archive_store import (
    COGNITIVE_TRIAGE_SCHEMA,
    AgyArchiveStore,
    build_cognitive_triage_prompt,
    extract_conversation_id,
    is_error_envelope,
    parse_calendar_action,
    parse_json_object,
    sanitize_annotation_title,
    unwrap_triage_payload,
)
from alfred.services.agy_daemon_sync import AgyDaemonSyncService


class AgySessionManager:
    """Manage Antigravity Web conversation lifecycle, model routing, and readable titles."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
        archive_hook: Callable[[str], None] | None = None,
        runner_fn: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        agy_locator_fn: Callable[[], str | None] | None = None,
        antigravity_data_dir: Path | None = None,
        systemctl_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        """Initialize the Antigravity Web session manager.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings.
            state_store: Atomic local state store.
            archive_hook: Optional callback invoked when archiving an Antigravity Web session.
            runner_fn: Optional subprocess runner override for deterministic offline tests.
            agy_locator_fn: Optional binary locator override returning agy executable path.
            antigravity_data_dir: Optional override for ~/.gemini/antigravity-cli data dir.
            systemctl_runner: Optional subprocess override for systemd daemon reconciliation.
        """
        self._paths = paths
        self._settings = settings
        self._state_store = state_store
        self._archive_hook = archive_hook
        self._runner_fn = runner_fn
        self._agy_locator_fn = agy_locator_fn
        self._agy_data_dir = antigravity_data_dir or (Path.home() / ".gemini" / "antigravity-cli")
        self._archive_store = AgyArchiveStore(self._agy_data_dir)
        self._daemon_sync = AgyDaemonSyncService(
            state_dir=self._paths.state_dir, agy_data_dir=self._agy_data_dir,
            service_name=self._settings.agy_remote_control_service,
            enabled=self._settings.agy_sync_daemon_on_changes,
            idle_wal_grace_sec=self._settings.agy_idle_wal_grace_sec,
            systemctl_runner=systemctl_runner, alfred_root=self._paths.root_dir,
        )
        self._plans_dir = self._paths.state_dir / "plans"
        self._plans_dir.mkdir(parents=True, exist_ok=True)
        self._last_needs_orchestrator_study: bool = False

    @property
    def archive_store(self) -> AgyArchiveStore:
        """Return the underlying AgyArchiveStore instance."""
        return self._archive_store

    @property
    def daemon_sync(self) -> AgyDaemonSyncService:
        """Return the idle-safe AgyDaemonSyncService instance."""
        return self._daemon_sync

    @property
    def state_store(self) -> StateStore:
        """Return the StateStore instance associated with this session manager."""
        return self._state_store

    @property
    def agy_data_dir(self) -> Path:
        """Return the configured Antigravity CLI data directory path."""
        return self._agy_data_dir

    @property
    def has_live_overrides(self) -> bool:
        """Return True when runner_fn or agy_locator_fn test overrides are installed."""
        return self._runner_fn is not None or self._agy_locator_fn is not None

    @property
    def last_needs_orchestrator_study(self) -> bool:
        """Return whether the most recent cognitive triage requested Orchestrator study."""
        return self._last_needs_orchestrator_study

    def build_model_cli_flags(self, workspace: str, trace: bool = False) -> list[str]:
        """Build --model (and --effort ONLY on non-orchestrator models) CLI flags.

        Args:
            workspace: Target specialist workspace role or slug.
            trace: When True, use lightweight trace model routing.

        Returns:
            CLI argument list (omits --effort whenever model is an orchestrator).
        """
        models = self._settings.models
        model_id, effort = (
            models.resolve_trace() if trace else models.resolve_for_workspace(workspace)
        )
        flags = ["--model", model_id]
        if effort is not None:
            flags.extend(["--effort", effort])
        return flags

    def resolve_workspace_cwd(self, workspace: str | None) -> Path:
        """Resolve the physical workspace directory for agy CLI execution or fallback to root_dir.

        Args:
            workspace: Optional workspace role or slug (e.g. 'alfred-inbox').
        """
        if workspace:
            project_slug = self._settings.resolve_workspace_project(workspace)
            for candidate in (
                self._paths.workspaces_dir / project_slug,
                self._paths.workspaces_dir / workspace.strip(),
            ):
                if candidate.is_dir():
                    return candidate
        return self._paths.root_dir

    def create_session(
        self,
        workspace: str,
        source_message_id: str = "",
        implementation_plan_md: str | None = None,
        spawn_live: bool = False,
        title: str = "",
        parent_session_id: str = "",
    ) -> ActiveSessionRecord:
        """Spawn a tracked Antigravity Web conversation with a clean sidebar title and Gate plan.

        Args:
            workspace: Target workspace slug ('alfred-inbox', 'alfred-calendar', etc.).
            source_message_id: Optional triggering message or event identifier.
            implementation_plan_md: Optional Markdown content written to implementation_plan.md.
            spawn_live: When True and a plan is given, spawn a live agy Web Gate conversation.
            title: Optional explicit human-readable conversation title.
            parent_session_id: Optional originating parent conversation UUID.
        """
        raw_title = title or f"[{workspace}] {source_message_id or 'Validation Gate'}"
        clean_title = sanitize_annotation_title(raw_title)
        live_id: str | None = None
        if spawn_live and implementation_plan_md is not None:
            live_id = self._spawn_live_plan_session(
                workspace=workspace, source_message_id=source_message_id,
                implementation_plan_md=implementation_plan_md, title=clean_title,
            )

        session_id = live_id or f"agy_{workspace.replace('-', '_')}_{uuid.uuid4().hex[:10]}"
        if live_id:
            self._archive_store.set_conversation_title(live_id, clean_title, archived=False)

        plan_path_str = ""
        if implementation_plan_md is not None:
            session_dir = self._plans_dir / session_id
            session_dir.mkdir(parents=True, exist_ok=True)
            plan_file = session_dir / "implementation_plan.md"
            atomic_write_text(plan_file, implementation_plan_md)
            plan_path_str = str(plan_file)

        record = ActiveSessionRecord(
            session_id=session_id, workspace=workspace, status="ACTIVE", title=clean_title,
            source_message_id=source_message_id, parent_session_id=parent_session_id,
            plan_path=plan_path_str,
        )
        self._state_store.conversations.update(
            lambda cur: cur.model_copy(update={"sessions": {**cur.sessions, session_id: record}})
        )
        self._daemon_sync.mark_dirty(session_id, reason=f"gate:{workspace}")
        return record

    def create_and_archive_trace(
        self,
        workspace: str,
        title: str,
        trace_markdown: str,
        source_message_id: str = "",
        outcome: str = "",
        existing_conv_id: str | None = None,
        spawn_live: bool = False,
        archived_at: str | None = None,
    ) -> ActiveSessionRecord:
        """Materialize and auto-archive a real Antigravity trace conversation in workspace.

        Args:
            workspace: Target Antigravity project/workspace ('alfred-inbox' or 'alfred-calendar').
            title: Sidebar title written into annotations/<conv_id>.pbtxt.
            trace_markdown: Concise audit Markdown rendered inside the conversation when spawned.
            source_message_id: Optional Gmail message_id or event_id reference.
            outcome: Outcome summary persisted in ActiveSessionRecord.
            existing_conv_id: Optional existing conversation UUID to reuse and archive in-place.
            spawn_live: When True and existing_conv_id is None, spawn a lightweight agy trace.
            archived_at: Optional explicit archival ISO-8601 timestamp.
        """
        resolved_id = existing_conv_id
        if not resolved_id and spawn_live:
            resolved_id = self._spawn_live_trace_session(workspace, title, trace_markdown)
        session_id = resolved_id or f"agy_{workspace.replace('-', '_')}_{uuid.uuid4().hex[:10]}"
        ts = archived_at or utc_now_iso()
        clean_title = sanitize_annotation_title(title)
        self._archive_store.archive_conversation(session_id, title=clean_title, archived_at_iso=ts)
        record = ActiveSessionRecord(
            session_id=session_id, workspace=workspace, status="ARCHIVED", title=clean_title,
            outcome=outcome or clean_title, source_message_id=source_message_id,
            created_at=ts, archived_at=ts,
        )
        self._state_store.conversations.update(
            lambda cur: cur.model_copy(update={"sessions": {**cur.sessions, session_id: record}})
        )
        self._daemon_sync.mark_dirty(session_id, reason=f"archive:{workspace}")
        if self._archive_hook is not None:
            self._archive_hook(session_id)
        return record

    def classify_email_with_agy(
        self,
        msg: EmailMessage,
        docs: list[KnowledgeDocument],
        user_addresses: frozenset[str] = frozenset(),
    ) -> tuple[InboxOutputType, str, str | None]:
        """Classify an ambiguous email and draft a reply via agy CLI (3-tuple API).

        Args:
            msg: Normalized incoming Gmail message.
            docs: Loaded Tier-2 reference KnowledgeDocument list for factual grounding.
            user_addresses: Optional authenticated user addresses/aliases for self-mail context.
        """
        out_type, reason, draft_body, _cal_action, conv_id = self.classify_email_full(
            msg, docs, user_addresses=user_addresses
        )
        self.purge_classifier_session(conv_id)
        return (out_type, reason, draft_body)

    def classify_email_full(
        self,
        msg: EmailMessage,
        docs: list[KnowledgeDocument],
        user_addresses: frozenset[str] = frozenset(),
        calendar_context: str = "",
    ) -> tuple[InboxOutputType, str, str | None, CalendarTriageAction, str | None]:
        """Classify an email inside project alfred-inbox using Hydrogen light model routing.

        Args:
            msg: Normalized incoming Gmail message.
            docs: Loaded Tier-2 reference KnowledgeDocument list.
            user_addresses: Optional authenticated owner email addresses.
            calendar_context: Optional upcoming primary calendar & [HOLD] context block.
        """
        self._last_needs_orchestrator_study = False
        agy_bin = self._locate_agy_binary()
        if agy_bin is None:
            return self._fallback_triage("agy unavailable")
        target_project = self._settings.resolve_workspace_project("alfred-inbox")
        schema_json = json.dumps(COGNITIVE_TRIAGE_SCHEMA, ensure_ascii=False)
        prompt = build_cognitive_triage_prompt(
            msg, docs, user_addresses, calendar_context, paths=self._paths
        )
        cmd = [
            agy_bin, "--project", target_project, *self.build_model_cli_flags("alfred-inbox"),
            "--output-format", "json", "--json-schema", schema_json,
            "--print", prompt, "--disable-slash-commands",
        ]
        completed = self._run_agy_command(cmd, cwd=self.resolve_workspace_cwd("alfred-inbox"))
        if completed is None or completed.returncode != 0:
            return self._fallback_triage("agy fallback")
        parsed = parse_json_object(completed.stdout)
        if parsed is None or is_error_envelope(parsed):
            return self._fallback_triage("invalid agy JSON")
        conv_id = extract_conversation_id(parsed)
        unwrapped = unwrap_triage_payload(parsed)
        try:
            output_type = InboxOutputType(str(unwrapped.get("output_type", "")))
        except ValueError:
            self.purge_classifier_session(conv_id)
            return self._fallback_triage("unknown agy output_type")
        self._last_needs_orchestrator_study = bool(unwrapped.get("needs_orchestrator_study", False))
        reason = str(unwrapped.get("reason") or "Classified via agy cognitive triage")
        raw_draft = unwrapped.get("draft_body")
        draft_body = raw_draft.strip() if isinstance(raw_draft, str) and raw_draft.strip() else None
        return (output_type, reason, draft_body, parse_calendar_action(unwrapped), conv_id)

    def _fallback_triage(
        self, suffix: str
    ) -> tuple[InboxOutputType, str, str | None, CalendarTriageAction, str | None]:
        """Return safe default DRAFT_REPLY tuple when agy CLI triage fails."""
        rsn = f"Direct human inquiry requiring a reply draft ({suffix})"
        return (InboxOutputType.DRAFT_REPLY, rsn, None, CalendarTriageAction(), None)

    def purge_classifier_session(self, conv_id: str | None) -> None:
        """Purge an intermediate classifier conversation when replaced by a Gate session.

        Args:
            conv_id: Optional classifier conversation UUID to remove.
        """
        if conv_id:
            self._archive_store.purge_transient_conversation(conv_id)

    def write_session_plan(self, session_id: str, implementation_plan_md: str) -> Path:
        """Write or update implementation_plan.md for an active Antigravity Web session.

        Args:
            session_id: Active Antigravity session identifier.
            implementation_plan_md: Markdown Gate document content.
        """
        session_dir = self._plans_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        plan_file = session_dir / "implementation_plan.md"
        atomic_write_text(plan_file, implementation_plan_md)
        self._state_store.conversations.update(
            lambda cur: (
                cur.model_copy(update={"sessions": {
                    **cur.sessions,
                    session_id: cur.sessions[session_id].model_copy(
                        update={"plan_path": str(plan_file)}
                    ),
                }})
                if session_id in cur.sessions else cur
            )
        )
        return plan_file

    def auto_archive_silent_session(
        self, session_id: str, reason: str, title: str | None = None
    ) -> ActiveSessionRecord:
        """Immediately auto-archive a SILENT_NO_ACTION conversation and write .pbtxt if live.

        Args:
            session_id: Target Antigravity session identifier.
            reason: Concise classification rationale recorded in state.
            title: Optional explicit sidebar title for annotations/<session_id>.pbtxt.
        """
        state = self._state_store.conversations.load()
        if session_id not in state.sessions:
            raise KeyError(f"Unknown Antigravity session: {session_id}")
        ts = utc_now_iso()
        clean_title = sanitize_annotation_title(title or f"[SILENT] {reason}")
        self._archive_store.archive_conversation(session_id, title=clean_title, archived_at_iso=ts)
        archived = state.sessions[session_id].model_copy(update={
            "status": "ARCHIVED", "title": clean_title,
            "outcome": f"SILENT_NO_ACTION: {reason}", "archived_at": ts,
        })
        self._state_store.conversations.update(
            lambda cur: cur.model_copy(update={"sessions": {**cur.sessions, session_id: archived}})
        )
        self._daemon_sync.mark_dirty(session_id, reason="auto_archive")
        if self._archive_hook is not None:
            self._archive_hook(session_id)
        return archived

    def repair_session_titles(self) -> list[str]:
        """Repair blank or boilerplate conversation titles in SQLite using StateStore titles."""
        sessions = self._state_store.conversations.load().sessions
        known = {sid: rec.title for sid, rec in sessions.items() if rec.title}
        repaired = self._archive_store.repair_blank_or_boilerplate_titles(known)
        for cid in repaired:
            self._daemon_sync.mark_dirty(cid, reason="repair_titles")
        return repaired

    def locate_agy_binary(self) -> str | None:
        """Resolve an executable agy CLI binary path or None when unavailable."""
        return self._locate_agy_binary()

    def run_agy_command(
        self, cmd: list[str], cwd: Path | None = None, timeout: int = 180
    ) -> subprocess.CompletedProcess[str] | None:
        """Execute an agy CLI command in target cwd with configurable timeout.

        Args:
            cmd: Command argv list starting with agy binary.
            cwd: Optional working directory path.
            timeout: Subprocess timeout in seconds.
        """
        return self._run_agy_command(cmd, cwd=cwd, timeout=timeout)

    def _locate_agy_binary(self) -> str | None:
        """Resolve an executable agy CLI binary via override, PATH, or ~/.local/bin/agy."""
        if self._agy_locator_fn is not None:
            return self._agy_locator_fn()
        configured = self._settings.agy_binary.strip()
        if not configured:
            return None
        which_hit = shutil.which(configured)
        if which_hit:
            return which_hit
        if "/" in configured:
            cand = Path(configured)
            return str(cand) if cand.is_file() and os.access(cand, os.X_OK) else None
        if configured == "agy":
            default_bin = Path.home() / ".local" / "bin" / "agy"
            if default_bin.is_file() and os.access(default_bin, os.X_OK):
                return str(default_bin)
        return None

    def _run_agy_command(
        self, cmd: list[str], cwd: Path | None = None, timeout: int = 180
    ) -> subprocess.CompletedProcess[str] | None:
        """Execute an agy CLI command via injected runner or subprocess in target cwd."""
        try:
            if self._runner_fn is not None:
                return self._runner_fn(cmd)
            return subprocess.run(
                cmd, cwd=cwd or self._paths.root_dir, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, check=False, timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def _spawn_live_trace_session(
        self, workspace: str, title: str, trace_markdown: str
    ) -> str | None:
        """Spawn a fast, zero-tool Antigravity trace conversation in target workspace."""
        agy_bin = self._locate_agy_binary()
        if agy_bin is None:
            return None
        prompt = (
            f"# {title}\n\nAutomated audit trace (zero tool calls; "
            f"confirm archival in one sentence):\n\n{trace_markdown}"
        )
        cmd = [
            agy_bin, "--project", self._settings.resolve_workspace_project(workspace),
            *self.build_model_cli_flags(workspace, trace=True),
            "--disable-slash-commands", "--output-format", "json", "--print", prompt,
        ]
        completed = self._run_agy_command(cmd, cwd=self.resolve_workspace_cwd(workspace))
        if completed is None or completed.returncode != 0:
            return None
        parsed = parse_json_object(completed.stdout)
        return extract_conversation_id(parsed) if parsed is not None else None

    def _spawn_live_plan_session(
        self, workspace: str, source_message_id: str, implementation_plan_md: str, title: str = ""
    ) -> str | None:
        """Spawn a live Antigravity Web Gate session via agy CLI and return conversation ID."""
        agy_bin = self._locate_agy_binary()
        if agy_bin is None:
            return None
        heading = title or f"[{workspace}] {source_message_id}"
        prompt = (
            f"# {heading}\n\n"
            "Stage-1 research is complete; do not read workspace files or run shell/search "
            "commands. Materialize the blocking Gate artifact `implementation_plan.md` with "
            "`UserFacing: true` and `RequestFeedback: true` using the following exact Markdown, "
            f"then stop immediately:\n\n{implementation_plan_md}"
        )
        cmd = [
            agy_bin, "--project", self._settings.resolve_workspace_project(workspace),
            *self.build_model_cli_flags(workspace), "--add-dir", str(self._paths.root_dir),
            "--agent", "plan-synthesizer", "--mode", "plan",
            "--output-format", "json", "-p", prompt,
        ]
        model_id, _ = self._settings.models.resolve_for_workspace(workspace)
        timeout_sec = 600 if "orchestrator" in model_id.lower() else 180
        before_ids = self._archive_store.snapshot_summary_conversation_ids()
        completed = self._run_agy_command(
            cmd, cwd=self.resolve_workspace_cwd(workspace), timeout=timeout_sec
        )
        live_id: str | None = None
        if completed is not None and completed.returncode == 0:
            parsed = parse_json_object(completed.stdout)
            live_id = extract_conversation_id(parsed) if parsed is not None else None
        if live_id is None and before_ids is not None:
            self._archive_store.purge_incomplete_stubs(exclude_ids=before_ids)
        return live_id
