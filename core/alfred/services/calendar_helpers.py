"""Pure helper functions for Google Calendar payload building, [HOLD] parsing, and matching."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from alfred.domain.calendar_models import CalendarSlotProposal
from alfred.domain.models import CalendarEventItem

HOLD_PREFIX_RE = re.compile(r"^\s*\[hold\]\s*:?\s*", re.IGNORECASE)


def normalize_emails(emails: list[str] | set[str] | frozenset[str]) -> list[str]:
    """Return deduplicated lowercase email addresses preserving first-seen order.

    Args:
        emails: Raw collection of candidate email strings.

    Returns:
        Deduplicated lowercase email addresses containing '@'.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in emails:
        cleaned = raw.strip().lower()
        if "@" in cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def strip_hold_prefix(summary: str) -> str:
    """Remove any leading '[HOLD]' prefix from an event summary.

    Args:
        summary: Event title potentially prefixed with '[HOLD]'.

    Returns:
        Clean summary without '[HOLD]'.
    """
    cleaned = HOLD_PREFIX_RE.sub("", summary).strip()
    return cleaned or summary.strip()


def parse_iso_dt(iso_str: str) -> datetime | None:
    """Parse an ISO date or datetime string into an aware UTC datetime, or None.

    Args:
        iso_str: Candidate ISO-8601 date or datetime string.

    Returns:
        Timezone-aware datetime or None on parse failure.
    """
    try:
        parsed = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def build_time_bounds(start_iso: str, end_iso: str, timezone: str) -> dict[str, Any]:
    """Construct start/end Google Calendar v3 payload dicts for timed or all-day bounds.

    Args:
        start_iso: Start ISO date ('YYYY-MM-DD') or datetime string.
        end_iso: End ISO date ('YYYY-MM-DD') or datetime string.
        timezone: IANA timezone name for timed events.

    Returns:
        Dictionary containing Google Calendar 'start' and 'end' structures.
    """
    is_all_day = "T" not in start_iso and "T" not in end_iso
    start_spec = (
        {"date": start_iso}
        if is_all_day
        else {"dateTime": start_iso, "timeZone": timezone}
    )
    end_spec = (
        {"date": end_iso}
        if is_all_day
        else {"dateTime": end_iso, "timeZone": timezone}
    )
    return {"start": start_spec, "end": end_spec}


def build_hold_payload(
    slot: CalendarSlotProposal,
    timezone: str,
    allowed_guests: list[str],
    all_candidates: list[str],
    thread_id: str,
    message_id: str,
) -> dict[str, Any]:
    """Construct a Google Calendar [HOLD] event JSON payload.

    Args:
        slot: Proposed CalendarSlotProposal.
        timezone: IANA timezone name.
        allowed_guests: Allowlisted guest emails to invite immediately.
        all_candidates: Sanitized candidate emails stored in private extendedProperties.
        thread_id: Linked Gmail threadId.
        message_id: Linked Gmail messageId.

    Returns:
        Google Calendar event resource dictionary.
    """
    body = build_time_bounds(slot.start_iso, slot.end_iso, timezone)
    body.update(
        {
            "summary": f"[HOLD] {strip_hold_prefix(slot.summary)}",
            "description": slot.description,
            "transparency": "opaque",
            "attendees": [{"email": addr} for addr in allowed_guests],
            "extendedProperties": {
                "private": {
                    "alfred_hold": "true",
                    "alfred_thread_id": thread_id,
                    "alfred_message_id": message_id,
                    "alfred_candidate_attendees": ",".join(all_candidates),
                }
            },
        }
    )
    if slot.location:
        body["location"] = slot.location
    return body


def build_confirmed_payload(
    slot: CalendarSlotProposal,
    timezone: str,
    merged_attendees: list[str],
    eff_thread: str,
    matched_hold: CalendarEventItem | None,
) -> dict[str, Any]:
    """Construct a confirmed Google Calendar event JSON payload.

    Args:
        slot: Approved CalendarSlotProposal.
        timezone: IANA timezone name.
        merged_attendees: Sanitized external guest emails.
        eff_thread: Effective Gmail threadId.
        matched_hold: Optional existing [HOLD] event being promoted.

    Returns:
        Google Calendar event resource dictionary.
    """
    body = build_time_bounds(slot.start_iso, slot.end_iso, timezone)
    body.update(
        {
            "summary": strip_hold_prefix(slot.summary),
            "description": slot.description
            or (matched_hold.description if matched_hold is not None else ""),
            "status": "confirmed",
            "transparency": "opaque",
            "attendees": [{"email": addr} for addr in merged_attendees],
            "extendedProperties": {
                "private": {"alfred_hold": "false", "alfred_thread_id": eff_thread}
            },
        }
    )
    if slot.recurrence:
        body["recurrence"] = list(slot.recurrence)
    loc = slot.location or (matched_hold.location if matched_hold is not None else "")
    if loc:
        body["location"] = loc
    return body


def parse_event_item(raw: dict[str, Any]) -> CalendarEventItem:
    """Normalize a raw Google Calendar v3 event resource into a CalendarEventItem.

    Args:
        raw: Google Calendar v3 event JSON dictionary.

    Returns:
        Normalized CalendarEventItem value object.
    """
    start_obj, end_obj = raw.get("start", {}), raw.get("end", {})
    start_iso = str(start_obj.get("dateTime") or start_obj.get("date") or "")
    end_iso = str(end_obj.get("dateTime") or end_obj.get("date") or "")
    summary = str(raw.get("summary", ""))
    priv = raw.get("extendedProperties", {}).get("private", {})
    priv = priv if isinstance(priv, dict) else {}
    is_hold = str(priv.get("alfred_hold", "")).lower() == "true" or bool(
        HOLD_PREFIX_RE.match(summary)
    )
    cand_raw = str(priv.get("alfred_candidate_attendees", ""))
    candidates = normalize_emails(cand_raw.split(",")) if cand_raw else []
    att_list = normalize_emails(
        [str(a.get("email", "")) for a in raw.get("attendees", []) if isinstance(a, dict)]
    )
    raw_rec = raw.get("recurrence", [])
    rec = [str(r) for r in raw_rec] if isinstance(raw_rec, list) else []
    return CalendarEventItem(
        event_id=str(raw.get("id", "")),
        summary=summary,
        start_iso=start_iso,
        end_iso=end_iso,
        description=str(raw.get("description", "")),
        location=str(raw.get("location", "")),
        attendees=att_list,
        recurrence=rec,
        is_hold=is_hold,
        thread_id=str(priv.get("alfred_thread_id", "")),
        candidate_attendees=candidates,
    )


def compute_confirmation_window(start_iso: str, end_iso: str) -> tuple[str, str]:
    """Compute a +/- 21-day ISO search window around start_iso to locate thread [HOLD]s.

    Args:
        start_iso: Proposed slot start ISO string.
        end_iso: Proposed slot end ISO string.

    Returns:
        Tuple of (window_min_iso, window_max_iso).
    """
    anchor = parse_iso_dt(start_iso)
    if anchor is None:
        return (start_iso, end_iso)
    return (
        (anchor - timedelta(days=21)).isoformat(),
        (anchor + timedelta(days=21)).isoformat(),
    )


def match_existing_hold(
    events: list[CalendarEventItem],
    slot: CalendarSlotProposal,
    thread_id: str,
    hold_event_id: str,
) -> CalendarEventItem | None:
    """Find the best matching [HOLD] event by explicit ID, thread+slot, or slot time.

    Args:
        events: Candidate events fetched in the confirmation window.
        slot: Confirmed CalendarSlotProposal.
        thread_id: Optional Gmail threadId associated with the [HOLD]s.
        hold_event_id: Optional explicit Google Calendar [HOLD] event_id.

    Returns:
        Matching CalendarEventItem or None.
    """
    holds = [ev for ev in events if ev.is_hold]
    if hold_event_id:
        for ev in holds:
            if ev.event_id == hold_event_id:
                return ev
    if thread_id:
        for ev in holds:
            if ev.thread_id == thread_id and ev.start_iso == slot.start_iso:
                return ev
        for ev in holds:
            if ev.thread_id == thread_id:
                return ev
    for ev in holds:
        if ev.start_iso == slot.start_iso and ev.end_iso == slot.end_iso:
            return ev
    return None
