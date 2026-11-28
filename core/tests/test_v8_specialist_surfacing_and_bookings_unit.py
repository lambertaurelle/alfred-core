"""Unit tests for specialist trace surfacing, dated booking detection, and full-horizon calendar."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from alfred.domain.calendar_models import (
    CalendarActionIntent,
    CalendarSlotProposal,
    CalendarTriageAction,
)
from alfred.domain.models import AlfredPaths, AlfredSettings, EmailMessage
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.inbox_calendar_bridge import (
    enrich_calendar_action_attendees,
    load_upcoming_calendar_snapshot,
)

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


class _ExecWrapper:
    """Minimal Google API execute() response wrapper."""

    def __init__(self, payload: dict[str, Any]) -> None:
        """Store static JSON response dictionary."""
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        """Return the stored response dictionary."""
        return self._payload


class _FakeEventsResource:
    """In-memory Google Calendar events() resource recording list() calls and paging."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        """Initialize paginated event items and empty call ledger."""
        self._pages = pages
        self.list_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _ExecWrapper:
        """Record call kwargs and return the requested page of events."""
        self.list_calls.append(dict(kwargs))
        token = kwargs.get("pageToken")
        page_idx = int(str(token)) if token is not None else 0
        items = self._pages[page_idx] if page_idx < len(self._pages) else []
        payload: dict[str, Any] = {"items": items}
        if page_idx + 1 < len(self._pages):
            payload["nextPageToken"] = str(page_idx + 1)
        return _ExecWrapper(payload)


class _FakeCalendarClient:
    """In-memory Google Calendar v3 client exposing events()."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        """Wrap paginated events resource."""
        self.events_res = _FakeEventsResource(pages)

    def events(self) -> _FakeEventsResource:
        """Return fake events resource."""
        return self.events_res


def _make_agy_runner(
    agy_dir: Path,
    conv_id: str,
    recorded_cmds: list[list[str]],
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    """Build a deterministic fake agy runner creating <conv_id>.db and returning JSON."""

    def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Create synthetic conversation sqlite db and return conversation_id JSON."""
        recorded_cmds.append(list(cmd))
        conv_dir = agy_dir / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        (conv_dir / f"{conv_id}.db").write_bytes(b"sqlite")
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"conversation_id": conv_id}), stderr=""
        )

    return _run


def test_should_surface_trace_flag_and_cwd_deduplication(tmp_path: Path) -> None:
    """Verify explicit trace flags and cwd-based specialist workspace deduplication."""
    from alfred.services.specialist_trace_service import should_surface_trace

    ws_root = WORKTREE_ROOT / "core" / "workspaces"
    life_dir, inbox_dir, evolve_dir = (
        ws_root / "alfred-life",
        ws_root / "alfred-inbox",
        ws_root / "alfred-evolve",
    )
    cal_dir, tasks_dir = ws_root / "alfred-calendar", ws_root / "alfred-tasks"

    for cwd_cand in (None, life_dir, cal_dir, tasks_dir, tmp_path):
        assert should_surface_trace(False, "alfred-calendar", cwd=cwd_cand) is False
        assert should_surface_trace(False, "alfred-tasks", cwd=cwd_cand) is False

    assert should_surface_trace(True, "alfred-calendar", cwd=cal_dir) is True
    assert should_surface_trace(True, "alfred-tasks", cwd=tasks_dir) is True

    for external_cwd in (life_dir, inbox_dir, evolve_dir, tmp_path):
        assert should_surface_trace(None, "alfred-calendar", cwd=external_cwd) is True
        assert should_surface_trace(None, "alfred-tasks", cwd=external_cwd) is True

    assert should_surface_trace(None, "alfred-calendar", cwd=cal_dir) is False
    assert should_surface_trace(None, "alfred-tasks", cwd=tasks_dir) is False
    assert should_surface_trace(None, "alfred-tasks", cwd=cal_dir) is True
    assert should_surface_trace(None, "alfred-calendar", cwd=tasks_dir) is True


def test_is_live_trace_spawn_allowed_sandbox_vs_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify pytest host guard blocks default home dir while allowing isolated test rigs."""
    from alfred.services.specialist_trace_service import is_live_trace_spawn_allowed

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "unit::test_is_live_trace_spawn_allowed")
    paths = AlfredPaths.from_env(tmp_path / "repo")
    store = StateStore(paths)
    default_settings = AlfredSettings()

    default_mgr = AgySessionManager(paths, default_settings, store)
    assert is_live_trace_spawn_allowed(default_mgr, default_settings) is False

    isolated_dir_mgr = AgySessionManager(
        paths, default_settings, store, antigravity_data_dir=tmp_path / "agy_data"
    )
    assert is_live_trace_spawn_allowed(isolated_dir_mgr, default_settings) is True

    runner_mgr = AgySessionManager(
        paths,
        default_settings,
        store,
        runner_fn=lambda cmd: subprocess.CompletedProcess(cmd, 0, "{}", ""),
    )
    assert is_live_trace_spawn_allowed(runner_mgr, default_settings) is True

    custom_bin_settings = AlfredSettings(agy_binary=str(tmp_path / "fake_agy.sh"))
    custom_bin_mgr = AgySessionManager(paths, custom_bin_settings, store)
    assert is_live_trace_spawn_allowed(custom_bin_mgr, custom_bin_settings) is True


@pytest.mark.parametrize(
    ("target_role", "expected_project", "conv_id"),
    [
        ("calendar", "alfred-calendar", "11111111-2222-3333-4444-555555555555"),
        ("tasks", "alfred-tasks", "22222222-3333-4444-5555-666666666666"),
    ],
)
def test_specialist_trace_service_surfaces_visible_executed_trace(
    tmp_path: Path,
    target_role: str,
    expected_project: str,
    conv_id: str,
) -> None:
    """Verify SpecialistTraceService spawns trace model, sets archived:false, and marks dirty."""
    from alfred.services.specialist_trace_service import SpecialistTraceService

    paths = AlfredPaths.from_env(tmp_path / "repo")
    settings = AlfredSettings()
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    cmds: list[list[str]] = []
    agy_mgr = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=_make_agy_runner(agy_dir, conv_id, cmds),
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )

    record = SpecialistTraceService().surface_mutation_trace(
        agy_manager=agy_mgr,
        settings=settings,
        target_role=target_role,
        title=f"[{expected_project}] Mutation test",
        trace_markdown="- Action exécutée",
        source_id="src_001",
        archive_trace_flag=None,
        cwd=WORKTREE_ROOT / "core" / "workspaces" / "alfred-life",
    )

    assert record is not None
    assert record.session_id == conv_id and record.status == "EXECUTED"
    assert record.workspace == expected_project and record.source_message_id == "src_001"
    assert len(cmds) == 1
    cmd = cmds[0]
    assert cmd[cmd.index("--project") + 1] == expected_project
    assert cmd[cmd.index("--model") + 1] == "gemini-pro-orchestrator-neon"
    assert "--effort" not in cmd

    pbtxt_text = (agy_dir / "annotations" / f"{conv_id}.pbtxt").read_text(encoding="utf-8")
    assert "archived:false" in pbtxt_text and "archived:true" not in pbtxt_text
    persisted = store.conversations.load().sessions[conv_id]
    assert persisted.status == "EXECUTED" and persisted.workspace == expected_project
    dirty = agy_mgr.daemon_sync.load_state()
    assert dirty.pending is True and conv_id in dirty.conversation_ids


def test_specialist_trace_service_skips_when_suppressed(tmp_path: Path) -> None:
    """Verify surface_mutation_trace returns None and emits zero spawn when opted out."""
    from alfred.services.specialist_trace_service import SpecialistTraceService

    paths = AlfredPaths.from_env(tmp_path / "repo")
    settings = AlfredSettings()
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    cmds: list[list[str]] = []
    agy_mgr = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=_make_agy_runner(agy_dir, "33333333-4444-5555-6666-777777777777", cmds),
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )

    svc, ws_root = SpecialistTraceService(), WORKTREE_ROOT / "core" / "workspaces"
    res_false = svc.surface_mutation_trace(
        agy_mgr, settings, "calendar", "T", "B", archive_trace_flag=False,
        cwd=ws_root / "alfred-life",
    )
    res_same = svc.surface_mutation_trace(
        agy_mgr, settings, "calendar", "T", "B", archive_trace_flag=None,
        cwd=ws_root / "alfred-calendar",
    )
    assert res_false is None and res_same is None
    assert cmds == [] and store.conversations.load().sessions == {}


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        (
            "Confirmation d'achat : Expérience Spatiale VR - Lyon",
            "Votre réservation est validée.\nlun. 28 déc. - 03:00 PM\nID du billet: 123456",
        ),
        (
            "Confirmation de rendez-vous : Dr Martin le 14/03/2027 à 10h30",
            "Merci de vous présenter 10 minutes avant l'heure prévue.",
        ),
        (
            "Booking confirmation: Grand Hotel — Check-in 2027-11-22",
            "Check-in: 2027-11-22, Check-out: 2027-11-24.",
        ),
        (
            "Votre e-billet Train Paris -> Lyon le 28/12/2026 départ 08h18",
            "Voiture 12, Place 44. Bon voyage.",
        ),
    ],
)
def test_has_dated_booking_signal_positive_cases(subject: str, body: str) -> None:
    """Verify FR/EN dated ticket, appointment, hotel, and train confirmations return True."""
    from alfred.services.inbox_booking_detector import has_dated_booking_signal

    msg = EmailMessage(
        message_id="m_pos",
        thread_id="th_pos",
        sender="Service <salut@ticketing.example>",
        subject=subject,
        body_text=body,
    )
    assert has_dated_booking_signal(msg) is True


@pytest.mark.parametrize(
    ("subject", "body"),
    [
        ("Reçu pour votre paiement de 116,60 € le 22/11/2026", "Transaction enregistrée."),
        ("Votre commande #42", "Merci pour votre achat. Colis en préparation."),
        ("Newsletter hebdo du 22 novembre", "Actualités de la semaine. Se désabonner."),
        ("Votre facture mensuelle d'abonnement est disponible", "Consultez votre espace client."),
        ("123456 est votre code de vérification", "Ce code expire dans 10 minutes."),
        ("Offre Flash : -30% sur vos billets jusqu'au 28 déc. à 23h59 !", "Profitez-en vite."),
    ],
)
def test_has_dated_booking_signal_negative_non_regression(subject: str, body: str) -> None:
    """Verify receipts, parcels, newsletters, invoices, OTPs, and promos stay False."""
    from alfred.services.inbox_booking_detector import has_dated_booking_signal

    msg = EmailMessage(
        message_id="m_neg",
        thread_id="th_neg",
        sender="Notification <noreply@example.com>",
        subject=subject,
        body_text=body,
    )
    assert has_dated_booking_signal(msg) is False


def test_is_automated_correspondent_address_rules() -> None:
    """Verify automated sender addresses are detected while human advisors are preserved."""
    from alfred.services.inbox_booking_detector import is_automated_correspondent_address

    for addr in (
        "noreply@example.com",
        "no-reply@example.com",
        "ne-pas-repondre@example.com",
        "do-not-reply@example.com",
        "mailer-daemon@example.com",
        "bounces+123@example.com",
        "feedback@example.com",
        "notifications@example.com",
    ):
        assert is_automated_correspondent_address(addr) is True
    assert is_automated_correspondent_address("conseiller@example.com") is False


def test_calendar_list_future_events_no_horizon_filters_cancelled_and_sorts(
    tmp_path: Path,
) -> None:
    """Verify list_future_events queries singleEvents=False without timeMax or orderBy."""
    pages = [
        [
            {
                "id": "ev_late",
                "summary": "Horizon Lointain",
                "status": "confirmed",
                "start": {"dateTime": "2036-06-01T10:00:00+02:00"},
                "end": {"dateTime": "2036-06-01T11:00:00+02:00"},
            },
            {
                "id": "ev_cancelled",
                "summary": "Annulé",
                "status": "cancelled",
                "start": {"dateTime": "2027-01-10T09:00:00+01:00"},
                "end": {"dateTime": "2027-01-10T10:00:00+01:00"},
            },
        ],
        [
            {
                "id": "ev_early",
                "summary": "Anniversaire Récurrent",
                "status": "confirmed",
                "start": {"date": "2026-12-05"},
                "end": {"date": "2026-12-06"},
                "recurrence": ["RRULE:FREQ=YEARLY"],
            }
        ],
    ]
    client = _FakeCalendarClient(pages)
    svc = CalendarService(
        AlfredPaths.from_env(tmp_path), AlfredSettings(), calendar_client=client
    )

    events = svc.list_future_events("primary", "2026-11-22T00:00:00+01:00")
    assert [ev.event_id for ev in events] == ["ev_early", "ev_late"]
    assert events[0].recurrence == ["RRULE:FREQ=YEARLY"]

    assert len(client.events_res.list_calls) == 2
    first_call = client.events_res.list_calls[0]
    assert first_call["singleEvents"] is False
    assert first_call["timeMin"] == "2026-11-22T00:00:00+01:00"
    assert "timeMax" not in first_call and "orderBy" not in first_call


def test_load_upcoming_calendar_snapshot_has_no_horizon_or_truncation(tmp_path: Path) -> None:
    """Verify snapshot returns all 60+ future events up to +10y and yearly recurrences."""
    raw_items: list[dict[str, Any]] = []
    for i in range(1, 59):
        day_str = f"{2027 + (i // 12):04d}-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}"
        raw_items.append(
            {
                "id": f"ev_{i}",
                "summary": f"Événement Futur {i:02d}",
                "status": "confirmed",
                "start": {"dateTime": f"{day_str}T10:00:00+01:00"},
                "end": {"dateTime": f"{day_str}T11:00:00+01:00"},
            }
        )
    raw_items.extend(
        [
            {
                "id": "ev_59",
                "summary": "Bilan +1 An",
                "status": "confirmed",
                "start": {"dateTime": "2027-11-25T14:00:00+01:00"},
                "end": {"dateTime": "2027-11-25T15:00:00+01:00"},
            },
            {
                "id": "ev_60",
                "summary": "Échéance +10 Ans",
                "status": "confirmed",
                "start": {"dateTime": "2036-06-01T09:00:00+02:00"},
                "end": {"dateTime": "2036-06-01T10:00:00+02:00"},
            },
            {
                "id": "ev_rec_1",
                "summary": "Déclaration Annuelle",
                "status": "confirmed",
                "start": {"date": "2027-05-15"},
                "end": {"date": "2027-05-16"},
                "recurrence": ["RRULE:FREQ=YEARLY"],
            },
        ]
    )
    settings = AlfredSettings()
    cal_svc = CalendarService(
        AlfredPaths.from_env(tmp_path), settings, calendar_client=_FakeCalendarClient([raw_items])
    )

    snapshot_text, returned_events = load_upcoming_calendar_snapshot(cal_svc, settings)
    assert len(returned_events) == 61
    assert "Événement Futur 01" in snapshot_text and "Événement Futur 58" in snapshot_text
    assert "Bilan +1 An" in snapshot_text and "2027-11-25" in snapshot_text
    assert "Échéance +10 Ans" in snapshot_text and "2036-06-01" in snapshot_text
    assert "Déclaration Annuelle" in snapshot_text and "RRULE:FREQ=YEARLY" in snapshot_text


def test_enrich_attendees_skips_sender_when_send_invites_false() -> None:
    """Verify send_invites=False never injects sender or Reply-To into slot.attendees."""
    merchant_msg = EmailMessage(
        message_id="m_ticket",
        thread_id="th_ticket",
        sender="Billetterie <salut@ticketing.example>",
        recipient="owner@example.com",
        subject="Confirmation d'achat : Billet",
        headers={"Reply-To": "feedback@ticketing.example"},
    )
    action = CalendarTriageAction(
        intent=CalendarActionIntent.CONFIRM_EVENT,
        slots=[
            CalendarSlotProposal(
                summary="Spectacle",
                start_iso="2026-12-28T15:00:00+01:00",
                end_iso="2026-12-28T16:30:00+01:00",
                attendees=[],
                send_invites=False,
            )
        ],
    )
    res = enrich_calendar_action_attendees(
        action, merchant_msg, frozenset({"owner@example.com"}), frozenset(), ["alias@example.com"]
    )
    assert res.slots[0].attendees == []


def test_enrich_attendees_excludes_automated_and_owner_when_send_invites_true() -> None:
    """Verify send_invites=True excludes automated addresses, owner, and never_invite."""
    human_msg = EmailMessage(
        message_id="m_human",
        thread_id="th_human",
        sender="Notifications <no-reply@example.com>",
        recipient="owner@example.com",
        subject="Point trimestriel",
        headers={
            "Reply-To": "feedback@example.com",
            "Cc": "bounces+123@example.com, partner@example.test, alias@example.com",
        },
    )
    action = CalendarTriageAction(
        intent=CalendarActionIntent.CONFIRM_EVENT,
        slots=[
            CalendarSlotProposal(
                summary="Point trimestriel",
                start_iso="2026-12-10T10:00:00+01:00",
                end_iso="2026-12-10T11:00:00+01:00",
                attendees=["noreply@example.com", "owner@example.com", "advisor@example.test"],
                send_invites=True,
            ),
            CalendarSlotProposal(
                summary="Suivi trimestriel",
                start_iso="2026-12-11T10:00:00+01:00",
                end_iso="2026-12-11T11:00:00+01:00",
                attendees=[],
                send_invites=True,
            ),
        ],
    )
    res = enrich_calendar_action_attendees(
        action,
        human_msg,
        frozenset({"owner@example.com"}),
        frozenset({"partner@example.test"}),
        ["alias@example.com"],
    )
    assert res.slots[0].attendees == ["advisor@example.test"]
    assert res.slots[1].attendees == ["partner@example.test"]
