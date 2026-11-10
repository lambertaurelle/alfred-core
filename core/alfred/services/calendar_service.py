"""Google Calendar API v3 connector for primary agenda and dedicated Garde Nounou calendar."""

from __future__ import annotations

from typing import Any

from alfred.domain.models import AlfredPaths, AlfredSettings, CalendarEventItem
from alfred.services.google_auth import GoogleOAuthService


class CalendarService:
    """Google Calendar v3 service managing calendar provisioning and event CRUD."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        calendar_client: Any | None = None,
        oauth_service: GoogleOAuthService | None = None,
    ) -> None:
        """Initialize the Google Calendar service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings.
            calendar_client: Optional injected Google Calendar v3 client.
            oauth_service: Optional shared GoogleOAuthService instance.
        """
        self._paths = paths
        self._settings = settings
        self._calendar_client = calendar_client
        self._oauth = oauth_service or GoogleOAuthService(paths, settings)

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
            Ordered list of CalendarEventItem objects.
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
                start_obj = raw.get("start", {})
                end_obj = raw.get("end", {})
                start_iso = str(start_obj.get("dateTime") or start_obj.get("date") or "")
                end_iso = str(end_obj.get("dateTime") or end_obj.get("date") or "")
                events.append(
                    CalendarEventItem(
                        event_id=str(raw.get("id", "")),
                        summary=str(raw.get("summary", "")),
                        start_iso=start_iso,
                        end_iso=end_iso,
                        description=str(raw.get("description", "")),
                    )
                )
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
        is_all_day = "T" not in event.start_iso and "T" not in event.end_iso
        start_spec = (
            {"date": event.start_iso}
            if is_all_day
            else {"dateTime": event.start_iso, "timeZone": self._settings.timezone}
        )
        end_spec = (
            {"date": event.end_iso}
            if is_all_day
            else {"dateTime": event.end_iso, "timeZone": self._settings.timezone}
        )
        body = {
            "summary": event.summary,
            "description": event.description,
            "start": start_spec,
            "end": end_spec,
        }
        created = client.events().insert(calendarId=calendar_id, body=body).execute()
        return str(created["id"])

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        """Delete an event from a Google Calendar post-Gate validation.

        Args:
            calendar_id: Target Google Calendar identifier.
            event_id: Identifier of the event to remove.
        """
        client = self._require_client()
        client.events().delete(calendarId=calendar_id, eventId=event_id).execute()

    def _require_client(self) -> Any:
        """Return injected Calendar client or build lazily via GoogleOAuthService."""
        if self._calendar_client is not None:
            return self._calendar_client
        self._calendar_client = self._oauth.build_api_client("calendar", "v3")
        return self._calendar_client
