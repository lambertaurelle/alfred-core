"""WP9 TDD suite: default Gmail archiving after interactive email processing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from alfred.domain.calendar_models import (
    CalendarActionIntent,
    CalendarSlotProposal,
    CalendarTriageAction,
)
from alfred.domain.models import AlfredPaths, AlfredSettings, EmailMessage
from alfred.main import build_controller
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_calendar_bridge import (
    render_action_required_plan,
    render_calendar_confirm_gate_plan,
    render_draft_reply_plan,
)


class _DummyCalClient:
    """Minimal Google Calendar stub for confirm-event CLI testing."""

    def events(self) -> _DummyCalClient:
        """Return self as events resource."""
        return self

    def list(self, **_kwargs: Any) -> _DummyCalClient:
        """Simulate empty event list."""
        self._resp: dict[str, Any] = {"items": []}
        return self

    def insert(self, **kwargs: Any) -> _DummyCalClient:
        """Simulate inserted event."""
        self._resp = {"id": "ev_conf_1", **kwargs.get("body", {})}
        return self

    def execute(self) -> dict[str, Any]:
        """Return prepared response payload."""
        return self._resp


def test_gmail_archive_thread_and_message_labels(tmp_path: Path) -> None:
    """archive_thread and archive_message remove INBOX (+ UNREAD/STARRED as requested)."""
    paths = AlfredPaths.from_env(tmp_path)
    client = MagicMock()
    users = client.users.return_value
    svc = GmailService(paths=paths, settings=AlfredSettings(), gmail_client=client)

    svc.archive_thread("th_1")
    users.threads.return_value.modify.assert_called_with(
        userId="me",
        id="th_1",
        body={"removeLabelIds": ["INBOX", "UNREAD"]},
    )

    svc.archive_thread("th_2", mark_read=False, unstar=True)
    users.threads.return_value.modify.assert_called_with(
        userId="me",
        id="th_2",
        body={"removeLabelIds": ["INBOX", "STARRED"]},
    )

    svc.archive_message("m_1", mark_read=True, unstar=True)
    users.messages.return_value.modify.assert_called_with(
        userId="me",
        id="m_1",
        body={"removeLabelIds": ["INBOX", "UNREAD", "STARRED"]},
    )


def test_send_draft_archives_thread_by_default_and_respects_keep_in_inbox(
    tmp_path: Path, capsys: Any
) -> None:
    """send-draft archives the Gmail thread by default unless --keep-in-inbox or disabled."""
    cfg_dir = tmp_path / "instance" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "settings.yaml").write_text(
        "inbox:\n  archive_processed_emails_by_default: true\n",
        encoding="utf-8",
    )
    ctrl = build_controller(str(tmp_path))
    assert ctrl._settings.archive_processed_emails_by_default is True

    client = MagicMock()
    users = client.users.return_value
    users.drafts.return_value.send.return_value.execute.return_value = {
        "id": "sent_99",
        "threadId": "th_from_api",
    }
    ctrl._gmail = GmailService(AlfredPaths.from_env(tmp_path), ctrl._settings, gmail_client=client)

    assert ctrl.run(["send-draft", "--draft-id", "dr_99", "--unstar"]) == 0
    out_arch = json.loads(capsys.readouterr().out)
    assert out_arch["sent_message_id"] == "sent_99"
    assert out_arch["thread_archived"] is True
    users.threads.return_value.modify.assert_called_once_with(
        userId="me",
        id="th_from_api",
        body={"removeLabelIds": ["INBOX", "UNREAD", "STARRED"]},
    )

    users.threads.return_value.modify.reset_mock()
    assert (
        ctrl.run(
            ["send-draft", "--draft-id", "dr_100", "--thread-id", "th_keep", "--keep-in-inbox"]
        )
        == 0
    )
    out_keep = json.loads(capsys.readouterr().out)
    assert out_keep["thread_archived"] is False
    users.threads.return_value.modify.assert_not_called()


def test_archive_email_cli_batch_and_flags(tmp_path: Path, capsys: Any) -> None:
    """archive-email archives multiple threads/messages and honors --unstar / --keep-unread."""
    ctrl = build_controller(str(tmp_path))
    client = MagicMock()
    users = client.users.return_value
    ctrl._gmail = GmailService(AlfredPaths.from_env(tmp_path), ctrl._settings, gmail_client=client)

    rc = ctrl.run(
        [
            "archive-email",
            "--thread-id",
            "th_a",
            "--thread-id",
            "th_b",
            "--message-id",
            "msg_c",
            "--unstar",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["archived_threads"] == ["th_a", "th_b"]
    assert payload["archived_messages"] == ["msg_c"]
    assert payload["unstarred"] is True
    assert users.threads.return_value.modify.call_count == 2
    users.messages.return_value.modify.assert_called_once_with(
        userId="me",
        id="msg_c",
        body={"removeLabelIds": ["INBOX", "UNREAD", "STARRED"]},
    )

    users.messages.return_value.modify.reset_mock()
    assert ctrl.run(["archive-email", "--message-id", "msg_d", "--keep-unread"]) == 0
    capsys.readouterr()
    users.messages.return_value.modify.assert_called_once_with(
        userId="me",
        id="msg_d",
        body={"removeLabelIds": ["INBOX"]},
    )


def test_confirm_event_archives_source_thread_unless_keep_in_inbox(
    tmp_path: Path, capsys: Any
) -> None:
    """confirm-event --thread-id archives the Gmail thread by default unless --keep-in-inbox."""
    ctrl = build_controller(str(tmp_path))
    ctrl._calendar._calendar_client = _DummyCalClient()
    client = MagicMock()
    users = client.users.return_value
    ctrl._gmail = GmailService(AlfredPaths.from_env(tmp_path), ctrl._settings, gmail_client=client)

    base_cmd = [
        "confirm-event",
        "--summary",
        "Point",
        "--from",
        "2026-11-26T10:00:00+01:00",
        "--to",
        "2026-11-26T10:30:00+01:00",
        "--no-send-invites",
    ]
    assert ctrl.run([*base_cmd, "--thread-id", "th_cal_1"]) == 0
    out_1 = json.loads(capsys.readouterr().out)
    assert out_1["email_thread_archived"] is True
    users.threads.return_value.modify.assert_called_once_with(
        userId="me",
        id="th_cal_1",
        body={"removeLabelIds": ["INBOX", "UNREAD"]},
    )

    users.threads.return_value.modify.reset_mock()
    assert ctrl.run([*base_cmd, "--thread-id", "th_cal_2", "--keep-in-inbox"]) == 0
    out_2 = json.loads(capsys.readouterr().out)
    assert out_2["email_thread_archived"] is False
    users.threads.return_value.modify.assert_not_called()


def test_gate_plans_and_manifests_document_default_email_archiving() -> None:
    """Gate plans and agent manifests document default Gmail archiving and --keep-in-inbox."""
    msg = EmailMessage(
        message_id="m_gate_1",
        thread_id="th_gate_1",
        sender="contact@example.test",
        recipient="owner@example.test",
        subject="Demande de devis",
    )
    draft_plan = render_draft_reply_plan(
        "contact@example.test",
        "Re: Demande de devis",
        "dr_gate_1",
        "Bonjour,",
        "",
        thread_id="th_gate_1",
    )
    assert "--thread-id th_gate_1" in draft_plan
    assert "--keep-in-inbox" in draft_plan

    action_plan = render_action_required_plan(msg, "Action requise", ["Contexte"], "doc_1")
    assert "archive-email --thread-id th_gate_1 --unstar" in action_plan

    cal_action = CalendarTriageAction(
        intent=CalendarActionIntent.CONFIRM_EVENT,
        slots=[
            CalendarSlotProposal(
                summary="RDV",
                start_iso="2026-11-27T14:00:00+01:00",
                end_iso="2026-11-27T15:00:00+01:00",
                attendees=["contact@example.test"],
            )
        ],
    )
    cal_plan = render_calendar_confirm_gate_plan(msg, "Créneau validé", cal_action, [])
    assert "--keep-in-inbox" in cal_plan

    repo_root = Path(__file__).resolve().parents[2]
    exec_md = (repo_root / ".agents/agents/plan-executor.md").read_text(encoding="utf-8")
    synth_md = (repo_root / ".agents/agents/plan-synthesizer.md").read_text(encoding="utf-8")
    assert "archive-email" in exec_md and "--keep-in-inbox" in exec_md
    assert "--keep-in-inbox" in synth_md
