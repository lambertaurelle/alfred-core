"""alfred-inbox new email routine with 3 exclusive outputs (SILENT / DRAFT / ACTION)."""

from __future__ import annotations

import re
from collections.abc import Callable

from alfred.domain.models import (
    EmailMessage,
    InboxOutputType,
    InboxTriageDecision,
    KnowledgeDocument,
    fold_accents,
    utc_now_iso,
)
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailHistoryExpiredError, GmailService
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
    {
        "action",
        "requise",
        "valider",
        "avant",
        "votre",
        "merci",
        "bonjour",
        "nouveau",
        "nouvelle",
        "echeance",
    }
)


class InboxTriageService:
    """Process newly arrived Gmail messages into 3 exclusive V7.1 Lean outputs."""

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
    ) -> None:
        """Initialize the inbox triage service.

        Args:
            state_store: Local atomic state repository facade.
            gmail_service: Gmail API v1 connector.
            knowledge_service: 3-tier YAML frontmatter knowledge loader.
            drive_service: Google Drive metadata index and native reader service.
            agy_manager: Antigravity Web session manager and silent auto-archiver.
            cognitive_fn: Optional LLM cognitive classifier/drafter override.
            spawn_live_agy: When True, spawn live Antigravity Web sessions on DRAFT/ACTION.
        """
        self._state_store = state_store
        self._gmail = gmail_service
        self._knowledge = knowledge_service
        self._drive = drive_service
        self._agy = agy_manager
        self._cognitive_fn = cognitive_fn
        self._spawn_live_agy = spawn_live_agy

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
        """Route one incoming email into SILENT_NO_ACTION, DRAFT_REPLY, or USER_ACTION_REQUIRED.

        Args:
            msg: Normalized incoming Gmail message.
            spawn_live_agy: Optional override to spawn live Antigravity Web Gate sessions.
            use_cognitive_agy: When True, invoke agy CLI cognitive triage on human emails.

        Returns:
            Completed InboxTriageDecision with Gmail & Antigravity side effects applied.
        """
        effective_spawn = self._spawn_live_agy if spawn_live_agy is None else spawn_live_agy
        relevant_docs = self._load_relevant_knowledge(msg)
        output_type, reason, custom_draft = self._classify_message(
            msg, relevant_docs, use_cognitive_agy=use_cognitive_agy
        )

        if output_type == InboxOutputType.SILENT_NO_ACTION:
            decision = self._handle_silent(msg, reason)
        elif output_type == InboxOutputType.USER_ACTION_REQUIRED:
            decision = self._handle_action_required(
                msg, reason, relevant_docs, spawn_live=effective_spawn
            )
        else:
            decision = self._handle_draft_reply(
                msg, reason, relevant_docs, custom_draft, spawn_live=effective_spawn
            )

        self._state_store.record_processed_item(
            item_type="gmail",
            item_id=msg.message_id,
            payload_for_hash=f"{msg.message_id}:{msg.subject}:{decision.output_type}",
            outcome=str(decision.output_type),
            details=decision.reason,
        )
        return decision

    def _load_relevant_knowledge(self, msg: EmailMessage) -> list[KnowledgeDocument]:
        """Tier-1 -> Tier-2 progressive lookup of reference .md files matching the email."""
        query = f"{msg.subject} {msg.snippet} {msg.body_text[:200]}"
        matches = self._knowledge.select_relevant_frontmatters(query)[:2]
        docs: list[KnowledgeDocument] = []
        for file_name, _ in matches:
            docs.append(self._knowledge.load_document(file_name))
        return docs

    def _classify_message(
        self,
        msg: EmailMessage,
        docs: list[KnowledgeDocument],
        use_cognitive_agy: bool = False,
    ) -> tuple[InboxOutputType, str, str | None]:
        """Classify an email deterministically first, delegating to cognitive_fn or agy CLI."""
        combined = fold_accents(f"{msg.subject}\n{msg.snippet}\n{msg.body_text}")
        for pat in ACTION_REQUIRED_PATTERNS:
            if re.search(pat, combined):
                return (
                    InboxOutputType.USER_ACTION_REQUIRED,
                    f"Matched user-action signal: '{pat}'",
                    None,
                )

        has_unsub = "list-unsubscribe" in {k.lower() for k in msg.headers}
        auto_sub = msg.headers.get("auto-submitted", "no").lower() != "no"
        sender_lower = msg.sender.lower()
        is_noreply = (
            "no-reply" in sender_lower
            or "ne-pas-repondre" in sender_lower
            or "noreply" in sender_lower
        )
        for pat in PASSIVE_PORTAL_PATTERNS:
            if re.search(pat, combined):
                return (
                    InboxOutputType.SILENT_NO_ACTION,
                    "Passive portal or routine subscription notification without anomaly",
                    None,
                )
        if has_unsub or auto_sub or is_noreply:
            return (
                InboxOutputType.SILENT_NO_ACTION,
                "Automated bulk/newsletter header or no-reply sender without action signal",
                None,
            )

        if self._cognitive_fn is not None:
            return self._cognitive_fn(msg, docs)
        if use_cognitive_agy:
            return self._agy.classify_email_with_agy(msg, docs)

        return (
            InboxOutputType.DRAFT_REPLY,
            "Direct human inquiry requiring a reply draft",
            None,
        )

    def _handle_silent(self, msg: EmailMessage, reason: str) -> InboxTriageDecision:
        """Execute SILENT_NO_ACTION: 0 Gmail edits + immediate Antigravity auto-archive."""
        session = self._agy.create_session(
            workspace="alfred-inbox",
            source_message_id=msg.message_id,
            spawn_live=False,
        )
        self._agy.auto_archive_silent_session(session.session_id, reason=reason)
        return InboxTriageDecision(
            message_id=msg.message_id,
            output_type=InboxOutputType.SILENT_NO_ACTION,
            reason=reason,
            starred=False,
            session_id=session.session_id,
            session_archived=True,
        )

    def _handle_draft_reply(
        self,
        msg: EmailMessage,
        reason: str,
        docs: list[KnowledgeDocument],
        custom_draft: str | None,
        spawn_live: bool = False,
    ) -> InboxTriageDecision:
        """Execute DRAFT_REPLY: create Gmail draft + active Gate implementation_plan.md."""
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
        plan_md = (
            f"# Brouillon de Réponse Gmail (`DRAFT_REPLY`)\n\n"
            f"- **À** : `{reply_to}`\n"
            f"- **Objet** : {reply_subject}\n"
            f"- **Gmail Draft ID** : `{draft_id}`\n\n"
            f"## Brouillon Proposé (Annotable par Commentaires Inline)\n\n"
            f"```text\n{draft_body}\n```\n\n"
            f"## Exécution Post-Validation (`Proceed`)\n"
            f"- Mettre à jour et envoyer le brouillon `{draft_id}` dans Gmail.\n"
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
        bullets = "\n".join(f"- {item}" for item in brief_items)
        plan_md = (
            f"# Action Utilisateur Requise (`USER_ACTION_REQUIRED`)\n\n"
            f"- **E-mail étoilé (`STARRED`)** : `{msg.message_id}`\n"
            f"- **Expéditeur** : `{msg.sender}`\n"
            f"- **Objet** : {msg.subject}\n"
            f"- **Motif** : {reason}\n\n"
            f"## Défrichage Complet & Contexte Rassemblé\n"
            f"{bullets}\n\n"
            f"## Proposition de Mise à Jour Référentiel Post-Action\n"
            f"- Fiche cible : `{target_doc_id}`\n"
        )
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
