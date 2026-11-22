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
    start_raw, end_raw = raw.get("start"), raw.get("end")
    start_obj = start_raw if isinstance(start_raw, dict) else {}
    end_obj = end_raw if isinstance(end_raw, dict) else {}
    start_iso = str(start_obj.get("dateTime") or start_obj.get("date") or "")
    end_iso = str(end_obj.get("dateTime") or end_obj.get("date") or "")
    summary = str(raw.get("summary") or "")
    ext_props = raw.get("extendedProperties")
    priv_raw = ext_props.get("private") if isinstance(ext_props, dict) else None
    priv = priv_raw if isinstance(priv_raw, dict) else {}
    is_hold = str(priv.get("alfred_hold") or "").lower() == "true" or bool(
        HOLD_PREFIX_RE.match(summary)
    )
    cand_raw = str(priv.get("alfred_candidate_attendees") or "")
    candidates = normalize_emails(cand_raw.split(",")) if cand_raw else []
    raw_att = raw.get("attendees")
    att_items = raw_att if isinstance(raw_att, list) else []
    att_list = normalize_emails(
        [str(a.get("email") or "") for a in att_items if isinstance(a, dict)]
    )
    raw_rec = raw.get("recurrence")
    rec = [str(r) for r in raw_rec if r is not None] if isinstance(raw_rec, list) else []
    return CalendarEventItem(
        event_id=str(raw.get("id") or ""),
        summary=summary,
        start_iso=start_iso,
        end_iso=end_iso,
        description=str(raw.get("description") or ""),
        location=str(raw.get("location") or ""),
        attendees=att_list,
        recurrence=rec,
        is_hold=is_hold,
        thread_id=str(priv.get("alfred_thread_id") or ""),
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


def fetch_paginated_events(
    client: Any,
    calendar_id: str,
    time_min_iso: str,
    single_events: bool = True,
    time_max_iso: str | None = None,
) -> list[CalendarEventItem]:
    """Fetch paginated Google Calendar events while filtering out cancelled entries.

    Args:
        client: Google Calendar v3 API client resource.
        calendar_id: Resolved Google Calendar identifier.
        time_min_iso: Inclusive lower bound ISO-8601 timestamp.
        single_events: When True, expand recurrences and pass orderBy='startTime'.
        time_max_iso: Optional inclusive upper bound ISO-8601 timestamp.
    """
    events: list[CalendarEventItem] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "timeMin": time_min_iso,
            "singleEvents": single_events,
            "maxResults": 2500,
        }
        if time_max_iso is not None:
            kwargs["timeMax"] = time_max_iso
        if single_events:
            kwargs["orderBy"] = "startTime"
        if page_token:
            kwargs["pageToken"] = page_token
        resp = client.events().list(**kwargs).execute()
        raw_items = resp.get("items")
        for raw in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(raw, dict) or str(raw.get("status") or "").lower() == "cancelled":
                continue
            events.append(parse_event_item(raw))
        next_token = resp.get("nextPageToken")
        if not next_token or str(next_token) in seen_tokens:
            break
        seen_tokens.add(str(next_token))
        page_token = str(next_token)
    return events


def sort_events_chronologically(events: list[CalendarEventItem]) -> list[CalendarEventItem]:
    """Sort CalendarEventItem records chronologically using parsed start timestamps.

    Args:
        events: List of normalized CalendarEventItem records.
    """
    max_dt = datetime.max.replace(tzinfo=UTC)
    return sorted(
        events,
        key=lambda ev: (parse_iso_dt(ev.start_iso) or max_dt, ev.end_iso, ev.summary),
    )


def format_unlimited_calendar_snapshot(
    now_iso: str,
    timezone: str,
    events: list[CalendarEventItem],
) -> str:
    """Render all future calendar events and compact recurring series without horizon caps.

    Args:
        now_iso: Current local ISO-8601 timestamp string.
        timezone: Configured IANA timezone name.
        events: Unlimited future CalendarEventItem list.
    """
    lines = [f"Maintenant ({timezone}) : {now_iso}"]
    single = sort_events_chronologically([ev for ev in events if not ev.recurrence])
    recurring = sort_events_chronologically([ev for ev in events if ev.recurrence])
    for ev in single:
        tag = f"[HOLD thread={ev.thread_id or '-'} id={ev.event_id}] " if ev.is_hold else ""
        lines.append(f"- {ev.start_iso} -> {ev.end_iso} : {tag}{ev.summary}")
    for ev in recurring:
        tag = f"[HOLD thread={ev.thread_id or '-'} id={ev.event_id}] " if ev.is_hold else ""
        rrule_txt = "; ".join(ev.recurrence)
        lines.append(
            f"- [RÉCURRENT {rrule_txt}] {ev.start_iso} -> {ev.end_iso} : {tag}{ev.summary}"
        )
    return "\n".join(lines)
