"""alfred-inbox 2-speed routine with 0-token pre-filters, sanitizer, and calendar bridge."""

from __future__ import annotations

import re
from collections.abc import Callable

from alfred.domain.attachment_models import CognitiveTriageDirectives
from alfred.domain.calendar_models import CalendarTriageAction
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
from alfred.services.attachment_extractor import split_attachment_body
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.email_security_service import EmailSanitizer, InboxCapabilityFirewall
from alfred.services.gmail_drive_bridge_service import GmailDriveBridgeService
from alfred.services.gmail_service import GmailHistoryExpiredError, GmailService
from alfred.services.inbox_booking_detector import (
    has_dated_booking_signal,
    is_passive_or_automated_email,
)
from alfred.services.inbox_calendar_bridge import (
    backfill_silent_inbox_records,
    enrich_calendar_action_attendees,
    load_upcoming_calendar_snapshot,
    materialize_draft_reply_decision,
    materialize_silent_triage_decision,
    render_draft_calendar_section,
    resolve_self_email_context,
)
from alfred.services.inbox_eligibility import InboxEligibilityPolicy
from alfred.services.inbox_plan_renderer import (
    build_action_research_brief,
    finalize_cognitive_triage_decision,
    render_action_required_plan,
)
from alfred.services.knowledge_service import KnowledgeService

ACTION_REQUIRED_PATTERNS = (
    r"action\s+requise", r"a\s+valider\s+avant", r"declaration\s+mensuelle",
    r"rejet\s+de\s+prelevement", r"impaye", r"document\s+a\s+signer",
    r"piece\s+justificative\s+manquante", r"mise\s+en\s+demeure",
    r"action\s+required", r"to\s+validate\s+before", r"monthly\s+filing",
    r"payment\s+failed", r"unpaid", r"document\s+to\s+sign",
    r"missing\s+supporting\s+document", r"formal\s+notice",
)
COMPLEX_STUDY_PATTERNS = (
    r"mise\s+en\s+demeure", r"controle\s+fiscal", r"avenant\s+au\s+contrat",
    r"succession", r"litige", r"redressement",
    r"formal\s+notice", r"tax\s+audit", r"contract\s+amendment",
    r"inheritance", r"litigation", r"tax\s+reassessment",
)
DRIVE_SEARCH_STOPWORDS = frozenset(
    {"action", "requise", "valider", "avant", "votre", "merci", "bonjour", "nouveau", "nouvelle"}
    | {"echeance", "required", "validate", "before", "your", "thanks", "hello", "please"}
)


class InboxTriageService:
    """Process newly arrived Gmail messages with surgical pre-filters and 2-speed routing."""

    def __init__(
        self, state_store: StateStore, gmail_service: GmailService,
        knowledge_service: KnowledgeService, drive_service: DriveService,
        agy_manager: AgySessionManager,
        cognitive_fn: Callable[
            [EmailMessage, list[KnowledgeDocument]], tuple[InboxOutputType, str, str | None]
        ] | None = None,
        spawn_live_agy: bool = False, calendar_service: CalendarService | None = None,
        settings: AlfredSettings | None = None,
        attachment_bridge: GmailDriveBridgeService | None = None,
    ) -> None:
        """Initialize the inbox triage service.

        Args:
            state_store: Local atomic state repository facade.
            gmail_service: Gmail API v1 connector.
            knowledge_service: 3-tier YAML frontmatter knowledge loader.
            drive_service: Google Drive metadata index and native reader service.
            agy_manager: Antigravity Web session manager.
            cognitive_fn: Optional LLM cognitive classifier/drafter override.
            spawn_live_agy: When True, spawn live Antigravity Web sessions.
            calendar_service: Optional CalendarService for [HOLD] & invitation Gates.
            settings: Optional AlfredSettings instance.
            attachment_bridge: Optional GmailDriveBridgeService for attachment enrichment.
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
        self._eligibility = InboxEligibilityPolicy(self._settings.inbox_filters)
        self._sanitizer = EmailSanitizer()
        self._firewall = InboxCapabilityFirewall()
        self._attachment_bridge = attachment_bridge or GmailDriveBridgeService(
            self._settings, self._state_store, self._gmail, self._drive
        )

    def poll_new_messages(
        self, spawn_live_agy: bool | None = None, use_cognitive_agy: bool = False
    ) -> list[InboxTriageDecision]:
        """Bookmark Gmail historyId on First-Run or process newly arrived eligible messages.

        Args:
            spawn_live_agy: Optional override to spawn live Antigravity Web Gate sessions.
            use_cognitive_agy: When True, invoke agy CLI cognitive triage on human emails.
        """
        if not self._settings.features.inbox_auto_triage:
            return []
        bookmarks = self._state_store.bookmarks.load()
        if not bookmarks.gmail_history_id:
            return self._reset_history_bookmark()
        try:
            messages, latest_id = self._gmail.list_new_messages_since(bookmarks.gmail_history_id)
        except GmailHistoryExpiredError:
            return self._reset_history_bookmark()

        has_seen = self._state_store.has_processed_item
        unseen = [m for m in messages if not has_seen("gmail", m.message_id)]
        selected, coalesced = self._eligibility.coalesce_by_thread(unseen)
        for older in coalesced:
            self._state_store.record_processed_item(
                "gmail", older.message_id, f"{older.message_id}:coalesced:{older.thread_id}",
                "SKIPPED_COALESCED_THREAD", f"Superseded in thread {older.thread_id}",
            )
        cal_snapshot = (
            load_upcoming_calendar_snapshot(self._calendar, self._settings)
            if use_cognitive_agy and selected else None
        )
        decisions = [
            self.process_message(m, spawn_live_agy, use_cognitive_agy, cal_snapshot)
            for m in selected
        ]
        self._state_store.bookmarks.update(lambda b: b.model_copy(update={
            "gmail_history_id": latest_id, "gmail_last_polled_at": utc_now_iso(),
        }))
        return decisions

    def backfill_silent_archives(self) -> list[str]:
        """Materialize archived Antigravity conversations for synthetic agy_* silent records."""
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
        self, msg: EmailMessage, spawn_live_agy: bool | None = None,
        use_cognitive_agy: bool = False,
        calendar_snapshot: tuple[str, list[CalendarEventItem]] | None = None,
    ) -> InboxTriageDecision:
        """Apply 0-token pre-filters, sanitize HTML/Unicode, and route to 3 inbox outputs.

        Args:
            msg: Normalized incoming Gmail message.
            spawn_live_agy: Optional override to spawn live Antigravity Web Gate sessions.
            use_cognitive_agy: When True, invoke agy CLI cognitive triage on human emails.
            calendar_snapshot: Optional preloaded calendar snapshot shared across a batch.
        """
        effective_spawn = self._spawn_live_agy if spawn_live_agy is None else spawn_live_agy
        is_from_self, is_self_only, user_addrs = resolve_self_email_context(
            msg, self._gmail.get_authenticated_addresses()
        )
        elig = self._eligibility.evaluate(msg, owner_addresses=user_addrs)
        if not elig.eligible:
            return self._record_pre_filtered_skip(msg, elig.reason, effective_spawn)

        sanitized = self._sanitizer.sanitize(msg.raw_html, msg.body_text)
        clean_msg = msg.model_copy(update={"body_text": sanitized.clean_text})
        clean_msg, att_brief, att_inj, has_verified = self._enrich_and_verify_attachments(clean_msg)
        suspicious_inj = sanitized.suspicious_injection or att_inj
        relevant_docs = self._load_relevant_knowledge(clean_msg)
        cal_ctx, upcoming_events = (
            (calendar_snapshot or load_upcoming_calendar_snapshot(self._calendar, self._settings))
            if use_cognitive_agy else ("", [])
        )
        out_t, rsn, draft, cal_act, cls_id, needs_orch, directives = self._classify_message(
            clean_msg, relevant_docs, use_cognitive_agy,
            is_from_self, is_self_only, user_addrs, cal_ctx,
            has_doc_attachment_override=has_verified,
        )
        allowlist = (
            self._calendar.get_effective_hold_allowlist(user_addrs)
            if self._calendar is not None else user_addrs
        )
        blocked_addrs = self._settings.identity.blocked_calendar_attendees(user_addrs)
        enriched_cal = enrich_calendar_action_attendees(
            cal_act, clean_msg, user_addrs, allowlist, sorted(blocked_addrs),
            is_dated_booking=has_dated_booking_signal(clean_msg),
        )
        decision = self._dispatch_triage_output(
            clean_msg, out_t, is_from_self, rsn, relevant_docs, draft, enriched_cal,
            upcoming_events, allowlist, cls_id, effective_spawn, needs_orch,
            suspicious_inj, attachment_brief=att_brief, directives=directives,
        )
        self._state_store.record_processed_item(
            "gmail", msg.message_id, f"{msg.message_id}:{msg.subject}:{decision.output_type}",
            str(decision.output_type), decision.reason,
        )
        return decision

    def _enrich_and_verify_attachments(
        self, clean_msg: EmailMessage
    ) -> tuple[EmailMessage, list[str], bool, bool | None]:
        """Enrich attachments and clear attachment_filenames only when payload is inline-only."""
        if hasattr(self._attachment_bridge, "enrich_email_with_attachments_and_status"):
            res = self._attachment_bridge.enrich_email_with_attachments_and_status(
                clean_msg, self._sanitizer
            )
            enriched, att_brief, att_inj, has_doc = res[0], res[1], res[2], res[3]
            has_non_inline = res[4] if len(res) >= 5 else res[3]
        else:
            enriched, att_brief, att_inj = self._attachment_bridge.enrich_email_with_attachments(
                clean_msg, self._sanitizer
            )
            has_doc, has_non_inline = None, None
        if has_non_inline is False:
            enriched = enriched.model_copy(update={"attachment_filenames": []})
        return (enriched, att_brief, att_inj, has_doc)

    def _record_pre_filtered_skip(
        self, msg: EmailMessage, skip_reason: str, spawn_live: bool = False
    ) -> InboxTriageDecision:
        """Handle deterministic 0-token pre-filtered emails without waking any LLM."""
        if skip_reason == "SKIP_BULK_AUTOMATED":
            decision = self._handle_silent(
                msg, "Pre-filtered automated bulk/newsletter message (0 tokens)",
                spawn_live=spawn_live,
            )
        else:
            decision = InboxTriageDecision(
                message_id=msg.message_id, output_type=InboxOutputType.SILENT_NO_ACTION,
                reason=f"Pre-filtered ({skip_reason})", starred=False,
                session_id=None, session_archived=True, skipped_pre_filter=True,
            )
        self._state_store.record_processed_item(
            "gmail", msg.message_id, f"{msg.message_id}:{skip_reason}", skip_reason, decision.reason
        )
        return decision

    def _dispatch_triage_output(
        self, msg: EmailMessage, output_type: InboxOutputType, is_from_self: bool, reason: str,
        docs: list[KnowledgeDocument], custom_draft: str | None, cal_action: CalendarTriageAction,
        upcoming_events: list[CalendarEventItem], allowlist: frozenset[str],
        cls_conv_id: str | None, spawn_live: bool, needs_orch: bool, suspicious_injection: bool,
        attachment_brief: list[str] | None = None,
        directives: CognitiveTriageDirectives | None = None,
    ) -> InboxTriageDecision:
        """Route classified output to silent, action-required, or draft-reply handler."""
        eff_dir = directives or CognitiveTriageDirectives()
        if output_type == InboxOutputType.SILENT_NO_ACTION:
            return self._handle_silent(
                msg, reason, cal_action, upcoming_events,
                cls_conv_id, spawn_live, directives=eff_dir,
            )
        self._agy.purge_classifier_session(cls_conv_id)
        if output_type == InboxOutputType.USER_ACTION_REQUIRED or is_from_self:
            return self._handle_action_required(
                msg, reason, docs, spawn_live, needs_orch, suspicious_injection,
                attachment_brief, cal_action=cal_action, allowlist=allowlist, directives=eff_dir,
            )
        return self._handle_draft_reply(
            msg, reason, docs, custom_draft, cal_action, allowlist,
            spawn_live, needs_orch, suspicious_injection, directives=eff_dir,
        )

    def _load_relevant_knowledge(self, msg: EmailMessage) -> list[KnowledgeDocument]:
        """Tier-1 -> Tier-2 progressive lookup of reference .md files matching the email."""
        query = f"{msg.sender} {msg.subject} {msg.snippet} {msg.body_text[:200]}"
        matches = self._knowledge.select_relevant_frontmatters(query)[:2]
        return [self._knowledge.load_document(file_name) for file_name, _ in matches]

    def _classify_message(
        self, msg: EmailMessage, docs: list[KnowledgeDocument],
        use_cognitive_agy: bool = False, is_from_self: bool = False, is_self_only: bool = False,
        user_addrs: frozenset[str] = frozenset(), calendar_context: str = "",
        *, has_doc_attachment_override: bool | None = None,
    ) -> tuple[
        InboxOutputType, str, str | None, CalendarTriageAction, str | None, bool,
        CognitiveTriageDirectives,
    ]:
        """Classify an email deterministically first, delegating to cognitive_fn or agy CLI."""
        combined = fold_accents(f"{msg.subject}\n{msg.snippet}\n{msg.body_text}")
        needs_orch = any(re.search(p, combined) for p in COMPLEX_STUDY_PATTERNS)
        no_cal, empty_dir = CalendarTriageAction(), CognitiveTriageDirectives()
        matched_pat = next((p for p in ACTION_REQUIRED_PATTERNS if re.search(p, combined)), None)
        if matched_pat is not None and not use_cognitive_agy and self._cognitive_fn is None:
            rsn = f"Matched user-action signal: '{matched_pat}'"
            return (
                InboxOutputType.USER_ACTION_REQUIRED, rsn, None, no_cal, None, needs_orch, empty_dir
            )
        if is_self_only:
            rsn = "Self-sent memo or personal reminder requiring user follow-up"
            return (
                InboxOutputType.USER_ACTION_REQUIRED, rsn, None, no_cal, None, needs_orch, empty_dir
            )
        email_body, _, _ = split_attachment_body(msg.body_text)
        passive_corpus = fold_accents(f"{msg.subject}\n{msg.snippet}\n{email_body}")
        if (
            matched_pat is None and not is_from_self
            and self._is_passive_or_automated(
                msg, passive_corpus, has_doc_attachment_override=has_doc_attachment_override
            )
        ):
            rsn = "Passive portal or automated bulk notification without action signal"
            return (InboxOutputType.SILENT_NO_ACTION, rsn, None, no_cal, None, False, empty_dir)
        out_type, reason, draft, cal_action, conv_id = self._run_cognitive_or_default(
            msg, docs, use_cognitive_agy, user_addrs, calendar_context
        )
        return finalize_cognitive_triage_decision(
            msg=msg, out_type=out_type, reason=reason, draft=draft, cal_action=cal_action,
            conv_id=conv_id, needs_orch=needs_orch, matched_pat=matched_pat,
            is_from_self=is_from_self, use_cognitive_agy=use_cognitive_agy,
            has_cognitive_fn=self._cognitive_fn is not None,
            directives=self._agy.last_triage_directives,
            last_needs_orch=self._agy.last_needs_orchestrator_study,
            pinned_recipient=self._firewall.pin_draft_recipient(msg),
        )

    def _is_passive_or_automated(
        self,
        msg: EmailMessage,
        combined_folded: str,
        has_doc_attachment_override: bool | None = None,
    ) -> bool:
        """Return True for passive portal notices, newsletters, or non-booking bulk mails."""
        return is_passive_or_automated_email(
            msg,
            combined_folded,
            inbox_filters=self._settings.inbox_filters,
            has_doc_attachment_override=has_doc_attachment_override,
        )

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
                msg, docs, user_addresses=user_addrs, calendar_context=calendar_context
            )
        rsn = "Direct human inquiry requiring a reply draft"
        return (InboxOutputType.DRAFT_REPLY, rsn, None, CalendarTriageAction(), None)

    def _handle_silent(
        self, msg: EmailMessage, reason: str,
        cal_action: CalendarTriageAction | None = None,
        upcoming_events: list[CalendarEventItem] | None = None,
        classifier_conv_id: str | None = None, spawn_live: bool = False,
        *, directives: CognitiveTriageDirectives | None = None,
    ) -> InboxTriageDecision:
        """Handle SILENT_NO_ACTION (0 live sessions unless silent_creates_agy_session)."""
        return materialize_silent_triage_decision(
            self._settings, self._agy, msg, reason, cal_action, upcoming_events,
            classifier_conv_id, spawn_live, directives or CognitiveTriageDirectives(),
        )

    def _handle_draft_reply(
        self, msg: EmailMessage, reason: str, docs: list[KnowledgeDocument],
        custom_draft: str | None, cal_action: CalendarTriageAction | None = None,
        allowlist: frozenset[str] = frozenset(), spawn_live: bool = False,
        needs_orch: bool = False, suspicious_injection: bool = False,
        *, directives: CognitiveTriageDirectives | None = None,
    ) -> InboxTriageDecision:
        """Execute DRAFT_REPLY with pinned recipient, sendAs alias, and clean Gate title."""
        return materialize_draft_reply_decision(
            self._settings, self._gmail, self._agy, msg, reason,
            self._firewall.pin_draft_recipient(msg),
            custom_draft or self._synthesize_default_draft(msg, docs),
            cal_action, allowlist, spawn_live, needs_orch, suspicious_injection,
            directives or CognitiveTriageDirectives(),
        )

    def _handle_action_required(
        self, msg: EmailMessage, reason: str, docs: list[KnowledgeDocument],
        spawn_live: bool = False, needs_orch: bool = False,
        suspicious_injection: bool = False, attachment_brief: list[str] | None = None,
        cal_action: CalendarTriageAction | None = None,
        allowlist: frozenset[str] = frozenset(),
        *, directives: CognitiveTriageDirectives | None = None,
    ) -> InboxTriageDecision:
        """Execute USER_ACTION_REQUIRED: STARRED in Gmail + clean Gate session + brief."""
        eff_dir = directives or CognitiveTriageDirectives()
        if self._settings.features.gmail_drafts_and_send:
            self._gmail.star_message(msg.message_id)
        sel_names = (
            {t.filename.lower() for t in eff_dir.attachments_to_save}
            if eff_dir.attachments_specified else None
        )
        brief_items = build_action_research_brief(
            msg, docs, self._drive, DRIVE_SEARCH_STOPWORDS,
            attachment_brief=attachment_brief, cognitive_brief=eff_dir.research_brief,
            selected_filenames=sel_names,
        )
        target_doc_id = docs[0].frontmatter.id if docs else "profil_et_contacts_cles"
        eff_action = cal_action or CalendarTriageAction()
        cal_sec = (
            render_draft_calendar_section(
                eff_action, msg.thread_id, msg.message_id, allowlist,
                keep_in_inbox=eff_dir.keep_in_inbox,
            ) if self._settings.features.calendar_management else ""
        )
        sel_atts = eff_dir.attachments_to_save if eff_dir.attachments_specified else None
        plan_md = render_action_required_plan(
            msg, reason, brief_items, target_doc_id, needs_orch, suspicious_injection, cal_sec,
            keep_in_inbox=eff_dir.keep_in_inbox,
            drive_watcher_enabled=self._settings.features.drive_watcher,
            attachments_to_save=sel_atts,
            archive_by_default=self._settings.archive_processed_emails_by_default,
        )
        prefix = "[Action · Carbon Study]" if needs_orch else "[Action]"
        session = self._agy.create_session(
            "alfred-inbox", msg.message_id, plan_md, spawn_live, title=f"{prefix} {msg.subject}"
        )
        return InboxTriageDecision(
            message_id=msg.message_id, output_type=InboxOutputType.USER_ACTION_REQUIRED,
            reason=reason, starred=self._settings.features.gmail_drafts_and_send,
            session_id=session.session_id, session_archived=False,
            implementation_plan_markdown=plan_md, research_brief=brief_items,
            suggested_knowledge_update_id=target_doc_id,
            calendar_action_intent=eff_action.intent.value,
            needs_orchestrator_study=needs_orch, suspicious_injection=suspicious_injection,
            keep_in_inbox=eff_dir.keep_in_inbox,
            attachments_to_save=list(eff_dir.attachments_to_save),
        )

    def _synthesize_default_draft(self, msg: EmailMessage, docs: list[KnowledgeDocument]) -> str:
        """Build a concise courteous draft reply grounded in loaded reference context."""
        context_hint = f" ({docs[0].frontmatter.title})" if docs else ""
        return (
            f"Hello,\n\nThank you for your message regarding '{msg.subject}'{context_hint}.\n"
            "I have reviewed your request and will get back to you to confirm.\n\n"
            "Best regards,"
        )
