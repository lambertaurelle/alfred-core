"""WP7 TDD suite (Part 2): allowlisted [HOLD] blocks, confirmation Gates, and Calendar CLI."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alfred.domain.calendar_models import CalendarActionIntent, CalendarSlotProposal
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    EmailMessage,
    InboxOutputType,
)
from alfred.main import build_controller
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService


class _FakeExec:
    """Minimal Google API execute() wrapper."""

    def __init__(self, payload: dict[str, Any]) -> None:
        """Store static response payload."""
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        """Return stored response payload."""
        return self._payload


class _FakeCalendarEventsResource:
    """In-memory fake Google Calendar v3 events() resource recording API calls."""

    def __init__(self) -> None:
        """Initialize empty event store and call logs."""
        self.items: dict[str, dict[str, Any]] = {}
        self.insert_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []
        self._seq = 0

    def list(self, **kwargs: Any) -> _FakeExec:
        """Return all stored calendar events."""
        return _FakeExec({"items": list(self.items.values()), "list_kwargs": kwargs})

    def insert(self, **kwargs: Any) -> _FakeExec:
        """Insert a calendar event and record kwargs (including sendUpdates)."""
        self._seq += 1
        ev_id = f"ev_{self._seq}"
        stored = {"id": ev_id, **dict(kwargs["body"])}
        self.items[ev_id] = stored
        self.insert_calls.append({"id": ev_id, **kwargs})
        return _FakeExec(stored)

    def update(self, **kwargs: Any) -> _FakeExec:
        """Update an existing calendar event and record kwargs."""
        ev_id = str(kwargs["eventId"])
        stored = {"id": ev_id, **dict(kwargs["body"])}
        self.items[ev_id] = stored
        self.update_calls.append(kwargs)
        return _FakeExec(stored)

    def delete(self, **kwargs: Any) -> _FakeExec:
        """Delete an event and record kwargs."""
        ev_id = str(kwargs["eventId"])
        self.items.pop(ev_id, None)
        self.delete_calls.append(kwargs)
        return _FakeExec({})


class _FakeCalendarClient:
    """In-memory Google Calendar v3 client."""

    def __init__(self) -> None:
        """Create underlying fake events resource."""
        self.events_res = _FakeCalendarEventsResource()

    def events(self) -> _FakeCalendarEventsResource:
        """Return the in-memory fake calendar events resource."""
        return self.events_res


class _FakeGmailStub:
    """Minimal GmailService stub for Inbox + Calendar Gate tests."""

    def __init__(self, owner_addrs: frozenset[str] = frozenset()) -> None:
        """Initialize owner addresses and draft log."""
        self.owner_addrs = owner_addrs
        self.drafts: list[tuple[str, str, str, str]] = []

    def get_authenticated_addresses(self) -> frozenset[str]:
        """Return configured owner email addresses."""
        return self.owner_addrs

    def star_message(self, message_id: str) -> None:
        """No-op star stub."""

    def create_draft(
        self,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to: str = "",
        references: str = "",
    ) -> str:
        """Record a created Gmail draft."""
        self.drafts.append((thread_id, to_addr, subject, body_text))
        return f"draft_{len(self.drafts)}"


def _build_triage_rig(
    tmp_path: Path,
    cls_conv: str,
    runner_fn: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> tuple[StateStore, Path, _FakeCalendarClient, CalendarService, InboxTriageService]:
    """Build an isolated InboxTriageService + CalendarService rig with seeded agy_data."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings()
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
    (agy_dir / "conversations" / f"{cls_conv}.db").write_bytes(b"sqlite")
    conn = sqlite3.connect(agy_dir / "conversation_summaries.db")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, title TEXT DEFAULT '', project_id TEXT DEFAULT '')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO conversation_summaries VALUES (?, '', 'proj-inbox')",
            (cls_conv,),
        )
        conn.commit()
    finally:
        conn.close()
    fake_cal_client = _FakeCalendarClient()
    cal = CalendarService(paths, settings, calendar_client=fake_cal_client, state_store=store)
    agy = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=runner_fn,
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )
    triage = InboxTriageService(
        store,
        _FakeGmailStub(frozenset({"owner1@example.test"})),  # type: ignore[arg-type]
        KnowledgeService(paths, settings, store),
        DriveService(paths, settings, store),
        agy,
        calendar_service=cal,
        spawn_live_agy=True,
    )
    return store, agy_dir, fake_cal_client, cal, triage


def test_hold_allowlist_sends_hold_only_to_allowlisted_emails(tmp_path: Path) -> None:
    """create_hold_event invites allowlisted emails and defers non-allowlisted external guests."""
    paths = AlfredPaths.from_env(tmp_path)
    fake_client = _FakeCalendarClient()
    cal = CalendarService(
        paths, AlfredSettings(), calendar_client=fake_client, state_store=StateStore(paths)
    )
    cal.set_hold_allowlist_entry("partner@example.test")

    owners = frozenset({"owner1@example.test", "owner2@example.test"})
    slot = CalendarSlotProposal(
        summary="Point Notaire",
        start_iso="2026-11-17T14:00:00+01:00",
        end_iso="2026-11-17T15:00:00+01:00",
        attendees=["partner@example.test", "owner2@example.test", "notary@external.test"],
        location="Visio",
    )
    ev_id = cal.create_hold_event("primary", slot, thread_id="th_notaire", owner_addresses=owners)
    assert ev_id == "ev_1"
    call = fake_client.events_res.insert_calls[0]
    assert call["sendUpdates"] == "all"
    invited = {a["email"] for a in call["body"]["attendees"]}
    assert invited == {"partner@example.test"}
    assert "owner2@example.test" not in invited
    assert "notary@external.test" not in invited
    assert call["body"]["summary"] == "[HOLD] Point Notaire"
    priv = call["body"]["extendedProperties"]["private"]
    assert priv["alfred_hold"] == "true"
    assert "notary@external.test" in priv["alfred_candidate_attendees"]


def test_confirm_event_converts_hold_invites_external_and_frees_other_holds(
    tmp_path: Path,
) -> None:
    """confirm_event converts the chosen [HOLD], sends external invite, and frees other [HOLD]s."""
    paths = AlfredPaths.from_env(tmp_path)
    fake_client = _FakeCalendarClient()
    cal = CalendarService(
        paths, AlfredSettings(), calendar_client=fake_client, state_store=StateStore(paths)
    )
    cal.set_hold_allowlist_entry("partner@example.test")
    owners = frozenset({"owner1@example.test"})
    slot_a = CalendarSlotProposal(
        summary="RDV Comptable",
        start_iso="2026-11-18T10:00:00+01:00",
        end_iso="2026-11-18T11:00:00+01:00",
        attendees=["partner@example.test", "cpa@external.test"],
    )
    slot_b = CalendarSlotProposal(
        summary="RDV Comptable",
        start_iso="2026-11-19T15:00:00+01:00",
        end_iso="2026-11-19T16:00:00+01:00",
        attendees=["partner@example.test", "cpa@external.test"],
    )
    hold_a = cal.create_hold_event("primary", slot_a, thread_id="th_cpa", owner_addresses=owners)
    hold_b = cal.create_hold_event("primary", slot_b, thread_id="th_cpa", owner_addresses=owners)

    confirmed_slot = CalendarSlotProposal(
        summary="[HOLD] RDV Comptable",
        start_iso="2026-11-19T15:00:00+01:00",
        end_iso="2026-11-19T16:00:00+01:00",
        attendees=[],
        send_invites=True,
    )
    conf_id, released = cal.confirm_event("primary", confirmed_slot, thread_id="th_cpa")
    assert conf_id == hold_b and released == [hold_a]
    upd = fake_client.events_res.update_calls[0]
    assert upd["sendUpdates"] == "all" and upd["body"]["summary"] == "RDV Comptable"
    assert {a["email"] for a in upd["body"]["attendees"]} == {
        "partner@example.test",
        "cpa@external.test",
    }
    assert fake_client.events_res.delete_calls[0]["sendUpdates"] == "all"


def test_confirmation_email_spawns_mandatory_calendar_gate_without_auto_invite(
    tmp_path: Path,
) -> None:
    """Confirmation email opens a mandatory Gate in alfred-calendar and sends 0 invites pre-Gate."""
    cls_conv = "cccccccc-1111-2222-3333-444444444444"
    cal_gate_conv = "dddddddd-1111-2222-3333-444444444444"
    spawned_projects: list[str] = []

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Simulate cognitive CONFIRM_EVENT classification followed by Gate spawn."""
        if "--json-schema" in cmd:
            payload = {
                "conversation_id": cls_conv,
                "structured_output": {
                    "output_type": "SILENT_NO_ACTION",
                    "reason": "Validation du créneau par l'interlocuteur",
                    "calendar_action": {
                        "intent": "CONFIRM_EVENT",
                        "slots": [
                            {
                                "summary": "Point Trimestriel",
                                "start_iso": "2026-11-20T14:00:00+01:00",
                                "end_iso": "2026-11-20T14:30:00+01:00",
                                "attendees": ["advisor@external.test"],
                                "send_invites": True,
                            }
                        ],
                    },
                },
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        if "--project" in cmd:
            spawned_projects.append(cmd[cmd.index("--project") + 1])
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"conversation_id": cal_gate_conv}), stderr=""
        )

    store, _, fake_cal, _, triage = _build_triage_rig(tmp_path, cls_conv, fake_runner)
    msg = EmailMessage(
        message_id="m_conf_1",
        thread_id="th_conf",
        sender="Advisor <advisor@external.test>",
        recipient="owner1@example.test",
        subject="Re: Point Trimestriel",
        body_text="Parfait pour vendredi 20 novembre à 14h, à vendredi !",
    )
    decision = triage.process_message(msg, use_cognitive_agy=True)
    assert fake_cal.events_res.insert_calls == [] and fake_cal.events_res.update_calls == []
    assert decision.calendar_action_intent == CalendarActionIntent.CONFIRM_EVENT.value
    assert decision.calendar_session_id == cal_gate_conv and "alfred-calendar" in spawned_projects
    plan_text = Path(store.conversations.load().sessions[cal_gate_conv].plan_path).read_text()
    assert "advisor@external.test" in plan_text and "--send-invites" in plan_text


def test_draft_reply_with_hold_renders_allowlist_and_purges_classifier_conv(
    tmp_path: Path,
) -> None:
    """DRAFT_REPLY + CREATE_HOLD purges transient classifier conv and embeds HOLD command."""
    cls_conv = "eeeeeeee-1111-2222-3333-444444444444"
    gate_conv = "ffffffff-1111-2222-3333-444444444444"

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Simulate cognitive DRAFT_REPLY + CREATE_HOLD classification followed by Gate spawn."""
        if "--json-schema" in cmd:
            payload = {
                "conversation_id": cls_conv,
                "structured_output": {
                    "output_type": "DRAFT_REPLY",
                    "reason": "Proposition de créneau",
                    "draft_body": "Bonjour, seriez-vous disponible mardi à 11h ?",
                    "calendar_action": {
                        "intent": "CREATE_HOLD",
                        "slots": [
                            {
                                "summary": "Appel Architecte",
                                "start_iso": "2026-11-24T11:00:00+01:00",
                                "end_iso": "2026-11-24T11:45:00+01:00",
                                "attendees": ["partner@example.test", "arch@external.test"],
                            }
                        ],
                    },
                },
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"conversation_id": gate_conv}), stderr=""
        )

    _, agy_dir, _, cal, triage = _build_triage_rig(tmp_path, cls_conv, fake_runner)
    cal.set_hold_allowlist_entry("partner@example.test")
    msg = EmailMessage(
        message_id="m_hold_1",
        thread_id="th_arch",
        sender="arch@external.test",
        recipient="owner1@example.test, partner@example.test",
        subject="Chantier cuisine",
        body_text="Quand peut-on s'appeler ?",
    )
    decision = triage.process_message(msg, use_cognitive_agy=True)
    assert decision.output_type == InboxOutputType.DRAFT_REPLY
    assert not (agy_dir / "conversations" / f"{cls_conv}.db").exists()
    assert decision.implementation_plan_markdown is not None
    assert "create-event" in decision.implementation_plan_markdown
    assert "--hold" in decision.implementation_plan_markdown
    assert "partner@example.test" in decision.implementation_plan_markdown


def test_calendar_cli_subcommands_end_to_end(tmp_path: Path, capsys: Any) -> None:
    """CLI hold-allowlist, create-event --hold, confirm-event, and delete-event work end-to-end."""
    ctrl = build_controller(str(tmp_path))
    ctrl._calendar._calendar_client = _FakeCalendarClient()
    ctrl._gmail = GmailService(
        AlfredPaths.from_env(tmp_path), AlfredSettings(), gmail_client=None
    )

    assert ctrl.run(["hold-allowlist", "--add", "partner@example.test"]) == 0
    assert "partner@example.test" in json.loads(capsys.readouterr().out)["emails"]

    create_args = [
        "create-event",
        "--summary",
        "Visite Ecole",
        "--from",
        "2026-11-25T09:00:00+01:00",
        "--to",
        "2026-11-25T10:00:00+01:00",
        "--hold",
        "--attendee",
        "partner@example.test",
        "--attendee",
        "director@school.test",
        "--thread-id",
        "th_school",
    ]
    assert ctrl.run(create_args) == 0
    assert json.loads(capsys.readouterr().out)["event_id"] == "ev_1"

    confirm_args = [
        "confirm-event",
        "--summary",
        "Visite Ecole",
        "--from",
        "2026-11-25T09:00:00+01:00",
        "--to",
        "2026-11-25T10:00:00+01:00",
        "--attendee",
        "partner@example.test",
        "--attendee",
        "director@school.test",
        "--send-invites",
        "--thread-id",
        "th_school",
    ]
    assert ctrl.run(confirm_args) == 0
    assert json.loads(capsys.readouterr().out)["confirmed_event_id"] == "ev_1"

    assert ctrl.run(["delete-event", "--event-id", "ev_1"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted_event_id"] == "ev_1"
