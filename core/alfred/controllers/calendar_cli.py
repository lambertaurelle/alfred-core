"""CLI subcommand registration and handlers for alfred-calendar [HOLD]s, invites, and events."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from alfred.domain.calendar_models import CalendarSlotProposal
from alfred.domain.models import AlfredSettings, CalendarEventItem
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.gmail_service import GmailService


def register_calendar_parsers(sub: Any) -> None:
    """Register Google Calendar listing, [HOLD] allowlist, creation, confirmation, and deletion.

    Args:
        sub: Argparse subparsers collection attached to top-level alfred parser.
    """
    p_list = sub.add_parser("list-events")
    p_list.add_argument("--calendar", default="primary")
    p_list.add_argument("--from", dest="from_iso", required=True)
    p_list.add_argument("--to", dest="to_iso", required=True)

    p_allow = sub.add_parser("hold-allowlist")
    p_allow.add_argument("--list", action="store_true")
    p_allow.add_argument("--add", action="append", default=[])
    p_allow.add_argument("--remove", action="append", default=[])

    p_create = sub.add_parser("create-event")
    p_create.add_argument("--calendar", default="primary")
    p_create.add_argument("--summary", required=True)
    p_create.add_argument("--from", dest="from_iso", required=True)
    p_create.add_argument("--to", dest="to_iso", required=True)
    p_create.add_argument("--hold", action="store_true")
    p_create.add_argument("--attendee", action="append", default=[])
    p_create.add_argument("--no-send-invites", action="store_true")
    p_create.add_argument("--location", default="")
    p_create.add_argument("--description", default="")
    p_create.add_argument("--thread-id", default="")
    p_create.add_argument("--message-id", default="")
    p_create.add_argument("--archive-trace", action="store_true")

    p_conf = sub.add_parser("confirm-event")
    p_conf.add_argument("--calendar", default="primary")
    p_conf.add_argument("--summary", required=True)
    p_conf.add_argument("--from", dest="from_iso", required=True)
    p_conf.add_argument("--to", dest="to_iso", required=True)
    p_conf.add_argument("--attendee", action="append", default=[])
    p_conf.add_argument("--send-invites", dest="send_invites", action="store_true", default=True)
    p_conf.add_argument("--no-send-invites", dest="send_invites", action="store_false")
    p_conf.add_argument("--hold-event-id", default="")
    p_conf.add_argument("--thread-id", default="")
    p_conf.add_argument("--no-release-holds", action="store_true")
    p_conf.add_argument("--location", default="")
    p_conf.add_argument("--description", default="")
    p_conf.add_argument("--archive-trace", action="store_true")
    p_conf.add_argument("--keep-in-inbox", action="store_true")

    p_del = sub.add_parser("delete-event")
    p_del.add_argument("--calendar", default="primary")
    p_del.add_argument("--event-id", required=True)
    p_del.add_argument("--notify", action="store_true")


def resolve_calendar_id(
    cal_spec: str,
    settings: AlfredSettings,
    store: StateStore,
) -> str:
    """Resolve 'primary', 'nounou', or an explicit Google Calendar ID.

    Args:
        cal_spec: User-supplied calendar selector.
        settings: Runtime settings providing primary_calendar_id.
        store: Local StateStore providing nounou_contract and bookmarks.

    Returns:
        Resolved Google Calendar identifier string.
    """
    if cal_spec == "primary":
        return settings.primary_calendar_id
    if cal_spec == "nounou":
        contract_cal_id = (
            store.nounou_contract.load().calendar_id if store.nounou_contract.exists() else ""
        )
        cal_id = contract_cal_id or (store.bookmarks.load().nounou_calendar_id or "")
        if not cal_id:
            raise ValueError(
                "Garde Nounou calendar is not initialized; run 'alfred init-nounou --execute'."
            )
        return cal_id
    return cal_spec


def normalize_calendar_bound(
    raw_iso: str,
    timezone: str,
    is_end: bool,
) -> tuple[str, datetime]:
    """Normalize an ISO date or timestamp into an RFC3339 string and aware datetime.

    Args:
        raw_iso: Raw ISO date ('YYYY-MM-DD') or datetime string.
        timezone: IANA timezone name (e.g. 'Europe/Paris').
        is_end: When True and raw_iso is date-only, use 23:59:59 instead of 00:00:00.

    Returns:
        Tuple of (RFC3339 timestamp string, timezone-aware datetime).
    """
    text = raw_iso.strip()
    tz = ZoneInfo(timezone)
    if "T" not in text and " " not in text:
        day = date.fromisoformat(text)
        hour, minute, second = (23, 59, 59) if is_end else (0, 0, 0)
        aware = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=tz)
        return aware.isoformat(), aware
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        aware = parsed.replace(tzinfo=tz)
        return aware.isoformat(), aware
    return text, parsed


def _normalize_event_iso(raw_iso: str, timezone: str, is_end: bool) -> str:
    """Preserve all-day YYYY-MM-DD strings while attaching timezone to naive datetimes."""
    text = raw_iso.strip()
    if "T" not in text and " " not in text:
        date.fromisoformat(text)
        return text
    iso_str, _ = normalize_calendar_bound(text, timezone, is_end=is_end)
    return iso_str


def _safe_owner_addresses(gmail_service: GmailService | None) -> frozenset[str]:
    """Best-effort lookup of authenticated Gmail owner addresses without failing offline."""
    if gmail_service is None:
        return frozenset()
    try:
        raw = gmail_service.get_authenticated_addresses()
    except Exception:
        return frozenset()
    return raw if isinstance(raw, frozenset) else frozenset()


def handle_list_events(
    calendar_service: CalendarService,
    settings: AlfredSettings,
    store: StateStore,
    timezone: str,
    args: argparse.Namespace,
) -> int:
    """Resolve calendar ID and print listed events within [--from, --to] as JSON.

    Args:
        calendar_service: Wired CalendarService instance.
        settings: Runtime settings.
        store: Local StateStore.
        timezone: Configured IANA timezone.
        args: Parsed CLI namespace.

    Returns:
        POSIX exit status code (0 on success).
    """
    cal_id = resolve_calendar_id(str(args.calendar), settings, store)
    t_min, start_dt = normalize_calendar_bound(str(args.from_iso), timezone, is_end=False)
    t_max, end_dt = normalize_calendar_bound(str(args.to_iso), timezone, is_end=True)
    if end_dt < start_dt:
        raise ValueError(f"Calendar --to '{args.to_iso}' precedes --from '{args.from_iso}'.")
    events = calendar_service.list_events(cal_id, t_min, t_max)
    print(json.dumps([e.model_dump(mode="json") for e in events], indent=2, ensure_ascii=False))
    return 0


def handle_hold_allowlist(
    calendar_service: CalendarService,
    store: StateStore,
    gmail_service: GmailService | None,
    args: argparse.Namespace,
) -> int:
    """List, add, or remove email addresses in the persistent [HOLD] allowlist.

    Args:
        calendar_service: Wired CalendarService instance.
        store: Local StateStore persisting calendar_hold_allowlist.json.
        gmail_service: Optional GmailService providing authenticated owner addresses.
        args: Parsed CLI namespace (--list, --add, --remove).

    Returns:
        POSIX exit status code (0 on success).
    """
    for addr in args.add:
        calendar_service.set_hold_allowlist_entry(str(addr), remove=False)
    for addr in args.remove:
        calendar_service.set_hold_allowlist_entry(str(addr), remove=True)
    owners = _safe_owner_addresses(gmail_service)
    persisted = sorted(store.hold_allowlist.load().emails)
    effective = sorted(calendar_service.get_effective_hold_allowlist(owners))
    payload = {"emails": persisted, "effective_allowlist": effective}
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def handle_create_event(
    calendar_service: CalendarService,
    settings: AlfredSettings,
    store: StateStore,
    gmail_service: GmailService | None,
    agy_manager: AgySessionManager | None,
    timezone: str,
    args: argparse.Namespace,
) -> int:
    """Create a [HOLD] block (respecting allowlist) or a standard Google Calendar event.

    Args:
        calendar_service: Wired CalendarService instance.
        settings: Runtime settings.
        store: Local StateStore.
        gmail_service: Optional GmailService for owner allowlist union.
        agy_manager: Optional AgySessionManager for archived trace creation.
        timezone: Configured IANA timezone.
        args: Parsed CLI namespace.

    Returns:
        POSIX exit status code (0 on success).
    """
    cal_id = resolve_calendar_id(str(args.calendar), settings, store)
    start_iso = _normalize_event_iso(str(args.from_iso), timezone, is_end=False)
    end_iso = _normalize_event_iso(str(args.to_iso), timezone, is_end=True)
    summary = str(args.summary)
    if args.hold:
        slot = CalendarSlotProposal(
            summary=summary,
            start_iso=start_iso,
            end_iso=end_iso,
            attendees=[str(a) for a in args.attendee],
            send_invites=not bool(args.no_send_invites),
            location=str(args.location),
            description=str(args.description),
        )
        ev_id = calendar_service.create_hold_event(
            cal_id,
            slot,
            thread_id=str(args.thread_id),
            message_id=str(args.message_id),
            owner_addresses=_safe_owner_addresses(gmail_service),
        )
    else:
        item = CalendarEventItem(
            summary=summary,
            start_iso=start_iso,
            end_iso=end_iso,
            description=str(args.description),
            location=str(args.location),
        )
        ev_id = calendar_service.insert_event(cal_id, item)

    if args.archive_trace and agy_manager is not None:
        prefix = "[HOLD]" if args.hold else "[EVENT]"
        agy_manager.create_and_archive_trace(
            workspace="alfred-calendar",
            title=f"{prefix} {summary}",
            trace_markdown=f"- **Event ID** : `{ev_id}` (`{start_iso}` -> `{end_iso}`)\n",
            source_message_id=ev_id,
            spawn_live=True,
        )
        agy_manager.daemon_sync.sync_if_needed()
    print(json.dumps({"event_id": ev_id, "calendar_id": cal_id, "hold": bool(args.hold)}, indent=2))
    return 0


def _try_archive_source_thread(
    gmail_service: GmailService | None,
    settings: AlfredSettings,
    thread_id: str,
    keep_in_inbox: bool,
) -> bool:
    """Best-effort archive of a confirmed event's source Gmail thread unless opted out."""
    cleaned = thread_id.strip()
    if (
        gmail_service is None
        or not cleaned
        or keep_in_inbox
        or not settings.archive_processed_emails_by_default
    ):
        return False
    try:
        gmail_service.archive_thread(cleaned, mark_read=True)
        return True
    except Exception:
        return False


def handle_confirm_event(
    calendar_service: CalendarService,
    settings: AlfredSettings,
    store: StateStore,
    agy_manager: AgySessionManager | None,
    timezone: str,
    args: argparse.Namespace,
    gmail_service: GmailService | None = None,
) -> int:
    """Convert a [HOLD] (or insert firm event), send validated invites, and free unused [HOLD]s.

    Args:
        calendar_service: Wired CalendarService instance.
        settings: Runtime settings.
        store: Local StateStore.
        agy_manager: Optional AgySessionManager for archived trace creation.
        timezone: Configured IANA timezone.
        args: Parsed CLI namespace.
        gmail_service: Optional GmailService to archive the source email thread by default.

    Returns:
        POSIX exit status code (0 on success).
    """
    cal_id = resolve_calendar_id(str(args.calendar), settings, store)
    start_iso = _normalize_event_iso(str(args.from_iso), timezone, is_end=False)
    end_iso = _normalize_event_iso(str(args.to_iso), timezone, is_end=True)
    summary = str(args.summary)
    thread_id = str(args.thread_id)
    slot = CalendarSlotProposal(
        summary=summary,
        start_iso=start_iso,
        end_iso=end_iso,
        attendees=[str(a) for a in args.attendee],
        send_invites=bool(args.send_invites),
        location=str(args.location),
        description=str(args.description),
    )
    conf_id, released = calendar_service.confirm_event(
        cal_id,
        slot,
        thread_id=thread_id,
        hold_event_id=str(args.hold_event_id),
        release_thread_holds=not bool(args.no_release_holds),
    )
    email_archived = _try_archive_source_thread(
        gmail_service, settings, thread_id, bool(getattr(args, "keep_in_inbox", False))
    )
    if args.archive_trace and agy_manager is not None:
        agy_manager.create_and_archive_trace(
            workspace="alfred-calendar",
            title=f"[CONFIRMED] {summary}",
            trace_markdown=(
                f"- **Confirmed Event ID** : `{conf_id}` (`{start_iso}` -> `{end_iso}`)\n"
                f"- **Released [HOLD]s** : `{', '.join(released) or 'none'}`\n"
            ),
            source_message_id=conf_id,
            spawn_live=True,
        )
        agy_manager.daemon_sync.sync_if_needed()
    payload = {
        "confirmed_event_id": conf_id,
        "released_hold_ids": released,
        "calendar_id": cal_id,
        "email_thread_archived": email_archived,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def handle_delete_event(
    calendar_service: CalendarService,
    settings: AlfredSettings,
    store: StateStore,
    args: argparse.Namespace,
) -> int:
    """Delete an event from Google Calendar post-Gate validation.

    Args:
        calendar_service: Wired CalendarService instance.
        settings: Runtime settings.
        store: Local StateStore.
        args: Parsed CLI namespace (--calendar, --event-id, --notify).

    Returns:
        POSIX exit status code (0 on success).
    """
    cal_id = resolve_calendar_id(str(args.calendar), settings, store)
    ev_id = str(args.event_id)
    calendar_service.delete_event(
        cal_id,
        ev_id,
        send_updates="all" if bool(args.notify) else "",
    )
    print(json.dumps({"deleted_event_id": ev_id, "calendar_id": cal_id}, indent=2))
    return 0
