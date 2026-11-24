"""Cross-project specialist delegation service with model routing and inbox-to-evolve firewall."""

from __future__ import annotations

import contextlib
import uuid
from email.utils import parseaddr

from alfred.domain.models import ActiveSessionRecord, AlfredPaths, AlfredSettings, EmailMessage
from alfred.domain.security_models import DelegationMode, DelegationRecord, DelegationTarget
from alfred.repositories.state_store import StateStore
from alfred.services.agy_archive_store import (
    extract_conversation_id,
    parse_json_object,
    sanitize_annotation_title,
)
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.gmail_service import GmailService

INBOX_SOURCE_WORKSPACES: frozenset[str] = frozenset({"inbox", "alfred-inbox"})


class DelegationService:
    """Spawn model-routed Antigravity specialist sessions with Single-Gate safety."""

    BRAIN_COMMAND_TIMEOUT_SEC = 600
    DEFAULT_COMMAND_TIMEOUT_SEC = 180

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore | None = None,
        agy_manager: AgySessionManager | None = None,
        gmail_service: GmailService | None = None,
    ) -> None:
        """Initialize the cross-project delegation service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings including feature flags and model routing.
            state_store: Optional atomic StateStore instance.
            agy_manager: Optional AgySessionManager instance for CLI spawning and title sync.
            gmail_service: Optional GmailService used to resolve authenticated owner addresses.
        """
        self._paths = paths
        self._settings = settings
        self._state_store = state_store or StateStore(paths)
        self._agy_manager = agy_manager or AgySessionManager(
            paths=paths,
            settings=settings,
            state_store=self._state_store,
        )
        self._gmail_service = gmail_service

    def delegate(
        self,
        target: DelegationTarget | str,
        title: str,
        brief: str,
        mode: DelegationMode | str = DelegationMode.OPEN_GATE,
        parent_session_id: str = "",
        source_workspace: str = "alfred-life",
        source_email: EmailMessage | None = None,
        source_message_id: str = "",
    ) -> DelegationRecord:
        """Delegate a brief to a specialist Antigravity project under Single-Gate or Open-Gate mode.

        Args:
            target: Specialist role ('calendar', 'tasks', 'inbox', 'evolve', 'life', 'onboard').
            title: Human-readable conversation title displayed in Antigravity Web sidebar.
            brief: Actionable brief or approved execution plan instructions.
            mode: DelegationMode.OPEN_GATE or DelegationMode.EXECUTE_APPROVED.
            parent_session_id: Optional originating parent session UUID for traceability.
            source_workspace: Originating workspace slug or role (default 'alfred-life').
            source_email: Optional originating EmailMessage required for inbox -> evolve firewall.
            source_message_id: Optional explicit message/event identifier.

        Returns:
            DelegationRecord capturing target project, model, mode, status, and session ID.

        Raises:
            PermissionError: If inbox -> evolve delegation fails owner + SPF/DKIM verification.
        """
        target_role = self._normalize_target_role(target)
        target_project = self._settings.resolve_workspace_project(target_role)
        resolved_mode = self._resolve_mode(target_role, mode)
        model_used, _ = self._settings.models.resolve_for_workspace(target_project)
        clean_title = sanitize_annotation_title(title or f"[{target_project}] Delegation")

        if not self._is_target_enabled(target_role):
            return DelegationRecord(
                target_role=target_role, target_project=target_project, mode=resolved_mode,
                status="SKIPPED_DISABLED", session_id="", title=clean_title,
                model_used=model_used, parent_session_id=parent_session_id,
                details=f"Feature flag disabled for specialist project '{target_project}'.",
            )

        if source_workspace.strip().lower() in INBOX_SOURCE_WORKSPACES and target_role == "evolve":
            self._enforce_inbox_to_evolve_firewall(source_email)

        delegation_status = (
            "GATE_OPENED" if resolved_mode == DelegationMode.OPEN_GATE else "EXECUTED"
        )
        live_id = self._spawn_specialist_session(
            target_project, clean_title, brief, resolved_mode, source_workspace, parent_session_id
        )
        eff_msg_id = source_message_id or (source_email.message_id if source_email else "")
        session_id = self._persist_delegated_session(
            live_id, target_project, clean_title, resolved_mode,
            delegation_status, source_workspace, eff_msg_id, parent_session_id,
        )
        return DelegationRecord(
            target_role=target_role, target_project=target_project, mode=resolved_mode,
            status=delegation_status, session_id=session_id, title=clean_title,
            model_used=model_used, parent_session_id=parent_session_id,
            details=f"Delegated to {target_project} ({delegation_status}).",
        )

    def resolve_spawn_timeout(self, target_project: str) -> int:
        """Return CLI spawn timeout (600s for orchestrators, 180s for light workspaces).

        Args:
            target_project: Target specialist project slug or role.

        Returns:
            Subprocess timeout in seconds.
        """
        model_id, _ = self._settings.models.resolve_for_workspace(target_project)
        if "orchestrator" in model_id.lower():
            return self.BRAIN_COMMAND_TIMEOUT_SEC
        return self.DEFAULT_COMMAND_TIMEOUT_SEC

    def _persist_delegated_session(
        self,
        live_id: str | None,
        target_project: str,
        clean_title: str,
        resolved_mode: DelegationMode,
        delegation_status: str,
        source_workspace: str,
        source_message_id: str,
        parent_session_id: str,
    ) -> str:
        """Set sidebar title when live and persist ActiveSessionRecord in StateStore."""
        session_id = live_id or f"agy_{target_project.replace('-', '_')}_{uuid.uuid4().hex[:10]}"
        if live_id:
            self._agy_manager.archive_store.set_conversation_title(
                live_id, clean_title, archived=False
            )
        session_rec = ActiveSessionRecord(
            session_id=session_id,
            workspace=target_project,
            status="ACTIVE" if resolved_mode == DelegationMode.OPEN_GATE else "EXECUTED",
            title=clean_title,
            outcome=f"{delegation_status}:{source_workspace}",
            source_message_id=source_message_id,
            parent_session_id=parent_session_id,
        )
        self._state_store.conversations.update(
            lambda cur: cur.model_copy(
                update={"sessions": {**cur.sessions, session_id: session_rec}}
            )
        )
        self._agy_manager.daemon_sync.mark_dirty(session_id, reason=f"delegate:{target_project}")
        return session_id

    def _normalize_target_role(self, target: DelegationTarget | str) -> str:
        """Resolve a DelegationTarget or string slug into a validated canonical role."""
        if isinstance(target, DelegationTarget):
            return target.value
        cleaned = target.strip().lower()
        if cleaned.startswith("alfred-"):
            cleaned = cleaned[7:]
        return DelegationTarget(cleaned).value

    def _resolve_mode(self, target_role: str, mode: DelegationMode | str) -> DelegationMode:
        """Normalize delegation mode and enforce OPEN_GATE whenever target_role is evolve."""
        if target_role == DelegationTarget.EVOLVE.value:
            return DelegationMode.OPEN_GATE
        if isinstance(mode, DelegationMode):
            return mode
        return DelegationMode(str(mode).strip().lower())

    def _is_target_enabled(self, target_role: str) -> bool:
        """Check whether the target specialist workspace feature flag is enabled."""
        feats = self._settings.features
        if target_role == DelegationTarget.TASKS.value:
            return feats.tasks_management
        if target_role == DelegationTarget.CALENDAR.value:
            return feats.calendar_management
        if target_role == DelegationTarget.INBOX.value:
            return feats.inbox_auto_triage
        if target_role == DelegationTarget.DRIVE.value:
            return feats.drive_knowledge_sync
        return True

    def _enforce_inbox_to_evolve_firewall(self, source_email: EmailMessage | None) -> None:
        """Verify owner sender and mandatory SPF + DKIM pass before inbox -> evolve delegation."""
        if source_email is None:
            raise PermissionError(
                "Delegating from alfred-inbox to alfred-evolve requires a verified source_email."
            )
        owner_addresses = self._collect_owner_addresses()
        _, bare_sender = parseaddr(source_email.sender)
        sender_norm = bare_sender.strip().lower()
        if not sender_norm or sender_norm not in owner_addresses:
            raise PermissionError(
                f"Sender '{sender_norm}' is not an authenticated owner address for alfred-evolve."
            )
        if not self._settings.security.require_spf_dkim_for_evolve_email:
            return
        lower_headers = {str(k).strip().lower(): str(v) for k, v in source_email.headers.items()}
        auth_hdr = (
            f"{lower_headers.get('authentication-results', '')} "
            f"{lower_headers.get('arc-authentication-results', '')}"
        ).lower()
        has_both_pass = "spf=pass" in auth_hdr and "dkim=pass" in auth_hdr
        has_any_fail = "spf=fail" in auth_hdr or "dkim=fail" in auth_hdr
        if not has_both_pass or has_any_fail:
            raise PermissionError(
                "Source email must pass both SPF and DKIM to delegate into alfred-evolve."
            )

    def _collect_owner_addresses(self) -> frozenset[str]:
        """Collect lowercase authenticated owner email addresses from identity and Gmail API."""
        owners: set[str] = set()
        default_send_as = self._settings.identity.default_send_as_email.strip().lower()
        if "@" in default_send_as:
            owners.add(default_send_as)
        if self._gmail_service is not None:
            with contextlib.suppress(Exception):
                for addr in self._gmail_service.get_authenticated_addresses():
                    cleaned = str(addr).strip().lower()
                    if "@" in cleaned:
                        owners.add(cleaned)
        return frozenset(owners)

    def _spawn_specialist_session(
        self,
        target_project: str,
        clean_title: str,
        brief: str,
        mode: DelegationMode,
        source_workspace: str,
        parent_session_id: str,
    ) -> str | None:
        """Invoke agy CLI in the specialist workspace cwd and extract conversation UUID."""
        agy_bin = self._agy_manager.locate_agy_binary()
        if agy_bin is None:
            return None
        origin = f" (parent={parent_session_id})" if parent_session_id else ""
        if mode == DelegationMode.OPEN_GATE:
            agent_flags = ["--agent", "plan-synthesizer", "--mode", "plan"]
            prompt = (
                f"# {clean_title}\n\n"
                f"Delegation from `{source_workspace}`{origin}. Directly produce the blocking "
                "Gate artifact `implementation_plan.md` (`UserFacing: true`, "
                "`RequestFeedback: true`) without redundant exploration loops for:\n\n"
                f"{brief}"
            )
        else:
            agent_flags = ["--agent", "plan-executor"]
            prompt = (
                f"# {clean_title}\n\n"
                f"Approved Single-Gate execution from `{source_workspace}`{origin}. "
                f"Directly execute the approved actions without opening a second plan:\n\n{brief}"
            )
        cmd = [
            agy_bin,
            "--project",
            target_project,
            *self._agy_manager.build_model_cli_flags(target_project),
            "--add-dir",
            str(self._paths.root_dir),
            *agent_flags,
            "--output-format",
            "json",
            "-p",
            prompt,
        ]
        before_ids = self._agy_manager.archive_store.snapshot_summary_conversation_ids()
        completed = self._agy_manager.run_agy_command(
            cmd,
            cwd=self._agy_manager.resolve_workspace_cwd(target_project),
            timeout=self.resolve_spawn_timeout(target_project),
        )
        live_id: str | None = None
        if completed is not None and completed.returncode == 0:
            parsed = parse_json_object(completed.stdout)
            if parsed is not None:
                live_id = extract_conversation_id(parsed)
        if live_id is None and before_ids is not None:
            self._agy_manager.archive_store.purge_incomplete_stubs(exclude_ids=before_ids)
        return live_id
