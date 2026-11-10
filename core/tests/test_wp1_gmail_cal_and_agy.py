"""WP1 tests for GmailService, CalendarService, and AgySessionManager."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock

from alfred.domain.models import AlfredPaths, AlfredSettings, CalendarEventItem
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.gmail_service import GmailHistoryExpiredError, GmailService


def test_gmail_service_history_drafts_and_star(tmp_path: Path) -> None:
    """Verify Gmail historyId polling, multipart body decoding, drafts CRUD, and STARRED."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    client = MagicMock()
    users = client.users.return_value

    users.getProfile.return_value.execute.return_value = {"historyId": "5000"}
    users.history.return_value.list.return_value.execute.return_value = {
        "history": [{"messagesAdded": [{"message": {"id": "m_1", "labelIds": ["INBOX"]}}]}],
        "historyId": "5005",
    }
    encoded = base64.urlsafe_b64encode(b"Bonjour, disponible vendredi ?").decode("ascii")
    users.messages.return_value.get.return_value.execute.return_value = {
        "id": "m_1",
        "threadId": "th_1",
        "snippet": "Bonjour",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "contact@example.com"},
                {"name": "Subject", "value": "Confirmation RDV"},
            ],
            "mimeType": "multipart/alternative",
            "parts": [{"mimeType": "text/plain", "body": {"data": encoded}}],
        },
    }
    users.drafts.return_value.create.return_value.execute.return_value = {"id": "dr_1"}
    users.drafts.return_value.update.return_value.execute.return_value = {"id": "dr_1"}
    users.drafts.return_value.send.return_value.execute.return_value = {"id": "sent_1"}

    svc = GmailService(paths=paths, settings=settings, gmail_client=client)
    assert svc.get_current_history_id() == "5000"
    msgs, latest = svc.list_new_messages_since("5000")
    assert latest == "5005"
    assert len(msgs) == 1
    assert "vendredi" in msgs[0].body_text
    assert svc.create_draft("th_1", "contact@example.com", "Re: RDV", "Oui") == "dr_1"
    assert svc.update_draft("dr_1", "th_1", "contact@example.com", "Re: RDV", "Parfait") == "dr_1"
    assert svc.send_draft("dr_1") == "sent_1"
    svc.star_message("m_1")
    users.messages.return_value.modify.assert_called_once()


class _Fake404Error(Exception):
    """Simulated Google API HTTP 404 exception."""

    status_code = 404


def test_gmail_pagination_spam_filter_and_404_skip(tmp_path: Path) -> None:
    """Verify history pagination, SPAM exclusion, 404 message skip, and HTML body decoding."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    client = MagicMock()
    users = client.users.return_value
    users.history.return_value.list.side_effect = [
        MagicMock(
            execute=lambda: {
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "m_spam", "labelIds": ["SPAM"]}},
                            {"message": {"id": "m_gone", "labelIds": ["INBOX"]}},
                        ]
                    }
                ],
                "nextPageToken": "p2",
                "historyId": "6001",
            }
        ),
        MagicMock(
            execute=lambda: {
                "history": [{"messagesAdded": [{"message": {"id": "m_html", "labelIds": []}}]}],
                "historyId": "6002",
            }
        ),
    ]
    html_raw = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><p>Action requise&nbsp;: rejet de pr&eacute;l&egrave;vement.</p></body></html>"
    )
    html_b64 = base64.urlsafe_b64encode(html_raw.encode("utf-8")).decode("ascii").rstrip("=")

    def _get_msg(**kwargs: object) -> MagicMock:
        """Return 404 for deleted message and HTML payload for active message."""
        if kwargs["id"] == "m_gone":
            return MagicMock(side_effect=_Fake404Error)
        return MagicMock(
            return_value={
                "id": "m_html",
                "threadId": "th_h",
                "snippet": "Info",
                "labelIds": ["INBOX"],
                "payload": {
                    "headers": [{"name": "Subject", "value": "Info"}],
                    "mimeType": "multipart/mixed",
                    "parts": [{"mimeType": "text/html", "body": {"data": html_b64}}],
                },
            }
        )

    users.messages.return_value.get.side_effect = lambda **kw: MagicMock(execute=_get_msg(**kw))
    svc = GmailService(paths=paths, settings=settings, gmail_client=client)
    msgs, latest = svc.list_new_messages_since("6000")
    assert latest == "6002"
    assert [m.message_id for m in msgs] == ["m_html"]
    assert "Action requise" in msgs[0].body_text and "prélèvement" in msgs[0].body_text
    assert ".x{color:red}" not in msgs[0].body_text


def test_gmail_reply_headers_and_expired_history_404(tmp_path: Path) -> None:
    """Verify RFC 2822 reply headers and GmailHistoryExpiredError on 404 history.list."""
    import pytest

    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    client = MagicMock()
    users = client.users.return_value
    users.drafts.return_value.create.return_value.execute.return_value = {"id": "dr_ref"}
    svc = GmailService(paths=paths, settings=settings, gmail_client=client)
    assert (
        svc.create_draft(
            "th_h",
            "reply@example.com",
            "Re: Info",
            "Noted",
            in_reply_to="<orig@example.com>",
            references="<root@example.com> <orig@example.com>",
        )
        == "dr_ref"
    )
    users.history.return_value.list.side_effect = [
        MagicMock(execute=MagicMock(side_effect=_Fake404Error))
    ]
    with pytest.raises(GmailHistoryExpiredError):
        svc.list_new_messages_since("1")


def test_calendar_service_ensure_list_insert_delete(tmp_path: Path) -> None:
    """Verify Garde Nounou calendar creation, event insertion, listing, and deletion."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    client = MagicMock()
    client.calendarList.return_value.list.side_effect = [
        MagicMock(execute=lambda: {"items": []}),
        MagicMock(execute=lambda: {"items": [{"id": "cal_nounou", "summary": "Garde Nounou"}]}),
    ]
    client.calendars.return_value.insert.return_value.execute.return_value = {"id": "cal_nounou"}
    client.events.return_value.insert.return_value.execute.return_value = {"id": "ev_1"}
    client.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": "ev_1",
                "summary": "Garde Enfant",
                "description": "Contrat",
                "start": {"dateTime": "2026-05-04T08:30:00+02:00"},
                "end": {"dateTime": "2026-05-04T18:30:00+02:00"},
            }
        ]
    }

    svc = CalendarService(paths=paths, settings=settings, calendar_client=client)
    assert svc.ensure_calendar("Garde Nounou") == "cal_nounou"
    assert svc.ensure_calendar("Garde Nounou") == "cal_nounou"
    ev_id = svc.insert_event(
        "cal_nounou",
        CalendarEventItem(
            summary="Garde Enfant",
            start_iso="2026-05-04T08:30:00+02:00",
            end_iso="2026-05-04T18:30:00+02:00",
        ),
    )
    assert ev_id == "ev_1"
    events = svc.list_events("cal_nounou", "2026-05-01T00:00:00Z", "2026-05-31T23:59:59Z")
    assert len(events) == 1
    all_day_id = svc.insert_event(
        "cal_nounou",
        CalendarEventItem(summary="Vacances", start_iso="2026-08-01", end_iso="2026-08-15"),
    )
    assert all_day_id == "ev_1"
    svc.delete_event("cal_nounou", "ev_1")
    client.events.return_value.delete.assert_called_once()


def test_agy_session_manager_active_plan_and_silent_auto_archive(tmp_path: Path) -> None:
    """Verify Antigravity session plan writing and SILENT_NO_ACTION auto-archiving."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    archived_hooks: list[str] = []

    agy = AgySessionManager(
        paths=paths,
        settings=settings,
        state_store=store,
        archive_hook=archived_hooks.append,
    )
    silent = agy.create_session(workspace="alfred-inbox", source_message_id="m_noise")
    done = agy.auto_archive_silent_session(silent.session_id, reason="Passive portal notice")
    assert done.status == "ARCHIVED"
    assert archived_hooks == [silent.session_id]

    active = agy.create_session(workspace="alfred-inbox", source_message_id="m_action")
    plan_file = agy.write_session_plan(active.session_id, "# Gate Plan\n\nAction required.")
    assert plan_file.exists()
    reloaded = store.conversations.load().sessions[active.session_id]
    assert reloaded.status == "ACTIVE"
    assert reloaded.plan_path == str(plan_file)
