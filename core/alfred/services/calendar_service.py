"""Google Calendar API v3 generic multi-calendar service, [HOLD] lifecycle, and FreeBusy."""

from __future__ import annotations

from typing import Any

from alfred.domain.calendar_models import (
    CalendarListEntry,
    CalendarSlotProposal,
    FreeBusyInterval,
)
from alfred.domain.models import AlfredPaths, AlfredSettings, CalendarEventItem, fold_accents
from alfred.repositories.state_store import StateStore
from alfred.services.calendar_helpers import (
    build_confirmed_payload,
    build_hold_payload,
    build_time_bounds,
    compute_confirmation_window,
    fetch_paginated_events,
    match_existing_hold,
    normalize_emails,
    sort_events_chronologically,
    strip_hold_prefix,
)
from alfred.services.google_auth import GoogleOAuthService


class CalendarService:
    """Generic multi-calendar Google Calendar v3 service with self-alias attendee exclusion."""

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
        self._cached_calendars: list[CalendarListEntry] | None = None

    def sanitize_guest_attendees(
        self,
        candidates: list[str] | set[str] | frozenset[str],
        owner_addresses: frozenset[str] = frozenset(),
    ) -> list[str]:
        """Strip never_invite_emails, sendAs aliases, and owner addresses from event attendees.

        Args:
            candidates: Raw candidate attendee email addresses.
            owner_addresses: Authenticated mailbox & sendAs addresses owned by user.

        Returns:
            Ordered list of external guest emails safe to invite on Google Calendar.
        """
        blocked = self._settings.identity.blocked_calendar_attendees(owner_addresses)
        return [addr for addr in normalize_emails(candidates) if addr not in blocked]

    def get_effective_hold_allowlist(
        self,
        owner_addresses: frozenset[str] = frozenset(),
    ) -> frozenset[str]:
        """Compute lowercase emails allowed to receive immediate [HOLD] calendar invites.

        Args:
            owner_addresses: Authenticated user email addresses resolved via Gmail API.

        Returns:
            Frozenset of allowlisted lowercase email addresses minus never_invite_emails.
        """
        persisted = (
            self._state_store.hold_allowlist.load().emails
            if self._state_store is not None
            else []
        )
        allowed = set(normalize_emails(persisted))
        if self._settings.calendar.include_authenticated_owners_in_hold_allowlist:
            allowed.update(normalize_emails(owner_addresses))
        hard_blocked = set(normalize_emails(self._settings.identity.never_invite_emails))
        if "@" in self._settings.identity.default_send_as_email:
            hard_blocked.add(self._settings.identity.default_send_as_email.strip().lower())
        return frozenset(allowed - hard_blocked)

    def set_hold_allowlist_entry(self, email: str, remove: bool = False) -> list[str]:
        """Add or remove an email address in the persistent [HOLD] allowlist state.

        Args:
            email: Target email address to allowlist or remove.
            remove: When True, remove email instead of adding it.

        Returns:
            Updated sorted list of persisted allowlist emails.
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
                        set(normalize_emails(cur.emails)) - {norm}
                        if remove
                        else set(normalize_emails(cur.emails)) | {norm}
                    )
                }
            )
        )
        return list(updated.emails)

    def list_calendars(self, refresh: bool = False) -> list[CalendarListEntry]:
        """List all Google Calendars accessible by the authenticated user across pages.

        Args:
            refresh: When True, bypass in-memory calendar list cache.

        Returns:
            Ordered list of CalendarListEntry objects (primary calendar first).
        """
        if self._cached_calendars is not None and not refresh:
            return list(self._cached_calendars)
        client = self._require_client()
        entries: list[CalendarListEntry] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"pageToken": page_token} if page_token else {}
            listing = client.calendarList().list(**kwargs).execute()
            for raw in listing.get("items", []):
                entries.append(
                    CalendarListEntry(
                        calendar_id=str(raw.get("id", "")),
                        summary=str(raw.get("summary", "")),
                        primary=bool(raw.get("primary", False)),
                        access_role=str(raw.get("accessRole", "")),
                        time_zone=str(raw.get("timeZone", "")),
                    )
                )
            page_token = listing.get("nextPageToken")
            if not page_token:
                break
        entries.sort(key=lambda item: (not item.primary, fold_accents(item.summary)))
        self._cached_calendars = entries
        return list(entries)

    def resolve_calendar_selector(self, selector: str) -> str:
        """Resolve 'primary', a calendar ID, or a display name to its Google Calendar ID.

        Args:
            selector: User-provided calendar identifier or summary name.

        Returns:
            Resolved Google Calendar ID string.
        """
        cleaned = selector.strip()
        if not cleaned or cleaned.lower() == "primary":
            return self._settings.primary_calendar_id
        if "@" in cleaned:
            return cleaned
        try:
            calendars = self.list_calendars()
        except Exception:
            return cleaned
        for cal in calendars:
            if cal.calendar_id == cleaned:
                return cal.calendar_id
        folded = fold_accents(cleaned)
        for cal in calendars:
            if fold_accents(cal.summary) == folded:
                return cal.calendar_id
        return cleaned

    def ensure_calendar(self, calendar_name: str) -> str:
        """Find an existing Google Calendar by summary across pages or create it idempotently.

        Args:
            calendar_name: Display name of the secondary calendar (e.g. 'Famille').

        Returns:
            Google Calendar identifier.
        """
        for entry in self.list_calendars(refresh=True):
            if entry.summary == calendar_name:
                return entry.calendar_id
        created = (
            self._require_client()
            .calendars()
            .insert(body={"summary": calendar_name, "timeZone": self._settings.timezone})
            .execute()
        )
        self._cached_calendars = None
        return str(created["id"])

    def query_freebusy(
        self, time_min_iso: str, time_max_iso: str, calendar_ids: list[str] | None = None
    ) -> list[FreeBusyInterval]:
        """Query Google Calendar FreeBusy busy windows across one or more calendars.

        Args:
            time_min_iso: Inclusive lower bound ISO-8601 timestamp.
            time_max_iso: Inclusive upper bound ISO-8601 timestamp.
            calendar_ids: Optional list of calendar IDs/names (defaults to primary).
        """
        targets = [
            self.resolve_calendar_selector(c)
            for c in (calendar_ids or [self._settings.primary_calendar_id])
        ]
        items_spec = [{"id": c} for c in targets]
        body = {"timeMin": time_min_iso, "timeMax": time_max_iso, "items": items_spec}
        resp = self._require_client().freebusy().query(body=body).execute()
        cal_map = resp.get("calendars", {}) if isinstance(resp, dict) else {}
        intervals = [
            FreeBusyInterval(calendar_id=cid, start_iso=str(it["start"]), end_iso=str(it["end"]))
            for cid in targets
            for it in (cal_map.get(cid, {}).get("busy", []) if isinstance(cal_map, dict) else [])
            if isinstance(it, dict) and it.get("start") and it.get("end")
        ]
        return sorted(intervals, key=lambda iv: (iv.start_iso, iv.end_iso))

    def list_events(
        self, calendar_id: str, time_min_iso: str, time_max_iso: str
    ) -> list[CalendarEventItem]:
        """List expanded single events across all pages within [time_min_iso, time_max_iso].

        Args:
            calendar_id: Target Google Calendar identifier or summary.
            time_min_iso: Inclusive lower bound ISO-8601 timestamp.
            time_max_iso: Inclusive upper bound ISO-8601 timestamp.
        """
        resolved_cal = self.resolve_calendar_selector(calendar_id)
        return fetch_paginated_events(
            self._require_client(), resolved_cal, time_min_iso,
            single_events=True, time_max_iso=time_max_iso,
        )

    def list_future_events(self, calendar_id: str, time_min_iso: str) -> list[CalendarEventItem]:
        """List all future non-cancelled events and master recurrences without timeMax horizon.

        Args:
            calendar_id: Target Google Calendar identifier or summary.
            time_min_iso: Inclusive lower bound ISO-8601 timestamp.
        """
        resolved_cal = self.resolve_calendar_selector(calendar_id)
        events = fetch_paginated_events(
            self._require_client(), resolved_cal, time_min_iso,
            single_events=False, time_max_iso=None,
        )
        return sort_events_chronologically(events)

    def insert_event(
        self,
        calendar_id: str,
        event: CalendarEventItem,
        send_invites: bool = False,
        owner_addresses: frozenset[str] = frozenset(),
    ) -> str:
        """Insert a timed, all-day, or recurring event while stripping blocked self-aliases.

        Args:
            calendar_id: Target Google Calendar identifier or summary.
            event: CalendarEventItem specification to create.
            send_invites: Whether Google Calendar should email external attendees.
            owner_addresses: Authenticated mailbox & alias addresses to exclude from guests.
        """
        resolved_cal = self.resolve_calendar_selector(calendar_id)
        body = build_time_bounds(event.start_iso, event.end_iso, self._settings.timezone)
        body["summary"] = event.summary
        body["description"] = event.description
        if event.location:
            body["location"] = event.location
        if event.recurrence:
            body["recurrence"] = list(event.recurrence)
        guests = self.sanitize_guest_attendees(event.attendees, owner_addresses)
        events_api = self._require_client().events()
        if guests:
            body["attendees"] = [{"email": addr} for addr in guests]
            mode = "all" if send_invites else "none"
            created = events_api.insert(calendarId=resolved_cal, body=body, sendUpdates=mode)
            return str(created.execute()["id"])
        return str(events_api.insert(calendarId=resolved_cal, body=body).execute()["id"])

    def update_event(
        self,
        calendar_id: str,
        event_id: str,
        summary: str | None = None,
        start_iso: str | None = None,
        end_iso: str | None = None,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
        recurrence: list[str] | None = None,
        send_invites: bool = True,
        owner_addresses: frozenset[str] = frozenset(),
    ) -> str:
        """Patch an existing Google Calendar event while stripping blocked self-aliases.

        Args:
            calendar_id: Target Google Calendar identifier or summary.
            event_id: Identifier of the event to update.
            summary: Optional updated title.
            start_iso: Optional updated start ISO timestamp/date (requires end_iso).
            end_iso: Optional updated end ISO timestamp/date (requires start_iso).
            description: Optional updated description.
            location: Optional updated location.
            attendees: Optional replacement attendee list (filtered against blocked aliases).
            recurrence: Optional replacement RRULE list.
            send_invites: Whether Google Calendar should notify external guests.
            owner_addresses: Authenticated mailbox & alias addresses to exclude from guests.
        """
        resolved_cal = self.resolve_calendar_selector(calendar_id)
        patch_body: dict[str, Any] = {}
        if start_iso is not None and end_iso is not None:
            patch_body.update(build_time_bounds(start_iso, end_iso, self._settings.timezone))
        for field_name, val in (
            ("summary", summary), ("description", description), ("location", location)
        ):
            if val is not None:
                patch_body[field_name] = val
        if recurrence is not None:
            patch_body["recurrence"] = list(recurrence)
        send_mode = "none"
        if attendees is not None:
            guests = self.sanitize_guest_attendees(attendees, owner_addresses)
            patch_body["attendees"] = [{"email": addr} for addr in guests]
            if guests and send_invites:
                send_mode = "all"
        updated = self._require_client().events().patch(
            calendarId=resolved_cal, eventId=event_id, body=patch_body, sendUpdates=send_mode,
        ).execute()
        return str(updated.get("id") or event_id)

    def create_hold_event(
        self,
        calendar_id: str,
        slot: CalendarSlotProposal,
        thread_id: str = "",
        message_id: str = "",
        owner_addresses: frozenset[str] = frozenset(),
    ) -> str:
        """Create an opaque [HOLD] block sending invites strictly to allowlisted non-blocked guests.

        Args:
            calendar_id: Target Google Calendar identifier (typically 'primary').
            slot: Proposed CalendarSlotProposal to block tentatively.
            thread_id: Optional Gmail threadId linked to this [HOLD].
            message_id: Optional Gmail messageId linked to this [HOLD].
            owner_addresses: Authenticated owner email addresses.
        """
        resolved_cal = self.resolve_calendar_selector(calendar_id)
        hold_summary = f"[HOLD] {strip_hold_prefix(slot.summary)}"
        for ev in self.list_events(resolved_cal, slot.start_iso, slot.end_iso):
            if (ev.summary, ev.start_iso, ev.end_iso) == (
                hold_summary, slot.start_iso, slot.end_iso
            ):
                return ev.event_id
        allowlist = self.get_effective_hold_allowlist(owner_addresses)
        all_candidates = self.sanitize_guest_attendees(slot.attendees, frozenset())
        allowed_guests = [addr for addr in normalize_emails(slot.attendees) if addr in allowlist]
        body = build_hold_payload(
            slot, self._settings.timezone, allowed_guests, all_candidates, thread_id, message_id
        )
        send_updates = "all" if (allowed_guests and slot.send_invites) else "none"
        res = self._require_client().events().insert(
            calendarId=resolved_cal, body=body, sendUpdates=send_updates
        )
        return str(res.execute()["id"])

    def confirm_event(
        self,
        calendar_id: str,
        slot: CalendarSlotProposal,
        thread_id: str = "",
        hold_event_id: str = "",
        release_thread_holds: bool = True,
        owner_addresses: frozenset[str] = frozenset(),
    ) -> tuple[str, list[str]]:
        """Convert a [HOLD] (or create a firm event), invite external guests, and free [HOLD]s.

        Args:
            calendar_id: Target Google Calendar identifier.
            slot: Validated CalendarSlotProposal approved via Blocking Gate.
            thread_id: Optional Gmail threadId used to match and release alternative [HOLD]s.
            hold_event_id: Optional explicit [HOLD] event_id to convert.
            release_thread_holds: When True, remove unchosen [HOLD] blocks on thread_id.
            owner_addresses: Authenticated owner & sendAs addresses excluded from guests.
        """
        resolved_cal = self.resolve_calendar_selector(calendar_id)
        w_min, w_max = compute_confirmation_window(slot.start_iso, slot.end_iso)
        window_events = self.list_events(resolved_cal, w_min, w_max)
        matched_hold = match_existing_hold(window_events, slot, thread_id, hold_event_id)
        raw_guests = (
            slot.attendees
            if slot.attendees
            else (matched_hold.candidate_attendees if matched_hold is not None else [])
        )
        merged_attendees = self.sanitize_guest_attendees(raw_guests, owner_addresses)
        eff_thread = thread_id or (matched_hold.thread_id if matched_hold is not None else "")
        body = build_confirmed_payload(
            slot, self._settings.timezone, merged_attendees, eff_thread, matched_hold
        )
        send_updates = "all" if (merged_attendees and slot.send_invites) else "none"
        confirmed_id = self._upsert_confirmed_event(
            resolved_cal, body, send_updates, matched_hold.event_id if matched_hold else ""
        )
        released = self._release_other_holds(
            resolved_cal, window_events, eff_thread, confirmed_id, release_thread_holds
        )
        return (confirmed_id, released)

    def _release_other_holds(
        self,
        calendar_id: str,
        events: list[CalendarEventItem],
        thread_id: str,
        confirmed_id: str,
        enabled: bool,
    ) -> list[str]:
        """Delete unused [HOLD] events belonging to thread_id after confirmation."""
        if not (enabled and thread_id):
            return []
        released: list[str] = []
        for ev in events:
            if ev.is_hold and ev.thread_id == thread_id and ev.event_id != confirmed_id:
                self.delete_event(
                    calendar_id, ev.event_id, send_updates="all" if ev.attendees else "none"
                )
                released.append(ev.event_id)
        return released

    def _upsert_confirmed_event(
        self, calendar_id: str, body: dict[str, Any], send_updates: str, existing_hold_id: str
    ) -> str:
        """Update an existing [HOLD] in place or insert a new confirmed Google Calendar event."""
        events_api = self._require_client().events()
        if existing_hold_id:
            saved = events_api.update(
                calendarId=calendar_id, eventId=existing_hold_id,
                body=body, sendUpdates=send_updates,
            ).execute()
            return str(saved.get("id") or existing_hold_id)
        inserted = events_api.insert(
            calendarId=calendar_id, body=body, sendUpdates=send_updates
        ).execute()
        return str(inserted["id"])

    def delete_event(self, calendar_id: str, event_id: str, send_updates: str = "") -> None:
        """Delete an event from a Google Calendar post-Gate validation.

        Args:
            calendar_id: Target Google Calendar identifier or summary.
            event_id: Identifier of the event to remove.
            send_updates: Optional Google Calendar sendUpdates mode ('all' or 'none').
        """
        resolved_cal = self.resolve_calendar_selector(calendar_id)
        kwargs: dict[str, Any] = {"calendarId": resolved_cal, "eventId": event_id}
        if send_updates:
            kwargs["sendUpdates"] = send_updates
        self._require_client().events().delete(**kwargs).execute()

    def _require_client(self) -> Any:
        """Return injected Calendar client or build lazily via GoogleOAuthService."""
        if self._calendar_client is not None:
            return self._calendar_client
        self._calendar_client = self._oauth.build_api_client("calendar", "v3")
        return self._calendar_client
