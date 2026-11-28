"""Bridge between alfred-inbox email triage, 2-speed Gate plans, and alfred-calendar."""

from __future__ import annotations

import functools
import shlex
from datetime import datetime
from email.utils import getaddresses, parseaddr
from zoneinfo import ZoneInfo

from alfred.domain.attachment_models import CognitiveTriageDirectives
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
    InboxOutputType,
    InboxTriageDecision,
)
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_helpers import format_unlimited_calendar_snapshot
from alfred.services.calendar_service import CalendarService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_booking_detector import is_automated_correspondent_address
from alfred.services.inbox_plan_renderer import (
    render_action_required_plan as render_action_required_plan,
)
from alfred.services.inbox_plan_renderer import (
    render_draft_reply_plan as render_draft_reply_plan,
)
from alfred.services.inbox_plan_renderer import (
    render_silent_trace_markdown as render_silent_trace_markdown,
)


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
    """
    user_addrs = raw_owner_addrs if isinstance(raw_owner_addrs, frozenset) else frozenset()
    lower_hdrs = {k.strip().lower(): v for k, v in msg.headers.items()}
    sender_addrs = _extract_email_set(msg.sender)
    rcpt_addrs = _extract_email_set(f"{msg.recipient}, {lower_hdrs.get('cc', '')}")
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
    """Extract external thread participant emails excluding self/never-invite addresses.

    Args:
        msg: Incoming normalized EmailMessage.
        owner_addresses: Lowercase email addresses owned by the mailbox owner.
        allowlist: Lowercase emails allowlisted for immediate [HOLD] blocks.
    """
    lower_hdrs = {k.strip().lower(): v for k, v in msg.headers.items()}
    raw_headers = [
        lower_hdrs.get("reply-to", ""), msg.sender, msg.recipient, lower_hdrs.get("cc", ""),
    ]
    seen: set[str] = set()
    participants: list[str] = []
    for _, addr in getaddresses(raw_headers):
        norm = addr.strip().lower()
        if "@" not in norm or norm in seen or is_automated_correspondent_address(norm):
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
    never_invite_emails: list[str] | None = None,
    is_dated_booking: bool = False,
) -> CalendarTriageAction:
    """Populate external participant emails when invites are enabled, stripping self/automated.

    Args:
        action: Parsed CalendarTriageAction from cognitive triage.
        msg: Incoming EmailMessage providing sender/recipient/cc headers.
        owner_addresses: Authenticated owner email addresses.
        allowlist: Effective [HOLD] allowlist email addresses.
        never_invite_emails: Optional list of user aliases never invited on Calendar.
        is_dated_booking: When True and slot attendees are empty, force send_invites=False.
    """
    if action.intent == CalendarActionIntent.NONE or not action.slots:
        return action
    blocked = {e.strip().lower() for e in (never_invite_emails or []) if "@" in e}
    blocked.update(e.strip().lower() for e in owner_addresses if "@" in e)
    fallback_guests = extract_correspondent_emails(msg, owner_addresses, allowlist)
    updated_slots: list[CalendarSlotProposal] = []
    for slot in action.slots:
        if is_dated_booking and not slot.attendees:
            updated_slots.append(slot.model_copy(update={"attendees": [], "send_invites": False}))
            continue
        candidates = (
            [*slot.attendees, *fallback_guests] if slot.send_invites else list(slot.attendees)
        )
        merged: list[str] = []
        for email in candidates:
            norm = email.strip().lower()
            if (
                "@" in norm and norm not in merged and norm not in blocked
                and not is_automated_correspondent_address(norm)
            ):
                merged.append(norm)
        updated_slots.append(slot.model_copy(update={"attendees": merged}))
    return action.model_copy(update={"slots": updated_slots})


def load_upcoming_calendar_snapshot(
    calendar_service: CalendarService | None,
    settings: AlfredSettings,
) -> tuple[str, list[CalendarEventItem]]:
    """Read all future primary calendar events and recurrences without horizon truncation.

    Args:
        calendar_service: Optional wired CalendarService.
        settings: Runtime settings providing timezone and primary_calendar_id.
    """
    if calendar_service is None or not settings.features.calendar_management:
        return ("", [])
    tz = ZoneInfo(settings.timezone)
    now_local = datetime.now(tz).replace(microsecond=0)
    try:
        if hasattr(calendar_service, "list_future_events"):
            events = calendar_service.list_future_events(
                settings.primary_calendar_id, now_local.isoformat()
            )
        else:
            far_future = now_local.replace(year=now_local.year + 50).isoformat()
            events = calendar_service.list_events(
                settings.primary_calendar_id, now_local.isoformat(), far_future
            )
    except Exception:
        return (f"Now ({settings.timezone}): {now_local.isoformat()}", [])
    return (
        format_unlimited_calendar_snapshot(now_local.isoformat(), settings.timezone, events),
        events,
    )


def _slot_meta_flags(slot: CalendarSlotProposal) -> str:
    """Build shlex-quoted --location and --description CLI flags for a slot."""
    parts: list[str] = []
    if slot.location:
        parts.append(f"--location {shlex.quote(slot.location)}")
    if slot.description:
        parts.append(f"--description {shlex.quote(slot.description)}")
    return " ".join(parts)


def _build_confirm_cmd(
    slot: CalendarSlotProposal, thread_id: str, keep_in_inbox: bool = False
) -> str:
    """Build a shlex-quoted 'alfred confirm-event' CLI command with --archive-trace."""
    att_flags = " ".join(f"--attendee {shlex.quote(a)}" for a in slot.attendees)
    meta_flags = _slot_meta_flags(slot)
    inv_flag = "--send-invites" if slot.send_invites and slot.attendees else "--no-send-invites"
    keep_flag = "--keep-in-inbox" if keep_in_inbox else ""
    parts = [
        f"alfred confirm-event --calendar primary --summary {shlex.quote(slot.summary)}",
        f"--from {shlex.quote(slot.start_iso)} --to {shlex.quote(slot.end_iso)}",
        f"--thread-id {shlex.quote(thread_id)}",
        *(p for p in (meta_flags, att_flags, inv_flag, keep_flag, "--archive-trace") if p),
    ]
    return " ".join(parts)


def render_draft_calendar_section(
    action: CalendarTriageAction,
    thread_id: str,
    message_id: str,
    allowlist: frozenset[str],
    *,
    keep_in_inbox: bool = False,
) -> str:
    """Render the [HOLD] or confirmation CLI execution block inside a Gate plan.

    Args:
        action: Enriched CalendarTriageAction attached to the draft reply or action plan.
        thread_id: Gmail threadId for [HOLD] correlation.
        message_id: Gmail messageId for [HOLD] correlation.
        allowlist: Effective [HOLD] allowlist emails.
        keep_in_inbox: When True, append --keep-in-inbox to confirm-event commands.
    """
    if action.intent == CalendarActionIntent.NONE or not action.slots:
        return ""
    lines: list[str] = ["\n## Calendar Coordination (`alfred-calendar`)\n"]
    cli_cmds: list[str] = []
    for slot in action.slots:
        allowed = [a for a in slot.attendees if a in allowlist]
        deferred = [a for a in slot.attendees if a not in allowlist]
        if action.intent == CalendarActionIntent.CREATE_HOLD:
            ext_str = ", ".join(deferred) or "none"
            lines.extend([
                f"- **`[HOLD] {slot.summary}`**: `{slot.start_iso}` -> `{slot.end_iso}`",
                f"  - Immediate attendees (allowlist): `{', '.join(allowed) or 'none'}`",
                f"  - External guests deferred until confirmation: `{ext_str}`",
            ])
            att_flags = " ".join(f"--attendee {shlex.quote(a)}" for a in slot.attendees)
            meta_flags = _slot_meta_flags(slot)
            parts = [
                "alfred create-event --calendar primary --hold",
                f"--summary {shlex.quote(slot.summary)}",
                f"--from {shlex.quote(slot.start_iso)} --to {shlex.quote(slot.end_iso)}",
                f"--thread-id {shlex.quote(thread_id)} --message-id {shlex.quote(message_id)}",
                *(p for p in (meta_flags, att_flags, "--archive-trace") if p),
            ]
            cli_cmds.append(" ".join(parts))
        else:
            att_str = ", ".join(slot.attendees) or "none"
            lines.extend([
                f"- **Confirmed event `{slot.summary}`**: `{slot.start_iso}` -> `{slot.end_iso}`",
                f"  - **Google Calendar invite after `Proceed`**: `{att_str}`",
            ])
            cli_cmds.append(_build_confirm_cmd(slot, thread_id, keep_in_inbox=keep_in_inbox))
    lines.append("\n### Post-Approval Calendar Commands (`Proceed`)\n")
    lines.extend(f"- `{cmd}`" for cmd in cli_cmds)
    return "\n".join(lines) + "\n"


def render_calendar_confirm_gate_plan(
    msg: EmailMessage,
    reason: str,
    action: CalendarTriageAction,
    upcoming_events: list[CalendarEventItem],
    *,
    keep_in_inbox: bool = False,
) -> str:
    """Render the mandatory Blocking Gate implementation_plan.md for project alfred-calendar.

    Args:
        msg: Incoming confirmation EmailMessage.
        reason: Cognitive rationale explaining why the event is confirmed.
        action: Enriched CalendarTriageAction with CONFIRM_EVENT slots.
        upcoming_events: Upcoming primary CalendarEventItem list to surface matching [HOLD]s.
        keep_in_inbox: When True, append --keep-in-inbox to confirm-event commands.
    """
    thread_holds = [
        ev for ev in upcoming_events
        if ev.is_hold and ev.thread_id and ev.thread_id == msg.thread_id
    ]
    has_invites = any(s.send_invites and s.attendees for s in action.slots)
    fallback_sender = parseaddr(msg.sender)[0].strip() or "(sender without invite)"
    sender_display = msg.sender if has_invites else fallback_sender
    lines = [
        "# Calendar Event Approval & Google Calendar Invitation (`alfred-calendar`)\n",
        "> [!IMPORTANT]",
        "> **Mandatory Blocking Gate**: Zero Google Calendar invitations have been sent.",
        "> Review the time slot and recipients below, then click **`Proceed`**.\n",
        f"- **Source email**: `{msg.message_id}` (Thread `{msg.thread_id}`)",
        f"- **From**: `{sender_display}` — **Subject**: {msg.subject}",
        f"- **Detected reason**: {reason}\n",
        "## Confirmed Slot(s) & Invitation Recipients\n",
    ]
    cmds: list[str] = []
    for slot in action.slots:
        guests = ", ".join(slot.attendees) if slot.attendees else "no external guests"
        inv_lbl = (
            'Invited attendees (`sendUpdates="all"`)'
            if slot.send_invites and slot.attendees else "External guests (no notifications)"
        )
        lines.extend([
            f"- **Final title**: `{slot.summary}`",
            f"- **Time**: `{slot.start_iso}` -> `{slot.end_iso}`",
            *([f"- **Location / Video**: {slot.location}"] if slot.location else []),
            f"- **{inv_lbl}**: `{guests}`",
        ])
        cmds.append(_build_confirm_cmd(slot, msg.thread_id, keep_in_inbox=keep_in_inbox))
    if thread_holds:
        lines.append("\n## Existing `[HOLD]` Events on This Thread (Auto-Converted & Released)\n")
        for hold in thread_holds:
            lines.append(
                f"- `{hold.event_id}`: {hold.summary} (`{hold.start_iso}` -> `{hold.end_iso}`)"
            )
    lines.append("\n## Post-Approval Execution (`Proceed`)\n")
    lines.extend(f"- `{cmd.strip()}`" for cmd in cmds)
    lines.append(
        "- **Keep in Gmail inbox (`--keep-in-inbox`)**: retain thread in `INBOX` after approval."
        if keep_in_inbox
        else (
            "- **Default Gmail archiving**: remove `INBOX` after approval "
            "(comment `--keep-in-inbox` to keep it in inbox)."
        )
    )
    return "\n".join(lines) + "\n"


def _without_session(state: ActiveConversationsState, removed_id: str) -> ActiveConversationsState:
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
    """
    current = state_store.conversations.load()
    pending = [
        rec for rec in current.sessions.values()
        if rec.workspace == "alfred-inbox" and rec.status == "ARCHIVED"
        and rec.session_id.startswith("agy_") and rec.outcome.startswith("SILENT_NO_ACTION")
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
            sender="(history)",
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


def materialize_silent_triage_decision(
    settings: AlfredSettings, agy_manager: AgySessionManager, msg: EmailMessage,
    reason: str, cal_action: CalendarTriageAction | None,
    upcoming_events: list[CalendarEventItem] | None, classifier_conv_id: str | None,
    spawn_live: bool, directives: CognitiveTriageDirectives,
) -> InboxTriageDecision:
    """Persist an auto-archived silent trace and optional calendar confirm Gate.

    Args:
        settings: Runtime AlfredSettings controlling silent sessions and calendar.
        agy_manager: Antigravity Web session manager.
        msg: Incoming EmailMessage.
        reason: Classification rationale.
        cal_action: Optional CalendarTriageAction.
        upcoming_events: Optional upcoming CalendarEventItem list.
        classifier_conv_id: Optional classifier conversation UUID to reuse or purge.
        spawn_live: Whether live Agy session creation is permitted.
        directives: Validated CognitiveTriageDirectives from cognitive triage.
    """
    allow_live_silent = spawn_live and settings.inbox_filters.silent_creates_agy_session
    if not spawn_live and classifier_conv_id:
        agy_manager.purge_classifier_session(classifier_conv_id)
        classifier_conv_id = None
    session = agy_manager.create_and_archive_trace(
        workspace="alfred-inbox", title=f"[SILENT] {msg.subject}",
        trace_markdown=render_silent_trace_markdown(msg, reason),
        source_message_id=msg.message_id, outcome=f"SILENT_NO_ACTION: {reason}",
        existing_conv_id=classifier_conv_id, spawn_live=allow_live_silent,
    )
    cal_session_id: str | None = None
    eff_action = cal_action or CalendarTriageAction()
    if (
        eff_action.intent == CalendarActionIntent.CONFIRM_EVENT
        and eff_action.slots and settings.features.calendar_management
    ):
        gate_md = render_calendar_confirm_gate_plan(
            msg, reason, eff_action, upcoming_events or [],
            keep_in_inbox=directives.keep_in_inbox,
        )
        cal_session = agy_manager.create_session(
            workspace="alfred-calendar", source_message_id=msg.message_id,
            implementation_plan_md=gate_md, spawn_live=spawn_live,
            title=f"[Calendar] {eff_action.slots[0].summary}",
        )
        cal_session_id = cal_session.session_id
    return InboxTriageDecision(
        message_id=msg.message_id, output_type=InboxOutputType.SILENT_NO_ACTION,
        reason=reason, starred=False, session_id=session.session_id, session_archived=True,
        calendar_session_id=cal_session_id, calendar_action_intent=eff_action.intent.value,
        keep_in_inbox=directives.keep_in_inbox,
        attachments_to_save=list(directives.attachments_to_save),
    )


def materialize_draft_reply_decision(
    settings: AlfredSettings, gmail_service: GmailService, agy_manager: AgySessionManager,
    msg: EmailMessage, reason: str, reply_to: str, draft_body: str,
    cal_action: CalendarTriageAction | None, allowlist: frozenset[str],
    spawn_live: bool, needs_orch: bool, suspicious_injection: bool,
    directives: CognitiveTriageDirectives,
) -> InboxTriageDecision:
    """Create a Gmail draft reply and open its Blocking Gate session in alfred-inbox.

    Args:
        settings: Runtime AlfredSettings controlling Gmail drafts and calendar sections.
        gmail_service: Gmail API connector.
        agy_manager: Antigravity Web session manager.
        msg: Incoming EmailMessage.
        reason: Classification rationale.
        reply_to: Firewall-pinned reply recipient header.
        draft_body: Proposed plain-text draft body.
        cal_action: Optional CalendarTriageAction.
        allowlist: Effective [HOLD] allowlist emails.
        spawn_live: Whether live Agy Gate spawning is enabled.
        needs_orch: Whether Orchestrator Carbon study is requested.
        suspicious_injection: Whether prompt-injection cues were detected.
        directives: Validated CognitiveTriageDirectives from cognitive triage.
    """
    reply_subj = msg.subject if msg.subject.lower().startswith("re:") else f"Re: {msg.subject}"
    parent_id, refs = msg.headers.get("message-id", ""), msg.headers.get("references", "")
    chain = f"{refs} {parent_id}".strip() if parent_id else refs
    draft_id = (
        gmail_service.create_draft(
            msg.thread_id, reply_to, reply_subj, draft_body, parent_id, chain
        )
        if settings.features.gmail_drafts_and_send
        else "SKIPPED_DISABLED"
    )
    eff_action = cal_action or CalendarTriageAction()
    cal_sec = (
        render_draft_calendar_section(
            eff_action, msg.thread_id, msg.message_id, allowlist,
            keep_in_inbox=directives.keep_in_inbox,
        )
        if settings.features.calendar_management else ""
    )
    plan_md = render_draft_reply_plan(
        reply_to, reply_subj, draft_id, draft_body, cal_sec,
        msg.thread_id, needs_orch, suspicious_injection, keep_in_inbox=directives.keep_in_inbox,
    )
    session = agy_manager.create_session(
        "alfred-inbox", msg.message_id, plan_md, spawn_live, title=f"[Draft] {msg.subject}"
    )
    return InboxTriageDecision(
        message_id=msg.message_id, output_type=InboxOutputType.DRAFT_REPLY,
        reason=reason, draft_id=draft_id, draft_body=draft_body, starred=False,
        session_id=session.session_id, session_archived=False,
        implementation_plan_markdown=plan_md, calendar_action_intent=eff_action.intent.value,
        needs_orchestrator_study=needs_orch, suspicious_injection=suspicious_injection,
        keep_in_inbox=directives.keep_in_inbox,
        attachments_to_save=list(directives.attachments_to_save),
    )
