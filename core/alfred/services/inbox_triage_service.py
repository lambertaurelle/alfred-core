"""alfred-inbox new email routine with 3 exclusive outputs and alfred-calendar HOLD/Gate bridge."""

from __future__ import annotations

import re
from collections.abc import Callable

from alfred.domain.calendar_models import CalendarActionIntent, CalendarTriageAction
from alfred.domain.models import (
    AlfredSettings,
    CalendarEventItem,
    EmailMessage,
    InboxOutputType,
    InboxTriageDecision,
    KnowledgeDocument,
    fold_accents,
    utc_now_iso,
)
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailHistoryExpiredError, GmailService
from alfred.services.inbox_calendar_bridge import (
    backfill_silent_inbox_records,
    enrich_calendar_action_attendees,
    load_upcoming_calendar_snapshot,
    render_action_required_plan,
    render_calendar_confirm_gate_plan,
    render_draft_calendar_section,
    render_draft_reply_plan,
    render_silent_trace_markdown,
    resolve_self_email_context,
)
from alfred.services.knowledge_service import KnowledgeService

PASSIVE_PORTAL_PATTERNS = (
    r"nouveaux?\s+documents?.*espace",
    r"document\s+reglementaire.*disponible",
    r"facture.*abonnement.*disponible",
    r"espace\s+generali",
    r"newsletter",
    r"se\s+desabonner",
)

ACTION_REQUIRED_PATTERNS = (
    r"action\s+requise",
    r"a\s+valider\s+avant",
    r"declaration\s+mensuelle",
    r"rejet\s+de\s+prelevement",
    r"impaye",
    r"document\s+a\s+signer",
    r"piece\s+justificative\s+manquante",
    r"mise\s+en\s+demeure",
)

DRIVE_SEARCH_STOPWORDS = frozenset(
    {"action", "requise", "valider", "avant", "votre", "merci", "bonjour", "nouveau", "nouvelle"}
    | {"echeance"}
)


class InboxTriageService:
    """Process newly arrived Gmail messages into 3 outputs and coordinate alfred-calendar."""

    def __init__(
        self,
        state_store: StateStore,
        gmail_service: GmailService,
        knowledge_service: KnowledgeService,
        drive_service: DriveService,
        agy_manager: AgySessionManager,
        cognitive_fn: Callable[
            [EmailMessage, list[KnowledgeDocument]],
            tuple[InboxOutputType, str, str | None],
        ]
        | None = None,
        spawn_live_agy: bool = False,
        calendar_service: CalendarService | None = None,
        settings: AlfredSettings | None = None,
    ) -> None:
        """Initialize the inbox triage service.

        Args:
            state_store: Local atomic state repository facade.
            gmail_service: Gmail API v1 connector.
            knowledge_service: 3-tier YAML frontmatter knowledge loader.
            drive_service: Google Drive metadata index and native reader service.
            agy_manager: Antigravity Web session manager and silent auto-archiver.
            cognitive_fn: Optional LLM cognitive classifier/drafter override.
            spawn_live_agy: When True, spawn live Antigravity Web sessions & traces.
            calendar_service: Optional CalendarService for [HOLD] & invitation Gates.
            settings: Optional AlfredSettings instance.
        """
        self._state_store = state_store
        self._gmail = gmail_service
        self._knowledge = knowledge_service
        self._drive = drive_service
        self._agy = agy_manager
        self._cognitive_fn = cognitive_fn
        self._spawn_live_agy = spawn_live_agy
        self._calendar = calendar_service
        self._settings = settings or AlfredSettings()

    def poll_new_messages(
        self,
        spawn_live_agy: bool | None = None,
        use_cognitive_agy: bool = False,
    ) -> list[InboxTriageDecision]:
        """Bookmark Gmail historyId on First-Run or process only newly arrived messages.

        Args:
            spawn_live_agy: Optional override to spawn live Antigravity Web Gate sessions.
            use_cognitive_agy: When True, invoke agy CLI cognitive triage on human emails.

        Returns:
            List of InboxTriageDecision outcomes for newly arrived messages.
        """
        bookmarks = self._state_store.bookmarks.load()
        if not bookmarks.gmail_history_id:
            return self._reset_history_bookmark()

        try:
            messages, latest_id = self._gmail.list_new_messages_since(bookmarks.gmail_history_id)
        except GmailHistoryExpiredError:
            return self._reset_history_bookmark()

        decisions: list[InboxTriageDecision] = []
        for msg in messages:
            if self._state_store.has_processed_item("gmail", msg.message_id):
                continue
            decisions.append(
                self.process_message(
                    msg,
                    spawn_live_agy=spawn_live_agy,
                    use_cognitive_agy=use_cognitive_agy,
                )
            )

        self._state_store.bookmarks.update(
            lambda b: b.model_copy(
                update={"gmail_history_id": latest_id, "gmail_last_polled_at": utc_now_iso()}
            )
        )
        return decisions

    def backfill_silent_archives(self) -> list[str]:
        """Materialize real Antigravity archived conversations for synthetic agy_* silent records.

        Returns:
            Ordered list of newly materialized Antigravity conversation IDs.
        """
        return backfill_silent_inbox_records(self._state_store, self._gmail, self._agy)

    def _reset_history_bookmark(self) -> list[InboxTriageDecision]:
        """Record the current Gmail historyId without replaying historical emails."""
        current_id = self._gmail.get_current_history_id()
        self._state_store.bookmarks.update(
            lambda b: b.model_copy(
                update={"gmail_history_id": current_id, "gmail_last_polled_at": utc_now_iso()}
            )
        )
        return []

    def process_message(
        self,
        msg: EmailMessage,
        spawn_live_agy: bool | None = None,
        use_cognitive_agy: bool = False,
    ) -> InboxTriageDecision:
        """Route one incoming email into SILENT / DRAFT / ACTION + alfred-calendar Gate.

        Args:
            msg: Normalized incoming Gmail message.
            spawn_live_agy: Optional override to spawn live Antigravity Web Gate sessions.
            use_cognitive_agy: When True, invoke agy CLI cognitive triage on human emails.

        Returns:
            Completed InboxTriageDecision with Gmail & Antigravity side effects applied.
        """
        effective_spawn = self._spawn_live_agy if spawn_live_agy is None else spawn_live_agy
        relevant_docs = self._load_relevant_knowledge(msg)
        is_from_self, is_self_only, user_addrs = resolve_self_email_context(
            msg, self._gmail.get_authenticated_addresses()
        )
        cal_ctx, upcoming_events = (
            load_upcoming_calendar_snapshot(self._calendar, self._settings)
            if use_cognitive_agy
            else ("", [])
        )
        output_type, reason, custom_draft, cal_action, cls_conv_id = self._classify_message(
            msg,
            relevant_docs,
            use_cognitive_agy=use_cognitive_agy,
            is_from_self=is_from_self,
            is_self_only=is_self_only,
            user_addrs=user_addrs,
            calendar_context=cal_ctx,
        )
        allowlist = (
            self._calendar.get_effective_hold_allowlist(user_addrs)
            if self._calendar is not None
            else user_addrs
        )
        enriched_cal = enrich_calendar_action_attendees(cal_action, msg, user_addrs, allowlist)
        decision = self._dispatch_triage_output(
            msg=msg,
            output_type=output_type,
            is_from_self=is_from_self,
            reason=reason,
            docs=relevant_docs,
            custom_draft=custom_draft,
            cal_action=enriched_cal,
            upcoming_events=upcoming_events,
            allowlist=allowlist,
            cls_conv_id=cls_conv_id,
            spawn_live=effective_spawn,
        )
        self._state_store.record_processed_item(
            item_type="gmail",
            item_id=msg.message_id,
            payload_for_hash=f"{msg.message_id}:{msg.subject}:{decision.output_type}",
            outcome=str(decision.output_type),
            details=decision.reason,
        )
        return decision

    def _dispatch_triage_output(
        self,
        msg: EmailMessage,
        output_type: InboxOutputType,
        is_from_self: bool,
        reason: str,
        docs: list[KnowledgeDocument],
        custom_draft: str | None,
        cal_action: CalendarTriageAction,
        upcoming_events: list[CalendarEventItem],
        allowlist: frozenset[str],
        cls_conv_id: str | None,
        spawn_live: bool,
    ) -> InboxTriageDecision:
        """Route classified output to silent, action-required, or draft-reply handler."""
        if output_type == InboxOutputType.SILENT_NO_ACTION:
            return self._handle_silent(
                msg, reason, cal_action, upcoming_events, cls_conv_id, spawn_live
            )
        self._agy.purge_classifier_session(cls_conv_id)
        if output_type == InboxOutputType.USER_ACTION_REQUIRED or is_from_self:
            return self._handle_action_required(msg, reason, docs, spawn_live=spawn_live)
        return self._handle_draft_reply(
            msg, reason, docs, custom_draft, cal_action, allowlist, spawn_live
        )

    def _load_relevant_knowledge(self, msg: EmailMessage) -> list[KnowledgeDocument]:
        """Tier-1 -> Tier-2 progressive lookup of reference .md files matching the email."""
        query = f"{msg.sender} {msg.subject} {msg.snippet} {msg.body_text[:200]}"
        matches = self._knowledge.select_relevant_frontmatters(query)[:2]
        return [self._knowledge.load_document(file_name) for file_name, _ in matches]

    def _classify_message(
        self,
        msg: EmailMessage,
        docs: list[KnowledgeDocument],
        use_cognitive_agy: bool = False,
        is_from_self: bool = False,
        is_self_only: bool = False,
        user_addrs: frozenset[str] = frozenset(),
        calendar_context: str = "",
    ) -> tuple[InboxOutputType, str, str | None, CalendarTriageAction, str | None]:
        """Classify an email deterministically first, delegating to cognitive_fn or agy CLI."""
        combined = fold_accents(f"{msg.subject}\n{msg.snippet}\n{msg.body_text}")
        no_cal = CalendarTriageAction()
        for pat in ACTION_REQUIRED_PATTERNS:
            if re.search(pat, combined):
                msg_rsn = f"Matched user-action signal: '{pat}'"
                return (InboxOutputType.USER_ACTION_REQUIRED, msg_rsn, None, no_cal, None)
        if is_self_only:
            memo_rsn = "Self-sent memo or personal reminder requiring user follow-up"
            return (InboxOutputType.USER_ACTION_REQUIRED, memo_rsn, None, no_cal, None)
        if not is_from_self and self._is_passive_or_automated(msg, combined):
            pas_rsn = "Passive portal or automated bulk notification without action signal"
            return (InboxOutputType.SILENT_NO_ACTION, pas_rsn, None, no_cal, None)

        out_type, reason, draft, cal_action, conv_id = self._run_cognitive_or_default(
            msg, docs, use_cognitive_agy, user_addrs, calendar_context
        )
        if is_from_self and out_type == InboxOutputType.DRAFT_REPLY:
            self_rsn = "Self-authored email requiring personal follow-up (no self-reply draft)"
            return (InboxOutputType.USER_ACTION_REQUIRED, self_rsn, None, cal_action, conv_id)
        return (out_type, reason, draft, cal_action, conv_id)

    def _is_passive_or_automated(self, msg: EmailMessage, combined_folded: str) -> bool:
        """Return True for passive portal notices, newsletters, or no-reply bulk headers."""
        if any(re.search(pat, combined_folded) for pat in PASSIVE_PORTAL_PATTERNS):
            return True
        has_unsub = "list-unsubscribe" in {k.lower() for k in msg.headers}
        auto_sub = msg.headers.get("auto-submitted", "no").lower() != "no"
        sender_lower = msg.sender.lower()
        is_noreply = any(tok in sender_lower for tok in ("no-reply", "ne-pas-repondre", "noreply"))
        return has_unsub or auto_sub or is_noreply

    def _run_cognitive_or_default(
        self,
        msg: EmailMessage,
        docs: list[KnowledgeDocument],
        use_cognitive_agy: bool,
        user_addrs: frozenset[str],
        calendar_context: str = "",
    ) -> tuple[InboxOutputType, str, str | None, CalendarTriageAction, str | None]:
        """Invoke injected cognitive_fn, agy CLI classifier, or deterministic human fallback."""
        if self._cognitive_fn is not None:
            out_t, rsn, dft = self._cognitive_fn(msg, docs)
            return (out_t, rsn, dft, CalendarTriageAction(), None)
        if use_cognitive_agy:
            return self._agy.classify_email_full(
                msg,
                docs,
                user_addresses=user_addrs,
                calendar_context=calendar_context,
            )
        return (
            InboxOutputType.DRAFT_REPLY,
            "Direct human inquiry requiring a reply draft",
            None,
            CalendarTriageAction(),
            None,
        )

    def _handle_silent(
        self,
        msg: EmailMessage,
        reason: str,
        cal_action: CalendarTriageAction | None = None,
        upcoming_events: list[CalendarEventItem] | None = None,
        classifier_conv_id: str | None = None,
        spawn_live: bool = False,
    ) -> InboxTriageDecision:
        """Auto-archive SILENT_NO_ACTION in alfred-inbox and open alfred-calendar Gate if needed."""
        session = self._agy.create_and_archive_trace(
            workspace="alfred-inbox",
            title=f"[SILENT] {msg.subject}",
            trace_markdown=render_silent_trace_markdown(msg, reason),
            source_message_id=msg.message_id,
            outcome=f"SILENT_NO_ACTION: {reason}",
            existing_conv_id=classifier_conv_id,
            spawn_live=spawn_live,
        )
        cal_session_id: str | None = None
        eff_action = cal_action or CalendarTriageAction()
        if eff_action.intent == CalendarActionIntent.CONFIRM_EVENT and eff_action.slots:
            gate_md = render_calendar_confirm_gate_plan(
                msg, reason, eff_action, upcoming_events or []
            )
            cal_session = self._agy.create_session(
                workspace="alfred-calendar",
                source_message_id=msg.message_id,
                implementation_plan_md=gate_md,
                spawn_live=spawn_live,
            )
            cal_session_id = cal_session.session_id
        return InboxTriageDecision(
            message_id=msg.message_id,
            output_type=InboxOutputType.SILENT_NO_ACTION,
            reason=reason,
            starred=False,
            session_id=session.session_id,
            session_archived=True,
            calendar_session_id=cal_session_id,
            calendar_action_intent=eff_action.intent.value,
        )

    def _handle_draft_reply(
        self,
        msg: EmailMessage,
        reason: str,
        docs: list[KnowledgeDocument],
        custom_draft: str | None,
        cal_action: CalendarTriageAction | None = None,
        allowlist: frozenset[str] = frozenset(),
        spawn_live: bool = False,
    ) -> InboxTriageDecision:
        """Execute DRAFT_REPLY: create Gmail draft + active Gate plan (+ [HOLD] commands)."""
        reply_subject = (
            msg.subject if msg.subject.lower().startswith("re:") else f"Re: {msg.subject}"
        )
        reply_to = msg.headers.get("reply-to") or msg.sender
        parent_msg_id = msg.headers.get("message-id", "")
        refs = msg.headers.get("references", "")
        chain = f"{refs} {parent_msg_id}".strip() if parent_msg_id else refs
        draft_body = custom_draft or self._synthesize_default_draft(msg, docs)
        draft_id = self._gmail.create_draft(
            msg.thread_id,
            reply_to,
            reply_subject,
            draft_body,
            in_reply_to=parent_msg_id,
            references=chain,
        )
        eff_action = cal_action or CalendarTriageAction()
        cal_section = render_draft_calendar_section(
            eff_action, msg.thread_id, msg.message_id, allowlist
        )
        plan_md = render_draft_reply_plan(
            reply_to, reply_subject, draft_id, draft_body, cal_section, thread_id=msg.thread_id
        )
        session = self._agy.create_session(
            workspace="alfred-inbox",
            source_message_id=msg.message_id,
            implementation_plan_md=plan_md,
            spawn_live=spawn_live,
        )
        return InboxTriageDecision(
            message_id=msg.message_id,
            output_type=InboxOutputType.DRAFT_REPLY,
            reason=reason,
            draft_id=draft_id,
            draft_body=draft_body,
            starred=False,
            session_id=session.session_id,
            session_archived=False,
            implementation_plan_markdown=plan_md,
            calendar_action_intent=eff_action.intent.value,
        )

    def _handle_action_required(
        self,
        msg: EmailMessage,
        reason: str,
        docs: list[KnowledgeDocument],
        spawn_live: bool = False,
    ) -> InboxTriageDecision:
        """Execute USER_ACTION_REQUIRED: STARRED in Gmail + active session + thorough brief."""
        self._gmail.star_message(msg.message_id)
        brief_items: list[str] = [
            f"Fiche de référence `{d.file_name}` : {d.frontmatter.summary}" for d in docs
        ]
        search_tokens = [
            tok
            for tok in re.findall(r"[a-z0-9]{4,}", fold_accents(msg.subject))
            if tok not in DRIVE_SEARCH_STOPWORDS
        ][:4]
        seen_ids: set[str] = set()
        for token in search_tokens:
            for hit in self._drive.search_index(token)[:2]:
                if hit.file_id not in seen_ids:
                    seen_ids.add(hit.file_id)
                    brief_items.append(f"Pièce Google Drive : `{hit.path}` (`{hit.file_id}`)")
        if not brief_items:
            brief_items.append(
                "Vérification du dossier `03_dossiers_actifs_et_administratif.md` recommandée."
            )
        target_doc_id = docs[0].frontmatter.id if docs else "dossiers_actifs_et_administratif"
        plan_md = render_action_required_plan(msg, reason, brief_items, target_doc_id)
        session = self._agy.create_session(
            workspace="alfred-inbox",
            source_message_id=msg.message_id,
            implementation_plan_md=plan_md,
            spawn_live=spawn_live,
        )
        return InboxTriageDecision(
            message_id=msg.message_id,
            output_type=InboxOutputType.USER_ACTION_REQUIRED,
            reason=reason,
            starred=True,
            session_id=session.session_id,
            session_archived=False,
            implementation_plan_markdown=plan_md,
            research_brief=brief_items,
            suggested_knowledge_update_id=target_doc_id,
        )

    def _synthesize_default_draft(
        self,
        msg: EmailMessage,
        docs: list[KnowledgeDocument],
    ) -> str:
        """Build a concise courteous draft reply grounded in loaded reference context."""
        context_hint = f" ({docs[0].frontmatter.title})" if docs else ""
        return (
            f"Bonjour,\n\n"
            f"Merci pour votre message concernant « {msg.subject} »{context_hint}.\n"
            f"J'ai bien pris connaissance de votre demande et reviens vers vous pour confirmer.\n\n"
            f"Bien cordialement,"
        )
