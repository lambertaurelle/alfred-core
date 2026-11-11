"""Google Calendar API v3 connector for primary [HOLD]/confirmed events and Garde Nounou."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from alfred.domain.calendar_models import CalendarSlotProposal
from alfred.domain.models import AlfredPaths, AlfredSettings, CalendarEventItem
from alfred.repositories.state_store import StateStore
from alfred.services.google_auth import GoogleOAuthService

HOLD_PREFIX_RE = re.compile(r"^\s*\[hold\]\s*:?\s*", re.IGNORECASE)


def _normalize_emails(emails: list[str] | set[str] | frozenset[str]) -> list[str]:
    """Return deduplicated lowercase email addresses preserving first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in emails:
        cleaned = raw.strip().lower()
        if "@" in cleaned and cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def _strip_hold_prefix(summary: str) -> str:
    """Remove any leading '[HOLD]' prefix from an event summary."""
    cleaned = HOLD_PREFIX_RE.sub("", summary).strip()
    return cleaned or summary.strip()


def _parse_iso_dt(iso_str: str) -> datetime | None:
    """Parse an ISO date or datetime string into an aware UTC datetime, or None."""
    try:
        parsed = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


class CalendarService:
    """Google Calendar v3 service managing Garde Nounou, allowlisted [HOLD]s, and invites."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        calendar_client: Any | None = None,
        oauth_service: GoogleOAuthService | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        """Initialize the Google Calendar service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings.
            calendar_client: Optional injected Google Calendar v3 client.
            oauth_service: Optional shared GoogleOAuthService instance.
            state_store: Optional StateStore providing persistent [HOLD] allowlist.
        """
        self._paths = paths
        self._settings = settings
        self._calendar_client = calendar_client
        self._oauth = oauth_service or GoogleOAuthService(paths, settings)
        self._state_store = state_store

    def get_effective_hold_allowlist(
        self,
        owner_addresses: frozenset[str] = frozenset(),
    ) -> frozenset[str]:
        """Compute lowercase emails allowed to receive immediate [HOLD] calendar invites.

        Args:
            owner_addresses: Authenticated user email addresses resolved via Gmail API.

        Returns:
            Frozenset of allowlisted lowercase email addresses.
        """
        persisted = (
            self._state_store.hold_allowlist.load().emails
            if self._state_store is not None
            else []
        )
        allowed = set(_normalize_emails(persisted))
        if self._settings.calendar.include_authenticated_owners_in_hold_allowlist:
            allowed.update(_normalize_emails(owner_addresses))
        return frozenset(allowed)

    def set_hold_allowlist_entry(self, email: str, remove: bool = False) -> list[str]:
        """Add or remove an email address in the persistent [HOLD] allowlist state.

        Args:
            email: Target email address to allowlist or remove.
            remove: When True, remove email instead of adding it.

        Returns:
            Updated sorted list of persisted allowlist emails.

        Raises:
            RuntimeError: If StateStore is not wired on CalendarService.
        """
        if self._state_store is None:
            raise RuntimeError("StateStore is not wired on CalendarService.")
        norm = email.strip().lower()
        if "@" not in norm:
            raise ValueError(f"Invalid email address for [HOLD] allowlist: '{email}'")
        updated = self._state_store.hold_allowlist.update(
            lambda cur: cur.model_copy(
                update={
                    "emails": sorted(
                        set(_normalize_emails(cur.emails)) - {norm}
                        if remove
                        else set(_normalize_emails(cur.emails)) | {norm}
                    )
                }
            )
        )
        return list(updated.emails)

    def ensure_calendar(self, calendar_name: str) -> str:
        """Find an existing Google Calendar by summary across pages or create it idempotently.

        Args:
            calendar_name: Display name of the calendar (e.g. 'Garde Nounou').

        Returns:
            Google Calendar identifier.
        """
        client = self._require_client()
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {}
            if page_token:
                kwargs["pageToken"] = page_token
            listing = client.calendarList().list(**kwargs).execute()
            for entry in listing.get("items", []):
                if str(entry.get("summary", "")) == calendar_name:
                    return str(entry["id"])
            page_token = listing.get("nextPageToken")
            if not page_token:
                break
        created = (
            client.calendars()
            .insert(body={"summary": calendar_name, "timeZone": self._settings.timezone})
            .execute()
        )
        return str(created["id"])

    def list_events(
        self,
        calendar_id: str,
        time_min_iso: str,
        time_max_iso: str,
    ) -> list[CalendarEventItem]:
        """List expanded single events across all pages within [time_min_iso, time_max_iso].

        Args:
            calendar_id: Target Google Calendar identifier.
            time_min_iso: Inclusive lower bound ISO-8601 timestamp.
            time_max_iso: Inclusive upper bound ISO-8601 timestamp.

        Returns:
            Ordered list of CalendarEventItem objects including [HOLD] metadata.
        """
        client = self._require_client()
        events: list[CalendarEventItem] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "calendarId": calendar_id,
                "timeMin": time_min_iso,
                "timeMax": time_max_iso,
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 2500,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = client.events().list(**kwargs).execute()
            for raw in resp.get("items", []):
                events.append(self._parse_event_item(raw))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return events

    def insert_event(self, calendar_id: str, event: CalendarEventItem) -> str:
        """Insert a timed or all-day calendar event and return its Google Calendar event_id.

        Args:
            calendar_id: Target Google Calendar identifier.
            event: CalendarEventItem specification to create.

        Returns:
            Created Google Calendar event_id.
        """
        client = self._require_client()
        body = self._build_time_bounds(event.start_iso, event.end_iso)
        body["summary"] = event.summary
        body["description"] = event.description
        if event.location:
            body["location"] = event.location
        created = client.events().insert(calendarId=calendar_id, body=body).execute()
        return str(created["id"])

    def create_hold_event(
        self,
        calendar_id: str,
        slot: CalendarSlotProposal,
        thread_id: str = "",
        message_id: str = "",
        owner_addresses: frozenset[str] = frozenset(),
    ) -> str:
        """Create an opaque [HOLD] block sending invites strictly to allowlisted attendees.

        Args:
            calendar_id: Target Google Calendar identifier (typically 'primary').
            slot: Proposed CalendarSlotProposal to block tentatively.
            thread_id: Optional Gmail threadId linked to this [HOLD].
            message_id: Optional Gmail messageId linked to this [HOLD].
            owner_addresses: Authenticated owner email addresses for allowlist union.

        Returns:
            Created (or deduplicated existing) Google Calendar event_id.
        """
        hold_summary = f"[HOLD] {_strip_hold_prefix(slot.summary)}"
        existing = self.list_events(calendar_id, slot.start_iso, slot.end_iso)
        for ev in existing:
            if (
                ev.summary == hold_summary
                and ev.start_iso == slot.start_iso
                and ev.end_iso == slot.end_iso
            ):
                return ev.event_id

        allowlist = self.get_effective_hold_allowlist(owner_addresses)
        all_candidates = _normalize_emails(slot.attendees)
        allowed_guests = [addr for addr in all_candidates if addr in allowlist]

        body = self._build_time_bounds(slot.start_iso, slot.end_iso)
        body.update(
            {
                "summary": hold_summary,
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
        send_updates = "all" if (allowed_guests and slot.send_invites) else "none"
        client = self._require_client()
        created = (
            client.events()
            .insert(calendarId=calendar_id, body=body, sendUpdates=send_updates)
            .execute()
        )
        return str(created["id"])

    def confirm_event(
        self,
        calendar_id: str,
        slot: CalendarSlotProposal,
        thread_id: str = "",
        hold_event_id: str = "",
        release_thread_holds: bool = True,
    ) -> tuple[str, list[str]]:
        """Convert a [HOLD] (or create a firm event), invite guests, and free unused [HOLD]s.

        Args:
            calendar_id: Target Google Calendar identifier.
            slot: Validated CalendarSlotProposal approved via Blocking Gate.
            thread_id: Optional Gmail threadId used to match and release alternative [HOLD]s.
            hold_event_id: Optional explicit [HOLD] event_id to convert.
            release_thread_holds: When True, remove unchosen [HOLD] blocks on thread_id.

        Returns:
            Tuple of (confirmed_event_id, list of released alternative hold event_ids).
        """
        window_events = self._list_confirmation_window(calendar_id, slot.start_iso, slot.end_iso)
        matched_hold = self._match_existing_hold(window_events, slot, thread_id, hold_event_id)
        merged_attendees = _normalize_emails(
            slot.attendees
            if slot.attendees
            else (matched_hold.candidate_attendees if matched_hold is not None else [])
        )
        eff_thread = thread_id or (matched_hold.thread_id if matched_hold is not None else "")
        body = self._build_time_bounds(slot.start_iso, slot.end_iso)
        body.update(
            {
                "summary": _strip_hold_prefix(slot.summary),
                "description": slot.description
                or (matched_hold.description if matched_hold is not None else ""),
                "status": "confirmed",
                "transparency": "opaque",
                "attendees": [{"email": addr} for addr in merged_attendees],
                "extendedProperties": {
                    "private": {
                        "alfred_hold": "false",
                        "alfred_thread_id": eff_thread,
                    }
                },
            }
        )
        loc = slot.location or (matched_hold.location if matched_hold is not None else "")
        if loc:
            body["location"] = loc
        send_updates = "all" if (merged_attendees and slot.send_invites) else "none"
        confirmed_id = self._upsert_confirmed_event(
            calendar_id, body, send_updates, matched_hold.event_id if matched_hold else ""
        )
        released: list[str] = []
        if release_thread_holds and eff_thread:
            for ev in window_events:
                if ev.is_hold and ev.thread_id == eff_thread and ev.event_id != confirmed_id:
                    self.delete_event(
                        calendar_id, ev.event_id, send_updates="all" if ev.attendees else "none"
                    )
                    released.append(ev.event_id)
        return (confirmed_id, released)

    def _upsert_confirmed_event(
        self,
        calendar_id: str,
        body: dict[str, Any],
        send_updates: str,
        existing_hold_id: str,
    ) -> str:
        """Update an existing [HOLD] in place or insert a new confirmed Google Calendar event."""
        client = self._require_client()
        if existing_hold_id:
            saved = (
                client.events()
                .update(
                    calendarId=calendar_id,
                    eventId=existing_hold_id,
                    body=body,
                    sendUpdates=send_updates,
                )
                .execute()
            )
            return str(saved.get("id") or existing_hold_id)
        saved = (
            client.events()
            .insert(calendarId=calendar_id, body=body, sendUpdates=send_updates)
            .execute()
        )
        return str(saved["id"])

    def delete_event(self, calendar_id: str, event_id: str, send_updates: str = "") -> None:
        """Delete an event from a Google Calendar post-Gate validation.

        Args:
            calendar_id: Target Google Calendar identifier.
            event_id: Identifier of the event to remove.
            send_updates: Optional Google Calendar sendUpdates mode ('all' or 'none').
        """
        client = self._require_client()
        kwargs: dict[str, Any] = {"calendarId": calendar_id, "eventId": event_id}
        if send_updates:
            kwargs["sendUpdates"] = send_updates
        client.events().delete(**kwargs).execute()

    def _build_time_bounds(self, start_iso: str, end_iso: str) -> dict[str, Any]:
        """Construct start/end Google Calendar v3 payload dicts for timed or all-day bounds."""
        is_all_day = "T" not in start_iso and "T" not in end_iso
        start_spec = (
            {"date": start_iso}
            if is_all_day
            else {"dateTime": start_iso, "timeZone": self._settings.timezone}
        )
        end_spec = (
            {"date": end_iso}
            if is_all_day
            else {"dateTime": end_iso, "timeZone": self._settings.timezone}
        )
        return {"start": start_spec, "end": end_spec}

    def _parse_event_item(self, raw: dict[str, Any]) -> CalendarEventItem:
        """Normalize a raw Google Calendar v3 event resource into a CalendarEventItem."""
        start_obj = raw.get("start", {})
        end_obj = raw.get("end", {})
        start_iso = str(start_obj.get("dateTime") or start_obj.get("date") or "")
        end_iso = str(end_obj.get("dateTime") or end_obj.get("date") or "")
        summary = str(raw.get("summary", ""))
        priv = raw.get("extendedProperties", {}).get("private", {})
        if not isinstance(priv, dict):
            priv = {}
        is_hold = str(priv.get("alfred_hold", "")).lower() == "true" or bool(
            HOLD_PREFIX_RE.match(summary)
        )
        cand_raw = str(priv.get("alfred_candidate_attendees", ""))
        candidates = _normalize_emails(cand_raw.split(",")) if cand_raw else []
        att_list = _normalize_emails(
            [str(a.get("email", "")) for a in raw.get("attendees", []) if isinstance(a, dict)]
        )
        return CalendarEventItem(
            event_id=str(raw.get("id", "")),
            summary=summary,
            start_iso=start_iso,
            end_iso=end_iso,
            description=str(raw.get("description", "")),
            location=str(raw.get("location", "")),
            attendees=att_list,
            is_hold=is_hold,
            thread_id=str(priv.get("alfred_thread_id", "")),
            candidate_attendees=candidates,
        )

    def _list_confirmation_window(
        self,
        calendar_id: str,
        start_iso: str,
        end_iso: str,
    ) -> list[CalendarEventItem]:
        """List events in a +/- 21-day window around start_iso to locate thread [HOLD]s."""
        anchor = _parse_iso_dt(start_iso)
        if anchor is None:
            return self.list_events(calendar_id, start_iso, end_iso)
        t_min = (anchor - timedelta(days=21)).isoformat()
        t_max = (anchor + timedelta(days=21)).isoformat()
        return self.list_events(calendar_id, t_min, t_max)

    def _match_existing_hold(
        self,
        events: list[CalendarEventItem],
        slot: CalendarSlotProposal,
        thread_id: str,
        hold_event_id: str,
    ) -> CalendarEventItem | None:
        """Find the best matching [HOLD] event by explicit ID, thread+slot, or slot time."""
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

    def _require_client(self) -> Any:
        """Return injected Calendar client or build lazily via GoogleOAuthService."""
        if self._calendar_client is not None:
            return self._calendar_client
        self._calendar_client = self._oauth.build_api_client("calendar", "v3")
        return self._calendar_client
