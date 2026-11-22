"""CLI subcommand registration and handlers for alfred-calendar [HOLD]s, FreeBusy, and events."""

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
from alfred.services.gmail_service import GmailService
from alfred.services.specialist_trace_service import SpecialistTraceService


def print_skipped_disabled(feature_name: str, command: str = "") -> int:
    """Print a standardized SKIPPED_DISABLED JSON payload on stdout and return exit code 0.

    Args:
        feature_name: Disabled feature flag identifier in settings.features.
        command: Optional CLI subcommand name that was skipped.
    """
    payload: dict[str, str] = {
        "status": "SKIPPED_DISABLED",
        "feature": feature_name,
        "details": f"Feature '{feature_name}' is disabled in instance/config/settings.yaml.",
    }
    if command:
        payload["command"] = command
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def register_calendar_parsers(sub: Any) -> None:
    """Register Google Calendar listing, FreeBusy, [HOLD] allowlist, and event mutation commands.

    Args:
        sub: Argparse subparsers collection attached to top-level alfred parser.
    """
    p_cals = sub.add_parser("list-calendars")
    p_cals.add_argument("--refresh", action="store_true")

    p_fb = sub.add_parser("query-freebusy")
    p_fb.add_argument("--from", dest="from_iso", required=True)
    p_fb.add_argument("--to", dest="to_iso", required=True)
    p_fb.add_argument("--calendar", action="append", default=[])

    p_list = sub.add_parser("list-events")
    p_list.add_argument("--calendar", default="primary")
    p_list.add_argument("--from", dest="from_iso", required=True)
    p_list.add_argument("--to", dest="to_iso", required=True)

    p_allow = sub.add_parser("hold-allowlist")
    p_allow.add_argument("--list", action="store_true")
    p_allow.add_argument("--add", action="append", default=[])
    p_allow.add_argument("--remove", action="append", default=[])
    p_allow.add_argument("--archive-trace", dest="archive_trace", action="store_true", default=None)
    p_allow.add_argument("--no-archive-trace", dest="archive_trace", action="store_false")

    _register_event_mutation_parsers(sub)


def _register_event_mutation_parsers(sub: Any) -> None:
    """Register create-event, update-event, confirm-event, and delete-event subparsers."""
    p_create = sub.add_parser("create-event")
    p_create.add_argument("--calendar", default="primary")
    p_create.add_argument("--summary", required=True)
    p_create.add_argument("--from", dest="from_iso", required=True)
    p_create.add_argument("--to", dest="to_iso", required=True)
    p_create.add_argument("--hold", action="store_true")
    p_create.add_argument("--attendee", action="append", default=[])
    p_create.add_argument("--rrule", action="append", default=[])
    p_create.add_argument("--no-send-invites", action="store_true")
    p_create.add_argument("--location", default="")
    p_create.add_argument("--description", default="")
    p_create.add_argument("--thread-id", default="")
    p_create.add_argument("--message-id", default="")
    p_create.add_argument(
        "--archive-trace", dest="archive_trace", action="store_true", default=None
    )
    p_create.add_argument("--no-archive-trace", dest="archive_trace", action="store_false")

    p_upd = sub.add_parser("update-event")
    p_upd.add_argument("--calendar", default="primary")
    p_upd.add_argument("--event-id", required=True)
    p_upd.add_argument("--summary", default=None)
    p_upd.add_argument("--from", dest="from_iso", default=None)
    p_upd.add_argument("--to", dest="to_iso", default=None)
    p_upd.add_argument("--location", default=None)
    p_upd.add_argument("--description", default=None)
    p_upd.add_argument("--attendee", action="append", default=None)
    p_upd.add_argument("--rrule", action="append", default=None)
    p_upd.add_argument("--no-send-invites", action="store_true")
    p_upd.add_argument("--archive-trace", dest="archive_trace", action="store_true", default=None)
    p_upd.add_argument("--no-archive-trace", dest="archive_trace", action="store_false")

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
    p_conf.add_argument("--archive-trace", dest="archive_trace", action="store_true", default=None)
    p_conf.add_argument("--no-archive-trace", dest="archive_trace", action="store_false")
    p_conf.add_argument("--keep-in-inbox", action="store_true")

    p_del = sub.add_parser("delete-event")
    p_del.add_argument("--calendar", default="primary")
    p_del.add_argument("--event-id", required=True)
    p_del.add_argument("--notify", action="store_true")
    p_del.add_argument("--archive-trace", dest="archive_trace", action="store_true", default=None)
    p_del.add_argument("--no-archive-trace", dest="archive_trace", action="store_false")


def resolve_calendar_id(cal_spec: str, calendar_service: Any, settings: AlfredSettings) -> str:
    """Resolve 'primary', a calendar summary, or an explicit Google Calendar ID.

    Args:
        cal_spec: User-supplied calendar selector.
        calendar_service: Wired CalendarService or duck-typed double.
        settings: Runtime settings providing primary_calendar_id fallback.
    """
    resolver = getattr(calendar_service, "resolve_calendar_selector", None)
    if callable(resolver):
        return str(resolver(cal_spec))
    cleaned = cal_spec.strip()
    return settings.primary_calendar_id if not cleaned or cleaned.lower() == "primary" else cleaned


def normalize_calendar_bound(raw_iso: str, timezone: str, is_end: bool) -> tuple[str, datetime]:
    """Normalize an ISO date or timestamp into an RFC3339 string and aware datetime.

    Args:
        raw_iso: Raw ISO date ('YYYY-MM-DD') or datetime string.
        timezone: IANA timezone name (e.g. 'Europe/Paris').
        is_end: When True and raw_iso is date-only, use 23:59:59 instead of 00:00:00.
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
    return normalize_calendar_bound(text, timezone, is_end=is_end)[0]


def _safe_owner_addresses(gmail_service: GmailService | None) -> frozenset[str]:
    """Best-effort lookup of authenticated Gmail owner addresses without failing offline."""
    if gmail_service is None:
        return frozenset()
    try:
        raw = gmail_service.get_authenticated_addresses()
    except Exception:
        return frozenset()
    return raw if isinstance(raw, frozenset) else frozenset()


def _normalize_window(from_raw: str, to_raw: str, timezone: str) -> tuple[str, str]:
    """Normalize and validate an ordered [--from, --to] calendar window."""
    t_min, start_dt = normalize_calendar_bound(from_raw, timezone, is_end=False)
    t_max, end_dt = normalize_calendar_bound(to_raw, timezone, is_end=True)
    if end_dt < start_dt:
        raise ValueError(f"Calendar --to '{to_raw}' precedes --from '{from_raw}'.")
    return t_min, t_max


def dispatch_calendar_command(
    cmd: str,
    calendar_service: Any,
    settings: AlfredSettings,
    store: StateStore,
    gmail_service: GmailService | None,
    agy_manager: AgySessionManager | None,
    timezone: str,
    args: argparse.Namespace,
) -> int:
    """Dispatch any alfred-calendar CLI command with unified feature-flag gating.

    Args:
        cmd: Parsed calendar subcommand name.
        calendar_service: Wired CalendarService or duck-typed double.
        settings: Runtime settings including features.calendar_management.
        store: Local StateStore persisting [HOLD] allowlist entries.
        gmail_service: Optional GmailService for owner addresses and thread archiving.
        agy_manager: Optional AgySessionManager for archived trace creation.
        timezone: Configured IANA timezone.
        args: Parsed CLI namespace.
    """
    if not settings.features.calendar_management:
        return print_skipped_disabled("calendar_management", cmd)
    if cmd == "list-calendars":
        entries = calendar_service.list_calendars(refresh=bool(args.refresh))
        payload: Any = [e.model_dump(mode="json") for e in entries]
    elif cmd == "query-freebusy":
        t_min, t_max = _normalize_window(str(args.from_iso), str(args.to_iso), timezone)
        cal_ids = [str(c) for c in args.calendar if str(c).strip()] or None
        ivs = calendar_service.query_freebusy(t_min, t_max, calendar_ids=cal_ids)
        payload = [iv.model_dump(mode="json") for iv in ivs]
    elif cmd == "list-events":
        cal_id = resolve_calendar_id(str(args.calendar), calendar_service, settings)
        t_min, t_max = _normalize_window(str(args.from_iso), str(args.to_iso), timezone)
        events = calendar_service.list_events(cal_id, t_min, t_max)
        payload = [e.model_dump(mode="json") for e in events]
    elif cmd == "hold-allowlist":
        payload = _exec_hold_allowlist(
            calendar_service, store, gmail_service, agy_manager, settings, args
        )
    elif cmd == "create-event":
        payload = _exec_create_event(
            calendar_service, settings, gmail_service, agy_manager, timezone, args
        )
    elif cmd == "update-event":
        payload = _exec_update_event(
            calendar_service, settings, gmail_service, agy_manager, timezone, args
        )
    elif cmd == "confirm-event":
        payload = _exec_confirm_event(
            calendar_service, settings, gmail_service, agy_manager, timezone, args
        )
    else:
        cal_id = resolve_calendar_id(str(args.calendar), calendar_service, settings)
        ev_id = str(args.event_id)
        calendar_service.delete_event(cal_id, ev_id, send_updates="all" if args.notify else "")
        _surface_calendar_trace(
            agy_manager, settings, f"[DELETE] {ev_id}",
            f"- **Deleted Event ID** : `{ev_id}` (`{cal_id}`)\n", ev_id, args,
        )
        payload = {"deleted_event_id": ev_id, "calendar_id": cal_id}
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _exec_hold_allowlist(
    calendar_service: Any,
    store: StateStore,
    gmail_service: GmailService | None,
    agy_manager: AgySessionManager | None,
    settings: AlfredSettings,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Apply --add / --remove mutations and return current [HOLD] allowlist dictionary."""
    for addr in args.add:
        calendar_service.set_hold_allowlist_entry(str(addr), remove=False)
    for addr in args.remove:
        calendar_service.set_hold_allowlist_entry(str(addr), remove=True)
    mutated = [*(str(a) for a in args.add), *(str(r) for r in args.remove)]
    if mutated:
        _surface_calendar_trace(
            agy_manager, settings, f"[ALLOWLIST] {', '.join(mutated)}",
            f"- **Added** : `{args.add}`\n- **Removed** : `{args.remove}`\n",
            mutated[0], args,
        )
    owners = _safe_owner_addresses(gmail_service)
    return {
        "emails": sorted(store.hold_allowlist.load().emails),
        "effective_allowlist": sorted(calendar_service.get_effective_hold_allowlist(owners)),
    }


def _exec_create_event(
    calendar_service: Any,
    settings: AlfredSettings,
    gmail_service: GmailService | None,
    agy_manager: AgySessionManager | None,
    timezone: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Create a [HOLD] block or firm Google Calendar event and surface trace if needed."""
    cal_id = resolve_calendar_id(str(args.calendar), calendar_service, settings)
    start_iso = _normalize_event_iso(str(args.from_iso), timezone, is_end=False)
    end_iso = _normalize_event_iso(str(args.to_iso), timezone, is_end=True)
    summary = str(args.summary)
    owners = _safe_owner_addresses(gmail_service)
    if args.hold:
        slot = CalendarSlotProposal(
            summary=summary, start_iso=start_iso, end_iso=end_iso,
            attendees=[str(a) for a in args.attendee],
            send_invites=not bool(args.no_send_invites),
            location=str(args.location), description=str(args.description),
        )
        ev_id = calendar_service.create_hold_event(
            cal_id, slot, thread_id=str(args.thread_id),
            message_id=str(args.message_id), owner_addresses=owners,
        )
    else:
        item = CalendarEventItem(
            summary=summary, start_iso=start_iso, end_iso=end_iso,
            description=str(args.description), location=str(args.location),
            attendees=[str(a) for a in args.attendee],
            recurrence=[str(r) for r in args.rrule],
        )
        ev_id = calendar_service.insert_event(
            cal_id, item, send_invites=not bool(args.no_send_invites), owner_addresses=owners
        )
    _surface_calendar_trace(
        agy_manager, settings, f"{'[HOLD]' if args.hold else '[EVENT]'} {summary}",
        f"- **Event ID** : `{ev_id}` (`{start_iso}` -> `{end_iso}`)\n", str(ev_id), args,
    )
    return {"event_id": str(ev_id), "calendar_id": cal_id, "hold": bool(args.hold)}


def _exec_update_event(
    calendar_service: Any,
    settings: AlfredSettings,
    gmail_service: GmailService | None,
    agy_manager: AgySessionManager | None,
    timezone: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Patch an existing Google Calendar event and return serialized dictionary."""
    cal_id = resolve_calendar_id(str(args.calendar), calendar_service, settings)
    s_iso = (
        _normalize_event_iso(str(args.from_iso), timezone, False) if args.from_iso else None
    )
    e_iso = _normalize_event_iso(str(args.to_iso), timezone, True) if args.to_iso else None
    attendees = [str(a) for a in args.attendee] if args.attendee is not None else None
    recurrence = [str(r) for r in args.rrule] if args.rrule is not None else None
    ev_id = str(args.event_id)
    updated = calendar_service.update_event(
        cal_id, ev_id, summary=args.summary, start_iso=s_iso, end_iso=e_iso,
        description=args.description, location=args.location, attendees=attendees,
        recurrence=recurrence, send_invites=not bool(args.no_send_invites),
        owner_addresses=_safe_owner_addresses(gmail_service),
    )
    _surface_calendar_trace(
        agy_manager, settings, f"[UPDATE] {args.summary or ev_id}",
        f"- **Updated Event ID** : `{ev_id}` (`{cal_id}`)\n", ev_id, args,
    )
    if isinstance(updated, CalendarEventItem):
        return updated.model_dump(mode="json")
    return {"event_id": str(updated), "calendar_id": cal_id, "summary": str(args.summary or "")}


def _exec_confirm_event(
    calendar_service: Any,
    settings: AlfredSettings,
    gmail_service: GmailService | None,
    agy_manager: AgySessionManager | None,
    timezone: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Confirm a [HOLD] slot, release sibling holds, and archive source email thread."""
    cal_id = resolve_calendar_id(str(args.calendar), calendar_service, settings)
    start_iso = _normalize_event_iso(str(args.from_iso), timezone, is_end=False)
    end_iso = _normalize_event_iso(str(args.to_iso), timezone, is_end=True)
    summary, thread_id = str(args.summary), str(args.thread_id)
    slot = CalendarSlotProposal(
        summary=summary, start_iso=start_iso, end_iso=end_iso,
        attendees=[str(a) for a in args.attendee], send_invites=bool(args.send_invites),
        location=str(args.location), description=str(args.description),
    )
    conf_id, released = calendar_service.confirm_event(
        cal_id, slot, thread_id=thread_id, hold_event_id=str(args.hold_event_id),
        release_thread_holds=not bool(args.no_release_holds),
        owner_addresses=_safe_owner_addresses(gmail_service),
    )
    email_archived = _try_archive_source_thread(
        gmail_service, settings, thread_id, bool(getattr(args, "keep_in_inbox", False))
    )
    _surface_calendar_trace(
        agy_manager, settings, f"[CONFIRMED] {summary}",
        f"- **Confirmed** : `{conf_id}` (`{start_iso}` -> `{end_iso}`)\n", str(conf_id), args,
    )
    return {
        "confirmed_event_id": conf_id, "released_hold_ids": released,
        "calendar_id": cal_id, "email_thread_archived": email_archived,
    }


def _surface_calendar_trace(
    agy_manager: AgySessionManager | None,
    settings: AlfredSettings,
    title: str,
    trace_md: str,
    source_id: str,
    args: argparse.Namespace,
) -> None:
    """Surface a visible EXECUTED trace in alfred-calendar unless suppressed/deduplicated."""
    SpecialistTraceService().surface_mutation_trace(
        agy_manager=agy_manager,
        settings=settings,
        target_role="calendar",
        title=title,
        trace_markdown=trace_md,
        source_id=source_id,
        archive_trace_flag=getattr(args, "archive_trace", None),
    )


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
