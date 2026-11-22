"""Regression tests for booking detector, merchant privacy, trace resilience, & batching."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from alfred.controllers.app_factory import build_cli_controller
from alfred.domain.calendar_models import (
    CalendarActionIntent,
    CalendarSlotProposal,
    CalendarTriageAction,
)
from alfred.domain.models import AlfredPaths, AlfredSettings, EmailMessage, GoogleSyncBookmarks
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_helpers import fetch_paginated_events, parse_event_item
from alfred.services.calendar_service import CalendarService
from alfred.services.contacts_service import ContactsService
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_calendar_bridge import render_calendar_confirm_gate_plan
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.tasks_service import TasksService

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


class _Exec:
    """Minimal Google API execute() response wrapper."""

    def __init__(self, payload: dict[str, Any]) -> None:
        """Store static JSON response dictionary."""
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        """Return stored payload."""
        return self._payload


class _FakeCalClient:
    """In-memory Google Calendar v3 client & events stub recording list() calls."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        """Store scripted response pages and initialize call log."""
        self._pages = pages
        self.list_calls: list[dict[str, Any]] = []

    @property
    def events_res(self) -> _FakeCalClient:
        """Expose self as events resource for assertion consistency."""
        return self

    def events(self) -> _FakeCalClient:
        """Return fake events resource."""
        return self

    def list(self, **kwargs: Any) -> _Exec:
        """Record call kwargs, guard against loops, and return next scripted page."""
        self.list_calls.append(dict(kwargs))
        if len(self.list_calls) > 5:
            raise AssertionError("Infinite pagination loop detected in fetch_paginated_events")
        idx = min(len(self.list_calls) - 1, len(self._pages) - 1)
        return _Exec(self._pages[idx])

    def insert(self, **_kwargs: Any) -> _Exec:
        """Return synthetic event ID for CLI mutation tests."""
        return _Exec({"id": "ev_reg_1"})


class _FakeGmailBatch:
    """Minimal GmailService double supporting poll_new_messages batches and drafts."""

    def __init__(self, messages: list[EmailMessage]) -> None:
        """Initialize unread message batch and draft sink."""
        self._messages = messages
        self.drafts: list[tuple[str, str, str, str]] = []

    def get_authenticated_addresses(self) -> frozenset[str]:
        """Return owner email addresses."""
        return frozenset({"owner@example.com"})

    def list_new_messages_since(self, _history_id: str) -> tuple[list[EmailMessage], str]:
        """Return scripted batch of unread messages and updated historyId."""
        return (list(self._messages), "200")

    def star_message(self, _message_id: str) -> None:
        """No-op star."""

    def create_draft(
        self,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        _in_reply_to: str = "",
        _references: str = "",
    ) -> str:
        """Record created draft and return synthetic draft ID."""
        self.drafts.append((thread_id, to_addr, subject, body_text))
        return f"draft_{len(self.drafts)}"


def _make_email(subject: str, body: str = "", sender: str = "s@ticketing.example") -> EmailMessage:
    """Build a minimal normalized EmailMessage for regression tests.

    Args:
        subject: Email subject string.
        body: Plain-text email body.
        sender: Sender address string.
    """
    return EmailMessage(
        message_id="m_reg", thread_id="th_reg", sender=sender,
        recipient="owner@example.com", subject=subject, body_text=body,
    )


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("Confirmation de billet : Concert le 14 juillet 2027 à 20h00", "Place A12."),
        ("Confirmation de rendez-vous : Dr Martin le 03 février 2027 à 09h30", "Cabinet médical."),
        ("Confirmation de réservation : Visite musée le 15 avril 2027 à 14h00", "Entrée Nord."),
        ("Votre e-billet : Opéra le 10 septembre 2027 à 19h30", "Balcon rang 2."),
        ("Confirmation de séjour : Chalet le 12 octobre 2027 à 16h00", "Remise des clés."),
        ("Confirmation de rendez-vous : Notaire le 22 novembre 2026 à 11h00", "Salle 3."),
        ("Confirmation de votre réservation : Atelier Cuisine le 28/12/2026 à 18h00", "Confirmé."),
        ("Confirmation de votre rendez-vous : Clinique le 14/03/2027 à 10h30", "Bâtiment B."),
        ("Confirmation de votre vol : Paris -> Nice le 18/05/2027 départ 07h15", "Terminal 2."),
        ("Rappel de rendez-vous : Dentiste le 04/02/2027 à 15h00", "À demain."),
        ("Votre e-ticket pour le spectacle du 20/06/2027 à 20h30", "Code d'accès inclus."),
        ("Your boarding pass — Flight EX123 on 2027-07-14 departure 09:45", "Gate B4."),
        ("Votre carte d'embarquement — Vol EX456 le 14/07/2027 embarquement 08h30", "Siège 12A."),
        ("Confirmation de séjour : Hôtel du Parc", "Séjour du 14/07/2027 au 16/07/2027 confirmé."),
        (
            "Confirmation de réservation : Gîte Alpin",
            "Votre séjour du 14 juillet au 16 juillet 2027.",
        ),
        ("Booking confirmation: Lake Lodge", "Confirmed stay from July 14 to July 16, 2027."),
    ],
)
def test_booking_detector_french_months_determiners_keywords_and_ranges(
    subject: str,
    body: str,
) -> None:
    """Verify full FR months, determiners, flight/ticket terms, and stay date ranges return True."""
    from alfred.services.inbox_booking_detector import has_dated_booking_signal

    assert has_dated_booking_signal(_make_email(subject, body)) is True


@pytest.mark.parametrize(
    "footer_noise",
    [
        "Inscrivez-vous à notre newsletter pour recevoir nos prochains événements.",
        "Offre fidélité : -15% sur votre prochaine sortie.",
        "Utilisez le code promo HIVER15 lors de votre prochaine visite.",
        "Téléchargez votre reçu de paiement depuis votre espace client.",
    ],
)
def test_booking_detector_survives_promotional_or_receipt_footer(footer_noise: str) -> None:
    """Valid dated booking confirmation must stay True even with noisy promo/receipt footers."""
    from alfred.services.inbox_booking_detector import has_dated_booking_signal

    msg = _make_email(
        "Confirmation d'achat : Expérience Spatiale VR - Lyon",
        f"Votre billet est confirmé pour lun. 28 déc. - 03:00 PM\n"
        f"ID du billet: 123456\n\n{footer_noise}",
    )
    assert has_dated_booking_signal(msg) is True


def test_booking_detector_rejects_plain_ecommerce_while_keeping_dated_experience() -> None:
    """Timestamped physical e-commerce receipt returns False while dated ticket stays True."""
    from alfred.services.inbox_booking_detector import has_dated_booking_signal

    parcel_receipt = _make_email(
        "Confirmation d'achat de votre commande #42 du 22/11/2026 à 14h32 — Livraison standard",
        "Merci pour votre achat. Votre colis sera préparé sous 48h.",
    )
    dated_experience = _make_email(
        "Confirmation d'achat : Expérience Spatiale immersif - Lyon",
        "Votre réservation est confirmée.\nlun. 28 déc. - 03:00 PM\nID du billet: 123456",
    )
    assert has_dated_booking_signal(parcel_receipt) is False
    assert has_dated_booking_signal(dated_experience) is True


def test_render_calendar_confirm_gate_plan_omits_sendupdates_all_without_active_invites() -> None:
    """Gate Markdown hides sendUpdates='all' without active invites and keeps it otherwise."""
    msg = _make_email("Confirmation", "OK", sender="Advisor <advisor@example.test>")
    base_kw: dict[str, Any] = {
        "summary": "Spectacle",
        "start_iso": "2026-12-28T15:00:00+01:00",
        "end_iso": "2026-12-28T16:30:00+01:00",
    }
    no_inv = CalendarTriageAction(
        intent=CalendarActionIntent.CONFIRM_EVENT,
        slots=[CalendarSlotProposal(**base_kw, attendees=[], send_invites=False)],
    )
    empty_att = CalendarTriageAction(
        intent=CalendarActionIntent.CONFIRM_EVENT,
        slots=[CalendarSlotProposal(**base_kw, attendees=[], send_invites=True)],
    )
    with_inv = CalendarTriageAction(
        intent=CalendarActionIntent.CONFIRM_EVENT,
        slots=[
            CalendarSlotProposal(**base_kw, attendees=["advisor@example.test"], send_invites=True)
        ],
    )
    assert 'sendUpdates="all"' not in render_calendar_confirm_gate_plan(msg, "R", no_inv, [])
    assert 'sendUpdates="all"' not in render_calendar_confirm_gate_plan(msg, "R", empty_att, [])
    assert 'sendUpdates="all"' in render_calendar_confirm_gate_plan(msg, "R", with_inv, [])


def test_specialist_trace_oserror_degrades_gracefully_in_service_and_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError in archive_store or state_store degrades gracefully without crashing parent CLI."""
    from alfred.services.specialist_trace_service import SpecialistTraceService

    paths = AlfredPaths.from_env(tmp_path)
    cfg, store = AlfredSettings(), StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    cid = "77777777-1111-2222-3333-444444444444"

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Create synthetic db and return UUID."""
        (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
        (agy_dir / "conversations" / f"{cid}.db").write_bytes(b"sqlite")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"conversation_id": cid}), "")

    agy_mgr = AgySessionManager(
        paths, cfg, store, runner_fn=_runner, agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )
    monkeypatch.setattr(
        agy_mgr.archive_store, "set_conversation_title",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("disk full")),
    )
    rec = SpecialistTraceService().surface_mutation_trace(
        agy_mgr, cfg, "calendar", "T", "- body", archive_trace_flag=True
    )
    assert rec is not None and rec.status == "EXECUTED"

    monkeypatch.setattr(
        store.conversations, "update",
        lambda *_a, **_kw: (_ for _ in ()).throw(OSError("read-only fs")),
    )
    assert SpecialistTraceService().surface_mutation_trace(
        agy_mgr, cfg, "calendar", "T", "- body", archive_trace_flag=True
    ) is None

    monkeypatch.chdir(WORKTREE_ROOT / "core" / "workspaces" / "alfred-life")
    cal_svc = CalendarService(paths, cfg, calendar_client=_FakeCalClient([{"items": []}]))
    ctrl = build_cli_controller(
        paths=paths, settings=cfg, state_store=store, calendar_service=cal_svc,
        tasks_service=TasksService(paths, cfg), contacts_service=ContactsService(paths, cfg),
        gmail_service=GmailService(paths, cfg), agy_manager=agy_mgr,
    )
    rc = ctrl.run([
        "create-event", "--summary", "Test OSError",
        "--from", "2026-12-20T10:00:00+01:00", "--to", "2026-12-20T11:00:00+01:00",
    ])
    assert rc == 0


def test_host_agy_subprocess_safety_under_pytest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under PYTEST_CURRENT_TEST, host agy is never executed via subprocess even with agy_dir."""
    from alfred.services.specialist_trace_service import (
        SpecialistTraceService,
        is_live_trace_spawn_allowed,
    )

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "regression::test_host_agy_subprocess_safety")
    fake_home = tmp_path / "host_home"
    host_agy = fake_home / ".local" / "bin" / "agy"
    host_agy.parent.mkdir(parents=True, exist_ok=True)
    host_agy.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    host_agy.chmod(0o755)
    monkeypatch.setenv("HOME", str(fake_home))

    paths = AlfredPaths.from_env(tmp_path / "repo")
    store, agy_dir = StateStore(paths), tmp_path / "isolated_agy"
    default_cfg = AlfredSettings()
    isolated_mgr = AgySessionManager(paths, default_cfg, store, antigravity_data_dir=agy_dir)
    assert is_live_trace_spawn_allowed(isolated_mgr, default_cfg) is True

    subproc_calls: list[list[str]] = []

    def _spy_run(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        """Record subprocess invocation without running host binary."""
        subproc_calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", _spy_run)
    host_bin_cfg = AlfredSettings(agy_binary=str(host_agy))
    host_bin_mgr = AgySessionManager(paths, host_bin_cfg, store, antigravity_data_dir=agy_dir)
    svc = SpecialistTraceService()
    svc.surface_mutation_trace(
        host_bin_mgr, host_bin_cfg, "calendar", "T", "- b", archive_trace_flag=True
    )
    svc._spawn_trace_conversation(isolated_mgr, "alfred-calendar", "T", "- b")
    assert subproc_calls == []


def test_failed_trace_spawn_purges_incomplete_stubs(tmp_path: Path) -> None:
    """Failed trace spawn purges newly left 0-step RUNNING stubs while keeping existing ones."""
    from alfred.services.specialist_trace_service import SpecialistTraceService

    paths, cfg = AlfredPaths.from_env(tmp_path / "repo"), AlfredSettings()
    store, agy_dir = StateStore(paths), tmp_path / "isolated_agy"
    (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
    db_path = agy_dir / "conversation_summaries.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY, "
            "title TEXT DEFAULT '', project_id TEXT DEFAULT '', status TEXT DEFAULT '', "
            "step_count INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO conversation_summaries VALUES ('existing-stub-0', '', 'p', "
            "'CASCADE_RUN_STATUS_RUNNING', 0)"
        )
        conn.commit()
    (agy_dir / "conversations" / "existing-stub-0.db").write_bytes(b"db")

    def _failing_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Leave an orphan 0-step RUNNING stub before failing with exit code 1."""
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO conversation_summaries VALUES ('orphan-stub-1', '', 'p', "
                "'CASCADE_RUN_STATUS_RUNNING', 0)"
            )
            conn.commit()
        (agy_dir / "conversations" / "orphan-stub-1.db").write_bytes(b"db")
        return subprocess.CompletedProcess(cmd, 1, "", "failed")

    fail_mgr = AgySessionManager(
        paths, cfg, store, runner_fn=_failing_runner,
        agy_locator_fn=lambda: "/usr/bin/agy", antigravity_data_dir=agy_dir,
    )
    assert SpecialistTraceService()._spawn_trace_conversation(
        fail_mgr, "alfred-calendar", "T", "- b"
    ) is None
    assert not (agy_dir / "conversations" / "orphan-stub-1.db").exists()
    assert (agy_dir / "conversations" / "existing-stub-0.db").exists()


def test_calendar_helpers_handle_explicit_none_fields_and_repeated_page_tokens() -> None:
    """parse_event_item tolerates None JSON fields and fetch_paginated_events breaks loop tokens."""
    item = parse_event_item({
        "id": "e1", "summary": None, "start": None, "end": None, "extendedProperties": None,
        "attendees": None, "recurrence": None, "description": None, "location": None,
    })
    assert item.event_id == "e1" and item.summary == "" and item.start_iso == ""
    assert item.attendees == [] and item.recurrence == []

    fake_client = _FakeCalClient([
        {"items": None, "nextPageToken": "tok_loop"},
        {
            "items": [{
                "id": "e_ok", "summary": "Valid",
                "start": {"date": "2027-01-01"}, "end": {"date": "2027-01-02"},
            }],
            "nextPageToken": "tok_loop",
        },
    ])
    events = fetch_paginated_events(fake_client, "primary", "2026-11-22T00:00:00Z")
    assert [ev.event_id for ev in events] == ["e_ok"]
    assert len(fake_client.events_res.list_calls) == 2


def test_poll_new_messages_caches_calendar_snapshot_once_per_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """poll_new_messages fetches future calendar events at most once across a multi-email batch."""
    prompts_seen: list[str] = []

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Record prompt and return SILENT_NO_ACTION JSON."""
        if "--json-schema" not in cmd:
            return subprocess.CompletedProcess(cmd, 0, "{}", "")
        prompts_seen.append(cmd[cmd.index("--print") + 1])
        out = {"structured_output": {"output_type": "SILENT_NO_ACTION", "reason": "OK"}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    paths, cfg = AlfredPaths.from_env(tmp_path), AlfredSettings()
    store = StateStore(paths)
    store.bookmarks.save(GoogleSyncBookmarks(gmail_history_id="100"))
    fake_cal = _FakeCalClient([{"items": [{
        "id": "ev_far", "summary": "Horizon +1 An", "status": "confirmed",
        "start": {"dateTime": "2027-11-25T10:00:00+01:00"},
        "end": {"dateTime": "2027-11-25T11:00:00+01:00"},
    }]}])
    cal_svc = CalendarService(paths, cfg, calendar_client=fake_cal, state_store=store)
    orig_list_future, future_calls = cal_svc.list_future_events, 0

    def _counted_list_future(calendar_id: str, time_min_iso: str) -> Any:
        """Count list_future_events invocations and delegate."""
        nonlocal future_calls
        future_calls += 1
        return orig_list_future(calendar_id, time_min_iso)

    monkeypatch.setattr(cal_svc, "list_future_events", _counted_list_future)
    batch = [
        _make_email(f"Message {i}", f"Dispo #{i} ?", sender=f"u{i}@example.test").model_copy(
            update={"message_id": f"mb_{i}", "thread_id": f"thb_{i}", "label_ids": ["INBOX"]}
        )
        for i in range(1, 4)
    ]
    agy_mgr = AgySessionManager(
        paths, cfg, store, runner_fn=_runner, agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=tmp_path / "agy_data",
    )
    triage = InboxTriageService(
        store, _FakeGmailBatch(batch), KnowledgeService(paths, cfg, store),  # type: ignore[arg-type]
        DriveService(paths, cfg, store), agy_mgr, calendar_service=cal_svc, settings=cfg,
    )
    decisions = triage.poll_new_messages(use_cognitive_agy=True)
    assert len(decisions) == 3 and future_calls <= 1 and len(fake_cal.events_res.list_calls) == 1
    assert len(prompts_seen) == 3 and all("2027-11-25" in p for p in prompts_seen)
