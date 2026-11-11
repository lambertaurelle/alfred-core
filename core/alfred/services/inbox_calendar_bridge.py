"""Bridge between alfred-inbox email triage and alfred-calendar [HOLD]/invitation Gates."""

from __future__ import annotations

import functools
import shlex
from datetime import datetime, timedelta
from email.utils import getaddresses
from zoneinfo import ZoneInfo

from alfred.domain.calendar_models import (
    CalendarActionIntent,
    CalendarSlotProposal,
    CalendarTriageAction,
)
from alfred.domain.models import (
    ActiveConversationsState,
    AlfredSettings,
    CalendarEventItem,
    EmailMessage,
)
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.gmail_service import GmailService


def _extract_email_set(raw_header: str) -> set[str]:
    """Extract lowercase email addresses from an RFC 2822 address header string."""
    return {addr.strip().lower() for _, addr in getaddresses([raw_header]) if "@" in addr}


def resolve_self_email_context(
    msg: EmailMessage,
    raw_owner_addrs: object,
) -> tuple[bool, bool, frozenset[str]]:
    """Detect whether msg was sent by the user and whether exclusively to user addresses.

    Args:
        msg: Incoming normalized EmailMessage.
        raw_owner_addrs: Owner address collection returned by GmailService.

    Returns:
        Tuple of (is_from_self, is_self_only, owner_addresses_frozenset).
    """
    user_addrs = raw_owner_addrs if isinstance(raw_owner_addrs, frozenset) else frozenset()
    sender_addrs = _extract_email_set(msg.sender)
    rcpt_addrs = _extract_email_set(f"{msg.recipient}, {msg.headers.get('cc', '')}")
    if user_addrs:
        is_from_self = bool(sender_addrs & user_addrs)
        is_self_only = is_from_self and (not rcpt_addrs or rcpt_addrs.issubset(user_addrs))
        return (is_from_self, is_self_only, user_addrs)
    implicit_self = bool(sender_addrs) and rcpt_addrs == sender_addrs
    return (implicit_self, implicit_self, user_addrs)


def extract_correspondent_emails(
    msg: EmailMessage,
    owner_addresses: frozenset[str],
    allowlist: frozenset[str],
) -> list[str]:
    """Extract relevant thread participant emails (external correspondents + non-self allowlist).

    Args:
        msg: Incoming normalized EmailMessage.
        owner_addresses: Lowercase email addresses owned by the mailbox owner.
        allowlist: Lowercase emails allowlisted for immediate [HOLD] blocks.

    Returns:
        Deduplicated list of participant email addresses.
    """
    raw_headers = [
        msg.headers.get("reply-to", ""),
        msg.sender,
        msg.recipient,
        msg.headers.get("cc", ""),
    ]
    seen: set[str] = set()
    participants: list[str] = []
    for _, addr in getaddresses(raw_headers):
        norm = addr.strip().lower()
        if "@" not in norm or norm in seen:
            continue
        if norm in owner_addresses and norm not in (allowlist - owner_addresses):
            continue
        seen.add(norm)
        participants.append(norm)
    return participants


def enrich_calendar_action_attendees(
    action: CalendarTriageAction,
    msg: EmailMessage,
    owner_addresses: frozenset[str],
    allowlist: frozenset[str],
) -> CalendarTriageAction:
    """Populate default participant emails on slots when omitted by the classifier.

    Args:
        action: Parsed CalendarTriageAction from cognitive triage.
        msg: Incoming EmailMessage providing sender/recipient/cc headers.
        owner_addresses: Authenticated owner email addresses.
        allowlist: Effective [HOLD] allowlist email addresses.

    Returns:
        Enriched CalendarTriageAction with normalized attendees on each slot.
    """
    if action.intent == CalendarActionIntent.NONE or not action.slots:
        return action
    fallback_guests = extract_correspondent_emails(msg, owner_addresses, allowlist)
    updated_slots: list[CalendarSlotProposal] = []
    for slot in action.slots:
        merged: list[str] = []
        for email in [*slot.attendees, *fallback_guests]:
            norm = email.strip().lower()
            if "@" in norm and norm not in merged:
                merged.append(norm)
        updated_slots.append(slot.model_copy(update={"attendees": merged}))
    return action.model_copy(update={"slots": updated_slots})


def load_upcoming_calendar_snapshot(
    calendar_service: CalendarService | None,
    settings: AlfredSettings,
    lookahead_days: int = 14,
) -> tuple[str, list[CalendarEventItem]]:
    """Read upcoming primary calendar events over lookahead_days without mutating state.

    Args:
        calendar_service: Optional wired CalendarService.
        settings: Runtime settings providing timezone and primary_calendar_id.
        lookahead_days: Number of future days to inspect.

    Returns:
        Tuple of (formatted prompt context string, list of upcoming CalendarEventItem).
    """
    if calendar_service is None:
        return ("", [])
    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz).replace(microsecond=0)
    end_local = now_local + timedelta(days=lookahead_days)
    try:
        events = calendar_service.list_events(
            settings.primary_calendar_id,
            now_local.isoformat(),
            end_local.isoformat(),
        )
    except Exception:
        return (f"Maintenant ({settings.timezone}) : {now_local.isoformat()}", [])
    lines = [f"Maintenant ({settings.timezone}) : {now_local.isoformat()}"]
    for ev in events[:40]:
        tag = f"[HOLD thread={ev.thread_id or '-'} id={ev.event_id}] " if ev.is_hold else ""
        lines.append(f"- {ev.start_iso} -> {ev.end_iso} : {tag}{ev.summary}")
    return ("\n".join(lines), events)


def render_draft_calendar_section(
    action: CalendarTriageAction,
    thread_id: str,
    message_id: str,
    allowlist: frozenset[str],
) -> str:
    """Render the [HOLD] or confirmation CLI execution block inside a DRAFT_REPLY Gate plan.

    Args:
        action: Enriched CalendarTriageAction attached to the draft reply.
        thread_id: Gmail threadId for [HOLD] correlation.
        message_id: Gmail messageId for [HOLD] correlation.
        allowlist: Effective [HOLD] allowlist emails.

    Returns:
        Markdown section string (empty when action.intent is NONE).
    """
    if action.intent == CalendarActionIntent.NONE or not action.slots:
        return ""
    lines: list[str] = ["\n## Coordination Agenda (`alfred-calendar`)\n"]
    cli_cmds: list[str] = []
    for slot in action.slots:
        allowed = [a for a in slot.attendees if a in allowlist]
        deferred = [a for a in slot.attendees if a not in allowlist]
        att_flags = " ".join(f"--attendee {shlex.quote(a)}" for a in slot.attendees)
        if action.intent == CalendarActionIntent.CREATE_HOLD:
            allowed_txt = ", ".join(allowed) or "aucun (agenda primary seul)"
            deferred_txt = ", ".join(deferred) or "aucun"
            lines.extend(
                [
                    f"- **`[HOLD] {slot.summary}`** : `{slot.start_iso}` -> `{slot.end_iso}`",
                    f"  - Invités immédiats (allowlist) : `{allowed_txt}`",
                    f"  - Externes différés jusqu'à confirmation : `{deferred_txt}`",
                ]
            )
            cmd = (
                f"alfred create-event --calendar primary --hold "
                f"--summary {shlex.quote(slot.summary)} "
                f"--from {shlex.quote(slot.start_iso)} --to {shlex.quote(slot.end_iso)} "
                f"--thread-id {shlex.quote(thread_id)} --message-id {shlex.quote(message_id)} "
                f"{att_flags} --archive-trace"
            ).strip()
            cli_cmds.append(cmd)
        else:
            guests_txt = ", ".join(slot.attendees) or "aucun"
            inv_flag = (
                "--send-invites" if slot.send_invites and slot.attendees else "--no-send-invites"
            )
            lines.extend(
                [
                    f"- **Événement validé `{slot.summary}`** : "
                    f"`{slot.start_iso}` -> `{slot.end_iso}`",
                    f"  - **Invitation Google Calendar après `Proceed`** : `{guests_txt}`",
                ]
            )
            cmd = (
                f"alfred confirm-event --calendar primary "
                f"--summary {shlex.quote(slot.summary)} "
                f"--from {shlex.quote(slot.start_iso)} --to {shlex.quote(slot.end_iso)} "
                f"--thread-id {shlex.quote(thread_id)} {att_flags} {inv_flag} --archive-trace"
            ).strip()
            cli_cmds.append(cmd)
    lines.append("\n### Commandes Agenda Post-Validation (`Proceed`)\n")
    lines.extend(f"- `{cmd}`" for cmd in cli_cmds)
    return "\n".join(lines) + "\n"


def render_calendar_confirm_gate_plan(
    msg: EmailMessage,
    reason: str,
    action: CalendarTriageAction,
    upcoming_events: list[CalendarEventItem],
) -> str:
    """Render the mandatory Blocking Gate implementation_plan.md for project alfred-calendar.

    Args:
        msg: Incoming confirmation EmailMessage.
        reason: Cognitive rationale explaining why the event is confirmed.
        action: Enriched CalendarTriageAction with CONFIRM_EVENT slots.
        upcoming_events: Upcoming primary CalendarEventItem list to surface matching [HOLD]s.

    Returns:
        Complete Markdown Gate document requiring explicit user Proceed before sending invites.
    """
    thread_holds = [
        ev
        for ev in upcoming_events
        if ev.is_hold and ev.thread_id and ev.thread_id == msg.thread_id
    ]
    lines = [
        "# Validation d'Événement & Envoi d'Invitation Google Calendar (`alfred-calendar`)\n",
        "> [!IMPORTANT]",
        "> **Gate Bloquante Obligatoire** : Aucune invitation Google Calendar n'a été envoyée.",
        "> Vérifiez (ou corrigez en commentaire inline) l'horaire et les destinataires ci-dessous,",
        "> puis cliquez sur **`Proceed`** pour convertir le `[HOLD]` et envoyer l'invitation.\n",
        f"- **E-mail source** : `{msg.message_id}` (Fil `{msg.thread_id}`)",
        f"- **De** : `{msg.sender}` — **Objet** : {msg.subject}",
        f"- **Motif détecté** : {reason}\n",
        "## Créneau(x) Validé(s) & Destinataires de l'Invitation\n",
    ]
    cmds: list[str] = []
    for slot in action.slots:
        guests = ", ".join(slot.attendees) if slot.attendees else "aucun invité externe"
        lines.append(f"- **Titre final** : `{slot.summary}`")
        lines.append(f"- **Horaire** : `{slot.start_iso}` -> `{slot.end_iso}`")
        if slot.location:
            lines.append(f"- **Lieu / Visio** : {slot.location}")
        lines.append(f'- **Personnes invitées (`sendUpdates="all"`)** : `{guests}`')
        att_flags = " ".join(f"--attendee {shlex.quote(a)}" for a in slot.attendees)
        inv_flag = "--send-invites" if slot.send_invites and slot.attendees else "--no-send-invites"
        cmds.append(
            f"alfred confirm-event --calendar primary --summary {shlex.quote(slot.summary)} "
            f"--from {shlex.quote(slot.start_iso)} --to {shlex.quote(slot.end_iso)} "
            f"--thread-id {shlex.quote(msg.thread_id)} {att_flags} {inv_flag}"
        )
    if thread_holds:
        lines.append("\n## `[HOLD]` Existants sur ce Fil (Conversion & Libération Automatique)\n")
        for hold in thread_holds:
            lines.append(
                f"- `{hold.event_id}` : {hold.summary} (`{hold.start_iso}` -> `{hold.end_iso}`)"
            )
    lines.append("\n## Exécution Post-Validation (`Proceed`)\n")
    lines.extend(f"- `{cmd.strip()}`" for cmd in cmds)
    return "\n".join(lines) + "\n"


def render_draft_reply_plan(
    reply_to: str,
    reply_subject: str,
    draft_id: str,
    draft_body: str,
    cal_section: str,
) -> str:
    """Render the Blocking Gate implementation_plan.md for DRAFT_REPLY.

    Args:
        reply_to: Recipient header for the draft reply.
        reply_subject: Subject line for the draft reply.
        draft_id: Created Gmail draft identifier.
        draft_body: Proposed plain-text body.
        cal_section: Optional rendered alfred-calendar [HOLD]/confirmation section.

    Returns:
        Markdown Gate document content.
    """
    return (
        f"# Brouillon de Réponse Gmail (`DRAFT_REPLY`)\n\n"
        f"- **À** : `{reply_to}`\n"
        f"- **Objet** : {reply_subject}\n"
        f"- **Gmail Draft ID** : `{draft_id}`\n\n"
        f"## Brouillon Proposé (Annotable par Commentaires Inline)\n\n"
        f"```text\n{draft_body}\n```\n"
        f"{cal_section}\n"
        f"## Exécution Post-Validation (`Proceed`)\n"
        f"- Mettre à jour et envoyer le brouillon `{draft_id}` dans Gmail.\n"
    )


def render_action_required_plan(
    msg: EmailMessage,
    reason: str,
    brief_items: list[str],
    target_doc_id: str,
) -> str:
    """Render the Blocking Gate implementation_plan.md for USER_ACTION_REQUIRED.

    Args:
        msg: Incoming starred EmailMessage.
        reason: Classification rationale.
        brief_items: Gathered Tier-2 and Google Drive context bullets.
        target_doc_id: Suggested Tier-2 reference document ID for post-action update.

    Returns:
        Markdown Gate document content.
    """
    bullets = "\n".join(f"- {item}" for item in brief_items)
    return (
        f"# Action Utilisateur Requise (`USER_ACTION_REQUIRED`)\n\n"
        f"- **E-mail étoilé (`STARRED`)** : `{msg.message_id}`\n"
        f"- **Expéditeur** : `{msg.sender}`\n"
        f"- **Destinataires** : `{msg.recipient}`\n"
        f"- **Objet** : {msg.subject}\n"
        f"- **Motif** : {reason}\n\n"
        f"## Défrichage Complet & Contexte Rassemblé\n"
        f"{bullets}\n\n"
        f"## Proposition de Mise à Jour Référentiel Post-Action\n"
        f"- Fiche cible : `{target_doc_id}`\n"
    )


def render_silent_trace_markdown(msg: EmailMessage, reason: str) -> str:
    """Format a concise audit summary for an auto-archived SILENT_NO_ACTION conversation.

    Args:
        msg: Processed EmailMessage.
        reason: Classification rationale.

    Returns:
        Markdown audit summary stored in the archived Antigravity trace.
    """
    return (
        f"### Trace Auto-Archivée `SILENT_NO_ACTION`\n"
        f"- **Message ID** : `{msg.message_id}` (Fil `{msg.thread_id}`)\n"
        f"- **De** : `{msg.sender}`\n"
        f"- **Objet** : {msg.subject}\n"
        f"- **Motif** : {reason}\n"
        f"- **Action Gmail** : Aucune modification (`0` brouillon, `0` étoile).\n"
    )


def _without_session(
    state: ActiveConversationsState,
    removed_id: str,
) -> ActiveConversationsState:
    """Return a copy of ActiveConversationsState with removed_id deleted."""
    return state.model_copy(
        update={"sessions": {k: v for k, v in state.sessions.items() if k != removed_id}}
    )


def backfill_silent_inbox_records(
    state_store: StateStore,
    gmail_service: GmailService,
    agy_manager: AgySessionManager,
) -> list[str]:
    """Materialize real Antigravity archived conversations for synthetic agy_* silent records.

    Args:
        state_store: Local StateStore holding active_conversations.json.
        gmail_service: GmailService (or test stub) providing optional get_message().
        agy_manager: AgySessionManager spawning and archiving live traces.

    Returns:
        Ordered list of newly materialized Antigravity conversation IDs.
    """
    current = state_store.conversations.load()
    pending = [
        rec
        for rec in current.sessions.values()
        if rec.workspace == "alfred-inbox"
        and rec.status == "ARCHIVED"
        and rec.session_id.startswith("agy_")
        and rec.outcome.startswith("SILENT_NO_ACTION")
    ]
    migrated: list[str] = []
    for rec in pending:
        msg = (
            gmail_service.get_message(rec.source_message_id)
            if rec.source_message_id and hasattr(gmail_service, "get_message")
            else None
        )
        reason = rec.outcome.removeprefix("SILENT_NO_ACTION:").strip() or rec.outcome
        fallback_msg = msg or EmailMessage(
            message_id=rec.source_message_id or rec.session_id,
            thread_id="",
            sender="(historique)",
            subject=f"Message {rec.source_message_id or rec.session_id}",
        )
        created = agy_manager.create_and_archive_trace(
            workspace="alfred-inbox",
            title=f"[SILENT] {fallback_msg.subject}",
            trace_markdown=render_silent_trace_markdown(fallback_msg, reason),
            source_message_id=rec.source_message_id,
            outcome=rec.outcome,
            spawn_live=True,
            archived_at=rec.archived_at or rec.created_at,
        )
        if created.session_id != rec.session_id and not created.session_id.startswith("agy_"):
            state_store.conversations.update(
                functools.partial(_without_session, removed_id=rec.session_id)
            )
            migrated.append(created.session_id)
    return migrated
