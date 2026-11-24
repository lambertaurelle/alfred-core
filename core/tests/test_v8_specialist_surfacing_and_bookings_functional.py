"""Functional inter-service tests for dated booking triage, calendar snapshot, and Gate plans."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from alfred.domain.calendar_models import CalendarActionIntent
from alfred.domain.feature_models import InboxFilterConfig
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    EmailMessage,
    InboxOutputType,
)
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


class _Exec:
    """Minimal Google API execute() wrapper."""

    def __init__(self, payload: dict[str, Any]) -> None:
        """Store static response payload."""
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        """Return stored payload."""
        return self._payload


class _FakeEventsResource:
    """Fake Calendar events() resource returning far-future and recurring events."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        """Store future calendar items and record API calls."""
        self._items = items
        self.list_calls: list[dict[str, Any]] = []
        self.insert_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _Exec:
        """Record call kwargs and return stored items on first page."""
        self.list_calls.append(dict(kwargs))
        return _Exec({"items": self._items if not kwargs.get("pageToken") else []})

    def insert(self, **kwargs: Any) -> _Exec:
        """Record unexpected insert calls."""
        self.insert_calls.append(dict(kwargs))
        return _Exec({"id": "unexpected_insert"})


class _FakeCalendarClient:
    """In-memory Google Calendar v3 client."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        """Initialize fake events resource."""
        self.events_res = _FakeEventsResource(items)

    def events(self) -> _FakeEventsResource:
        """Return fake events resource."""
        return self.events_res


class _FakeGmailStub:
    """Minimal GmailService stub for InboxTriageService functional tests."""

    def __init__(self, owner_addrs: frozenset[str] = frozenset({"owner@example.com"})) -> None:
        """Initialize owner addresses and draft/star logs."""
        self._owners = owner_addrs
        self.drafts: list[tuple[str, str, str, str]] = []
        self.starred: list[str] = []

    def get_authenticated_addresses(self) -> frozenset[str]:
        """Return owner addresses."""
        return self._owners

    def star_message(self, message_id: str) -> None:
        """Record starred message_id."""
        self.starred.append(message_id)

    def create_draft(
        self,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to: str = "",
        references: str = "",
    ) -> str:
        """Record draft creation."""
        self.drafts.append((thread_id, to_addr, subject, body_text))
        return f"draft_{len(self.drafts)}"

    def get_message_attachments(self, _message_id: str) -> list[Any]:
        """Return empty attachment list."""
        return []


def _future_calendar_items() -> list[dict[str, Any]]:
    """Build far-future (+1y, +10y) and recurring events for snapshot verification."""
    return [
        {
            "id": "ev_plus_1y",
            "summary": "Contrôle Annuel +1 An",
            "status": "confirmed",
            "start": {"dateTime": "2027-11-25T10:00:00+01:00"},
            "end": {"dateTime": "2027-11-25T11:00:00+01:00"},
        },
        {
            "id": "ev_plus_10y",
            "summary": "Échéance Patrimoine +10 Ans",
            "status": "confirmed",
            "start": {"dateTime": "2036-06-01T09:00:00+02:00"},
            "end": {"dateTime": "2036-06-01T10:00:00+02:00"},
        },
        {
            "id": "ev_yearly",
            "summary": "Renouvellement Contrat",
            "status": "confirmed",
            "start": {"date": "2027-04-01"},
            "end": {"date": "2027-04-02"},
            "recurrence": ["RRULE:FREQ=YEARLY"],
        },
    ]


def _make_triage_rig(
    tmp_path: Path,
    runner_fn: Callable[[list[str]], subprocess.CompletedProcess[str]],
    settings: AlfredSettings | None = None,
) -> tuple[StateStore, Path, _FakeCalendarClient, InboxTriageService]:
    """Wire InboxTriageService with isolated StateStore, CalendarService, and AgyManager."""
    paths = AlfredPaths.from_env(tmp_path)
    cfg = settings or AlfredSettings()
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
    fake_cal = _FakeCalendarClient(_future_calendar_items())
    cal_svc = CalendarService(paths, cfg, calendar_client=fake_cal, state_store=store)
    agy_mgr = AgySessionManager(
        paths,
        cfg,
        store,
        runner_fn=runner_fn,
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )
    triage = InboxTriageService(
        store,
        _FakeGmailStub(),  # type: ignore[arg-type]
        KnowledgeService(paths, cfg, store),
        DriveService(paths, cfg, store),
        agy_mgr,
        spawn_live_agy=True,
        calendar_service=cal_svc,
        settings=cfg,
    )
    return store, agy_dir, fake_cal, triage


def _make_confirm_runner(
    tmp_path: Path,
    cls_conv: str,
    cal_conv: str,
    slot_payload: dict[str, Any],
    prompts_seen: list[str],
    projects_seen: list[str],
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Build a fake agy runner simulating CONFIRM_EVENT classification and Gate creation."""

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Simulate cognitive CONFIRM_EVENT classification and alfred-calendar Gate creation."""
        conv_dir = tmp_path / "agy_data" / "conversations"
        if "--json-schema" in cmd:
            prompts_seen.append(cmd[cmd.index("--print") + 1])
            (conv_dir / f"{cls_conv}.db").write_bytes(b"sqlite")
            out = {
                "conversation_id": cls_conv,
                "structured_output": {
                    "output_type": "SILENT_NO_ACTION",
                    "reason": "Confirmation de réservation datée à inscrire à l'agenda",
                    "calendar_action": {"intent": "CONFIRM_EVENT", "slots": [slot_payload]},
                },
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")
        projects_seen.append(cmd[cmd.index("--project") + 1])
        (conv_dir / f"{cal_conv}.db").write_bytes(b"sqlite")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"conversation_id": cal_conv}), "")

    return _runner


@pytest.mark.parametrize(
    ("sender", "subject", "body", "slot_payload"),
    [
        (
            "Billetterie <salut@ticketing.example>",
            "Confirmation d'achat : Expérience Spatiale VR - Lyon",
            "Votre billet est confirmé pour lun. 28 déc. - 03:00 PM\nID du billet: 123456",
            {
                "summary": "Expérience Spatiale VR - Lyon",
                "start_iso": "2026-12-28T15:00:00+01:00",
                "end_iso": "2026-12-28T16:30:00+01:00",
                "attendees": [],
                "send_invites": False,
                "location": "12 Rue de la République, 69002 Lyon",
                "description": "ID du billet: 123456",
            },
        ),
        (
            "Grand Hotel <no-reply@ticketing.example>",
            "Booking confirmation: Grand Hotel — Check-in 2027-12-27",
            "Reservation #GH-400 confirmed. Check-in: 2027-12-27, Check-out: 2027-12-29.",
            {
                "summary": "Séjour Grand Hotel",
                "start_iso": "2027-12-27",
                "end_iso": "2027-12-29",
                "attendees": [],
                "send_invites": False,
                "location": "1 Place Centrale, Nice",
                "description": "Reservation #GH-400",
            },
        ),
    ],
)
def test_dated_booking_in_promotions_bulk_wakes_cognitive_and_opens_calendar_gate(
    tmp_path: Path,
    sender: str,
    subject: str,
    body: str,
    slot_payload: dict[str, Any],
) -> None:
    """Dated booking confirmations bypass promo/bulk filters and open a merchant-free Gate."""
    cls_conv = "aaaaaaaa-1111-2222-3333-444444444444"
    cal_conv = "bbbbbbbb-1111-2222-3333-444444444444"
    prompts_seen: list[str] = []
    projects_seen: list[str] = []
    runner = _make_confirm_runner(
        tmp_path, cls_conv, cal_conv, slot_payload, prompts_seen, projects_seen
    )
    store, _, fake_cal, triage = _make_triage_rig(tmp_path, runner)
    msg = EmailMessage(
        message_id="m_booking_1",
        thread_id="th_booking_1",
        sender=sender,
        recipient="owner@example.com",
        subject=subject,
        body_text=body,
        headers={
            "Reply-To": "feedback@ticketing.example",
            "List-Unsubscribe": "<https://ticketing.example/unsub>",
            "Auto-Submitted": "auto-generated",
        },
        label_ids=["INBOX", "CATEGORY_PROMOTIONS"],
    )

    decision = triage.process_message(msg, use_cognitive_agy=True)
    assert decision.skipped_pre_filter is False and len(prompts_seen) == 1
    assert "2027-11-25" in prompts_seen[0] and "2036-06-01" in prompts_seen[0]
    assert fake_cal.events_res.insert_calls == []
    assert decision.calendar_action_intent == CalendarActionIntent.CONFIRM_EVENT.value
    assert decision.calendar_session_id == cal_conv and "alfred-calendar" in projects_seen

    plan_text = Path(store.conversations.load().sessions[cal_conv].plan_path).read_text(
        encoding="utf-8"
    )
    assert "--location" in plan_text and str(slot_payload["location"]) in plan_text
    assert "--description" in plan_text and str(slot_payload["description"]) in plan_text
    assert "--no-send-invites" in plan_text and "--archive-trace" in plan_text
    assert "--attendee" not in plan_text
    for merchant in (
        "salut@ticketing.example",
        "no-reply@ticketing.example",
        "feedback@ticketing.example",
    ):
        assert merchant not in plan_text


def test_user_action_required_with_confirm_event_renders_coordination_agenda_section(
    tmp_path: Path,
) -> None:
    """USER_ACTION_REQUIRED with CONFIRM_EVENT embeds Calendar Coordination in alfred-inbox plan."""
    cls_conv = "cccccccc-1111-2222-3333-444444444444"
    inbox_conv = "dddddddd-1111-2222-3333-444444444444"

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Simulate USER_ACTION_REQUIRED + CONFIRM_EVENT cognitive result."""
        conv_dir = tmp_path / "agy_data" / "conversations"
        if "--json-schema" in cmd:
            (conv_dir / f"{cls_conv}.db").write_bytes(b"sqlite")
            out = {
                "conversation_id": cls_conv,
                "structured_output": {
                    "output_type": "USER_ACTION_REQUIRED",
                    "reason": "Option petit-déjeuner à sélectionner avant le séjour",
                    "calendar_action": {
                        "intent": "CONFIRM_EVENT",
                        "slots": [
                            {
                                "summary": "Séjour Grand Hotel",
                                "start_iso": "2027-11-22",
                                "end_iso": "2027-11-24",
                                "attendees": [],
                                "send_invites": False,
                                "location": "1 Place Centrale, Paris",
                                "description": "Réservation #GH-789 — Option petit-déjeuner",
                            }
                        ],
                    },
                },
            }
            return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")
        (conv_dir / f"{inbox_conv}.db").write_bytes(b"sqlite")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"conversation_id": inbox_conv}), "")

    store, _, _, triage = _make_triage_rig(tmp_path, _runner)
    msg = EmailMessage(
        message_id="m_uar_cal",
        thread_id="th_uar_cal",
        sender="Reception <accueil@hotel.example>",
        recipient="owner@example.com",
        subject="Booking confirmation: Grand Hotel — Choix formule 2027-11-22",
        body_text="Votre séjour du 2027-11-22 au 2027-11-24 est confirmé. Choisissez votre option.",
    )
    decision = triage.process_message(msg, use_cognitive_agy=True)

    assert decision.output_type == InboxOutputType.USER_ACTION_REQUIRED
    assert decision.calendar_action_intent == CalendarActionIntent.CONFIRM_EVENT.value
    assert decision.implementation_plan_markdown is not None
    assert "Calendar Coordination" in decision.implementation_plan_markdown
    assert "--location" in decision.implementation_plan_markdown
    assert "--description" in decision.implementation_plan_markdown
    plan_file = Path(store.conversations.load().sessions[inbox_conv].plan_path)
    saved_plan = plan_file.read_text(encoding="utf-8")
    assert "Calendar Coordination" in saved_plan
    assert "--location" in saved_plan
    assert "--description" in saved_plan


def test_non_regression_ics_denylist_and_noreply_newsletter_zero_cognitive(
    tmp_path: Path,
) -> None:
    """ICS invites, denylist senders, and undated no-reply newsletters use 0 cognitive calls."""
    cognitive_calls: list[list[str]] = []

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Fail if cognitive classifier is triggered for non-booking pre-filtered mails."""
        if "--json-schema" in cmd:
            cognitive_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    settings = AlfredSettings(
        inbox_filters=InboxFilterConfig(denylist_senders=["blocked@example.test"])
    )
    _, _, _, triage = _make_triage_rig(tmp_path, _runner, settings=settings)

    ics_msg = EmailMessage(
        message_id="m_ics",
        thread_id="th_ics",
        sender="colleague@example.test",
        subject="Invitation: Point projet",
        mime_types=["text/calendar"],
        attachment_filenames=["invite.ics"],
        label_ids=["INBOX"],
    )
    ics_dec = triage.process_message(ics_msg, use_cognitive_agy=True)
    assert ics_dec.skipped_pre_filter is True
    assert "BYPASS_CALENDAR_INVITE" in ics_dec.reason

    deny_msg = EmailMessage(
        message_id="m_deny",
        thread_id="th_deny",
        sender="blocked@example.test",
        subject="Offre urgente",
        label_ids=["INBOX"],
    )
    deny_dec = triage.process_message(deny_msg, use_cognitive_agy=True)
    assert deny_dec.skipped_pre_filter is True
    assert "SKIP_DENYLIST" in deny_dec.reason

    news_msg = EmailMessage(
        message_id="m_news",
        thread_id="th_news",
        sender="Newsletter <no-reply@media.example>",
        subject="Newsletter hebdo du 22 novembre",
        body_text="Retrouvez tous nos articles de la semaine. Se désabonner.",
        headers={"List-Unsubscribe": "<https://media.example/unsub>"},
        label_ids=["INBOX"],
    )
    news_dec = triage.process_message(news_msg, use_cognitive_agy=True)
    assert news_dec.output_type == InboxOutputType.SILENT_NO_ACTION
    assert cognitive_calls == []


def test_inbox_triage_prompt_template_specifies_bookings_merchant_privacy_and_no_horizon() -> None:
    """Verify inbox_triage.md documents CONFIRM_EVENT bookings, merchant privacy, & no horizon."""
    prompt_path = WORKTREE_ROOT / "core" / "templates" / "prompts" / "inbox_triage.md"
    raw = prompt_path.read_text(encoding="utf-8")
    lower = raw.lower()

    assert "CONFIRM_EVENT" in raw
    assert re.search(r"send_invites\W+false", raw, re.IGNORECASE) is not None
    assert re.search(r"attendees\W+\[\s*\]", raw) is not None
    assert "ticket" in lower
    assert "reservation" in lower
    assert "merchant" in lower
    assert "near window" not in lower
    assert "14j" not in lower
    assert any(
        tok in lower
        for tok in ("no horizon", "all future events", "recurrences")
    )


def _make_omit_invites_runner(
    tmp_path: Path,
    cls_conv: str,
    cal_conv: str,
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Build a fake agy runner omitting send_invites on a CONFIRM_EVENT booking payload.

    Args:
        tmp_path: Temporary pytest root directory.
        cls_conv: Classifier conversation UUID.
        cal_conv: Target calendar Gate conversation UUID.
    """

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return CONFIRM_EVENT payload intentionally omitting send_invites."""
        conv_dir = tmp_path / "agy_data" / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        if "--json-schema" not in cmd:
            (conv_dir / f"{cal_conv}.db").write_bytes(b"sqlite")
            return subprocess.CompletedProcess(
                cmd, 0, json.dumps({"conversation_id": cal_conv}), ""
            )
        (conv_dir / f"{cls_conv}.db").write_bytes(b"sqlite")
        slot = {
            "summary": "Expérience Spatiale VR - Lyon",
            "start_iso": "2026-12-28T15:00:00+01:00",
            "end_iso": "2026-12-28T16:30:00+01:00",
            "attendees": [],
            "location": "12 Rue de la République, Lyon",
            "description": "ID du billet: 123456",
        }
        payload = {
            "conversation_id": cls_conv,
            "structured_output": {
                "output_type": "SILENT_NO_ACTION",
                "reason": "Confirmation billetterie datée",
                "calendar_action": {"intent": "CONFIRM_EVENT", "slots": [slot]},
            },
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")

    return _runner


def test_non_noreply_merchant_omitting_send_invites_does_not_leak_and_hides_sendupdates(
    tmp_path: Path,
) -> None:
    """Non-noreply vendor with omitted send_invites forces send_invites=False and no sendUpdates."""
    cls_conv = "99999999-1111-2222-3333-444444444444"
    cal_conv = "88888888-1111-2222-3333-444444444444"
    store, _, _, triage = _make_triage_rig(
        tmp_path, _make_omit_invites_runner(tmp_path, cls_conv, cal_conv)
    )
    msg = EmailMessage(
        message_id="m_non_noreply",
        thread_id="th_non_noreply",
        sender="Billetterie <bonjour@ticketing.example>",
        recipient="owner@example.com",
        subject="Confirmation d'achat : Expérience Spatiale VR - Lyon",
        body_text="lun. 28 déc. - 03:00 PM\nID du billet: 123456",
    )
    decision = triage.process_message(msg, use_cognitive_agy=True)
    assert decision.calendar_session_id == cal_conv
    plan_text = Path(store.conversations.load().sessions[cal_conv].plan_path).read_text(
        encoding="utf-8"
    )
    assert "--no-send-invites" in plan_text and "--attendee" not in plan_text
    assert "bonjour@ticketing.example" not in plan_text and 'sendUpdates="all"' not in plan_text
