"""Adversarial Red-Team tests for quoting, attendee leaks, false positives, & trace fallback."""

from __future__ import annotations

import importlib.util
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from alfred.controllers.app_factory import build_cli_controller
from alfred.domain.calendar_models import (
    CalendarActionIntent,
    CalendarSlotProposal,
    CalendarTriageAction,
)
from alfred.domain.models import AlfredPaths, AlfredSettings, EmailMessage
from alfred.repositories.state_store import StateStore
from alfred.services.agy_archive_store import format_annotations_pbtxt
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.contacts_service import ContactsService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_calendar_bridge import (
    enrich_calendar_action_attendees,
    render_calendar_confirm_gate_plan,
    render_draft_calendar_section,
)
from alfred.services.tasks_service import TasksService

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


class _Exec:
    """Minimal Google API execute() wrapper."""

    def __init__(self, payload: dict[str, Any]) -> None:
        """Store static response payload."""
        self._payload = payload

    def execute(self) -> dict[str, Any]:
        """Return stored payload."""
        return self._payload


class _MinimalCalClient:
    """Minimal fake Google Calendar v3 client for adversarial CLI failure tests."""

    def events(self) -> Any:
        """Return fake events resource."""
        return self

    def list(self, **_kwargs: Any) -> _Exec:
        """Return empty event list."""
        return _Exec({"items": []})

    def insert(self, **_kwargs: Any) -> _Exec:
        """Return inserted event id."""
        return _Exec({"id": "ev_adv_1"})


class _MinimalGmailClient:
    """Minimal fake Gmail v1 client."""

    def users(self) -> Any:
        """Return users resource."""
        return self

    def getProfile(self, **_kwargs: Any) -> _Exec:  # noqa: N802
        """Return mailbox profile."""
        return _Exec({"emailAddress": "owner@example.com", "historyId": "1"})

    def settings(self) -> Any:
        """Return settings resource."""
        return self

    def sendAs(self) -> Any:  # noqa: N802
        """Return sendAs resource."""
        return self

    def list(self, **_kwargs: Any) -> _Exec:
        """Return sendAs list."""
        return _Exec({"sendAs": [{"sendAsEmail": "owner@example.com", "isPrimary": True}]})


def _extract_cli_command(markdown_text: str, subcommand: str) -> list[str]:
    """Extract rendered alfred subcommand from Markdown bullet and split safely via shlex."""
    pattern = rf"- `(alfred {re.escape(subcommand)} [^`]+)`"
    match = re.search(pattern, markdown_text, flags=re.DOTALL)
    assert match is not None, f"Missing '{subcommand}' CLI command in:\n{markdown_text}"
    return shlex.split(match.group(1), posix=True)


def test_shell_injection_quoting_in_gate_and_draft_plans() -> None:
    """Malicious summary/location/description remain strictly shlex.quoted in Gate & Draft MD."""
    malicious_sum = 'Lyon $(touch /tmp/pwn) "test"\nL2'
    malicious_loc = "12 Rue '$(whoami)' ; rm -rf /"
    malicious_desc = 'Note "confidentielle" && curl https://evil.example\nSuite'

    slot = CalendarSlotProposal(
        summary=malicious_sum,
        start_iso="2026-12-28T15:00:00+01:00",
        end_iso="2026-12-28T16:30:00+01:00",
        attendees=[],
        send_invites=False,
        location=malicious_loc,
        description=malicious_desc,
    )
    action = CalendarTriageAction(intent=CalendarActionIntent.CONFIRM_EVENT, slots=[slot])
    msg = EmailMessage(
        message_id="m_inj",
        thread_id="th_inj",
        sender="salut@ticketing.example",
        subject="Confirmation d'achat",
    )

    gate_md = render_calendar_confirm_gate_plan(msg, "Confirmation", action, [])
    tokens_gate = _extract_cli_command(gate_md, "confirm-event")
    assert tokens_gate[tokens_gate.index("--summary") + 1] == malicious_sum
    assert tokens_gate[tokens_gate.index("--location") + 1] == malicious_loc
    assert tokens_gate[tokens_gate.index("--description") + 1] == malicious_desc
    assert ";" not in tokens_gate and "&&" not in tokens_gate

    draft_md = render_draft_calendar_section(action, "th_inj", "m_inj", frozenset())
    tokens_draft = _extract_cli_command(draft_md, "confirm-event")
    assert tokens_draft[tokens_draft.index("--summary") + 1] == malicious_sum
    assert tokens_draft[tokens_draft.index("--location") + 1] == malicious_loc
    assert tokens_draft[tokens_draft.index("--description") + 1] == malicious_desc


def test_pbtxt_escapes_malicious_title_newlines_and_quotes(tmp_path: Path) -> None:
    """Malicious title with newlines and quotes writes single-line escaped .pbtxt."""
    from alfred.services.specialist_trace_service import SpecialistTraceService

    malicious_title = 'Lyon $(touch /tmp/pwn) "test"\r\nL2'
    pbtxt_raw = format_annotations_pbtxt(malicious_title, archived=False)
    assert "\n" not in pbtxt_raw and "\r" not in pbtxt_raw
    assert '\\"test\\"' in pbtxt_raw and "archived:false" in pbtxt_raw

    paths = AlfredPaths.from_env(tmp_path)
    settings, store = AlfredSettings(), StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    cid = "eeeeeeee-2222-3333-4444-555555555555"

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Create synthetic sqlite db and return fixed UUID."""
        (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
        (agy_dir / "conversations" / f"{cid}.db").write_bytes(b"sqlite")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"conversation_id": cid}), "")

    agy_mgr = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=_runner,
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )
    SpecialistTraceService().surface_mutation_trace(
        agy_mgr, settings, "calendar", malicious_title, "- safe body", archive_trace_flag=True
    )
    written_pbtxt = (agy_dir / "annotations" / f"{cid}.pbtxt").read_text(encoding="utf-8")
    assert "\n" not in written_pbtxt and "\r" not in written_pbtxt
    assert '\\"test\\"' in written_pbtxt and "archived:false" in written_pbtxt


def test_adversarial_attendee_injection_blocks_owner_and_automated_when_invites_true() -> None:
    """Owner, never_invite, and noreply/bounces+ cannot leak when send_invites=True."""
    owners = frozenset({"owner@example.com"})
    allowlist = frozenset({"partner@example.test"})
    never_invite = ["alias@example.com"]
    poisoned_msg = EmailMessage(
        message_id="m_poison",
        thread_id="th_poison",
        sender="Automation <noreply@vendor.example>",
        recipient="owner@example.com",
        subject="Confirmation",
        headers={
            "Reply-To": "bounces+99@vendor.example",
            "Cc": "no-reply@vendor.example, feedback@vendor.example, advisor@example.test",
        },
    )
    action = CalendarTriageAction(
        intent=CalendarActionIntent.CONFIRM_EVENT,
        slots=[
            CalendarSlotProposal(
                summary="RDV",
                start_iso="2026-12-20T10:00:00+01:00",
                end_iso="2026-12-20T11:00:00+01:00",
                attendees=[
                    "owner@example.com",
                    "alias@example.com",
                    "noreply@example.com",
                    "bounces+42@example.com",
                    "mailer-daemon@example.com",
                ],
                send_invites=True,
            )
        ],
    )
    res = enrich_calendar_action_attendees(action, poisoned_msg, owners, allowlist, never_invite)
    assert res.slots[0].attendees == ["advisor@example.test"]


def test_adversarial_attendee_injection_blocks_vendor_fallback_when_invites_false() -> None:
    """Vendor sender, Reply-To, and Cc never leak into attendees when send_invites=False."""
    vendor_msg = EmailMessage(
        message_id="m_vendor",
        thread_id="th_vendor",
        sender="Conseiller Billetterie <salut@ticketing.example>",
        recipient="owner@example.com",
        subject="Confirmation d'achat : Billet",
        headers={"Reply-To": "support@ticketing.example", "Cc": "bounces+1@ticketing.example"},
    )
    action = CalendarTriageAction(
        intent=CalendarActionIntent.CONFIRM_EVENT,
        slots=[
            CalendarSlotProposal(
                summary="Concert",
                start_iso="2026-12-28T20:00:00+01:00",
                end_iso="2026-12-28T22:00:00+01:00",
                attendees=[],
                send_invites=False,
            )
        ],
    )
    res = enrich_calendar_action_attendees(
        action, vendor_msg, frozenset({"owner@example.com"}), frozenset(), ["alias@example.com"]
    )
    assert res.slots[0].attendees == []


def test_adversarial_false_positives_for_has_dated_booking_signal() -> None:
    """Promos with deadlines, dated newsletters, and payment receipts must return False."""
    from alfred.services.inbox_booking_detector import has_dated_booking_signal

    adversarial_negatives = [
        EmailMessage(
            message_id="adv_promo",
            thread_id="th_adv_1",
            sender="Offres <promo@ticketing.example>",
            subject="Offre Flash : -40% sur vos billets Jusqu'au 28 déc. à 23h59",
            body_text="Réservez vite avant le 28/12/2026 à 23h59 avec le code PROMO40.",
            headers={"List-Unsubscribe": "<https://ticketing.example/unsub>"},
            label_ids=["INBOX", "CATEGORY_PROMOTIONS"],
        ),
        EmailMessage(
            message_id="adv_news",
            thread_id="th_adv_2",
            sender="Revue <news@media.example>",
            subject="Newsletter du 22/11/2026 — Agenda des expositions du 15 décembre",
            body_text="Découvrez nos conférences à venir. Se désabonner en un clic.",
            headers={"List-Unsubscribe": "<https://media.example/unsub>"},
            label_ids=["INBOX"],
        ),
        EmailMessage(
            message_id="adv_payment",
            thread_id="th_adv_3",
            sender="Service Paiement Express <noreply@paiement.example>",
            subject="Reçu pour votre paiement du 22/11/2026 à 14h32",
            body_text="Transaction #TX-9042 horodatée le 22/11/2026 à 14h32 : 89,00 €.",
            label_ids=["INBOX"],
        ),
        EmailMessage(
            message_id="adv_sub_confirm",
            thread_id="th_adv_4",
            sender="Presse <no-reply@media.example>",
            subject="Confirmation de votre inscription newsletter jusqu'au 28 déc. à 23h59",
            body_text="Merci pour votre inscription à notre lettre hebdomadaire.",
            headers={"List-Unsubscribe": "<https://media.example/unsub>"},
            label_ids=["INBOX"],
        ),
    ]
    for msg in adversarial_negatives:
        assert has_dated_booking_signal(msg) is False, f"False positive on {msg.message_id}"


@pytest.mark.parametrize(
    "runner_factory",
    [
        lambda: (
            lambda cmd: subprocess.CompletedProcess(
                cmd, 0, json.dumps({"conversation_id": "../escape"}), ""
            )
        ),
        lambda: (
            lambda cmd: subprocess.CompletedProcess(
                cmd, 0, json.dumps({"conversation_id": "..\\escape"}), ""
            )
        ),
        lambda: (lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "agy crashed")),
        lambda: (lambda cmd: subprocess.CompletedProcess(cmd, 0, "{{{invalid-json", "")),
    ],
)
def test_surface_mutation_trace_path_traversal_and_agy_errors_fallback_safely(
    tmp_path: Path,
    runner_factory: Callable[[], Callable[[list[str]], subprocess.CompletedProcess[str]]],
) -> None:
    """Path traversal IDs or agy errors fall back safely to synthetic agy_alfred_calendar_* ID."""
    from alfred.services.specialist_trace_service import SpecialistTraceService

    paths = AlfredPaths.from_env(tmp_path)
    settings, store = AlfredSettings(), StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    agy_dir.mkdir(parents=True, exist_ok=True)
    agy_mgr = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=runner_factory(),
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )

    rec = SpecialistTraceService().surface_mutation_trace(
        agy_mgr, settings, "calendar", "[EVENT] Fallback Test", "- details", archive_trace_flag=True
    )
    assert rec is not None
    assert rec.session_id.startswith("agy_alfred_calendar_")
    assert rec.status == "EXECUTED" and rec.workspace == "alfred-calendar"
    assert rec.session_id in store.conversations.load().sessions
    assert not (tmp_path / "escape.pbtxt").exists() and not (agy_dir / "escape.pbtxt").exists()


@pytest.mark.parametrize(
    "runner_factory",
    [
        lambda: (
            lambda cmd: subprocess.CompletedProcess(
                cmd, 0, json.dumps({"conversation_id": "../escape"}), ""
            )
        ),
        lambda: (lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "agy crashed")),
    ],
)
def test_cli_parent_command_returns_zero_on_agy_trace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner_factory: Callable[[], Callable[[list[str]], subprocess.CompletedProcess[str]]],
) -> None:
    """Parent CLI mutation command still returns 0 when agy trace fails or returns bad ID."""
    monkeypatch.chdir(WORKTREE_ROOT / "core" / "workspaces" / "alfred-life")
    paths = AlfredPaths.from_env(tmp_path)
    settings, store = AlfredSettings(), StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    agy_dir.mkdir(parents=True, exist_ok=True)
    agy_mgr = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=runner_factory(),
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )
    ctrl = build_cli_controller(
        paths=paths,
        settings=settings,
        state_store=store,
        calendar_service=CalendarService(
            paths, settings, calendar_client=_MinimalCalClient(), state_store=store
        ),
        tasks_service=TasksService(paths, settings),
        contacts_service=ContactsService(paths, settings),
        gmail_service=GmailService(paths, settings, gmail_client=_MinimalGmailClient()),
        agy_manager=agy_mgr,
    )
    rc = ctrl.run(
        [
            "create-event",
            "--summary",
            "Test Résilience",
            "--from",
            "2026-12-15T10:00:00+01:00",
            "--to",
            "2026-12-15T11:00:00+01:00",
        ]
    )
    assert rc == 0
    sessions = list(store.conversations.load().sessions.values())
    assert len(sessions) == 1
    assert sessions[0].session_id.startswith("agy_alfred_calendar_")
    assert sessions[0].status == "EXECUTED"


def test_security_hook_allows_single_quoted_newline_semicolon_pipe_and_denies_escapes() -> None:
    """PreToolUse allows single-quoted confirm-event values while denying unquoted/double-quoted."""
    hook_path = WORKTREE_ROOT / "core" / "scripts" / "security_pre_tool_hook.py"
    spec = importlib.util.spec_from_file_location("wt_security_hook_adv", hook_path)
    assert spec is not None and spec.loader is not None
    hook_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook_mod)

    inbox_cwd = str(WORKTREE_ROOT / "core" / "workspaces" / "alfred-inbox")
    valid_cmd = (
        "alfred confirm-event --calendar primary --summary 'Expérience VR' "
        "--from 2026-12-28T15:00:00+01:00 --to 2026-12-28T16:30:00+01:00 "
        "--location '12 Rue X\nLyon' --description 'Billet #1 ; Groupe | VIP' "
        "--no-send-invites --archive-trace"
    )
    verdict = hook_mod.evaluate_pre_tool_event(
        {"hook_event_name": "PreToolUse", "tool_name": "run_command", "cwd": inbox_cwd,
         "tool_input": {"CommandLine": valid_cmd}}
    )
    assert verdict["decision"] == "allow"

    proc = subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps({"tool_name": "run_command", "cwd": inbox_cwd,
                          "tool_input": {"CommandLine": valid_cmd}}),
        text=True, capture_output=True, check=False,
    )
    assert proc.returncode == 0 and json.loads(proc.stdout)["decision"] == "allow"

    for bad_cmd in (
        "alfred confirm-event --summary a ; id",
        "alfred confirm-event --summary a | cat",
        "alfred confirm-event --summary a && id",
        "alfred confirm-event --summary a || id",
        "alfred confirm-event --summary a\nid",
        "alfred confirm-event --summary a > /tmp/out",
        "alfred confirm-event --summary a < /etc/passwd",
        'alfred confirm-event --summary "$(id)"',
        'alfred confirm-event --summary "`id`"',
        'alfred confirm-event --summary "a ; b"',
        'alfred confirm-event --summary "a\nb"',
    ):
        denied = hook_mod.evaluate_pre_tool_event(
            {"tool_name": "run_command", "cwd": inbox_cwd, "tool_input": {"CommandLine": bad_cmd}}
        )
        assert denied["decision"] == "deny", f"Expected deny for: {bad_cmd!r}"


def test_null_byte_conversation_id_rejected_by_parser_and_trace_service() -> None:
    """Null bytes in conversation_id are rejected by extract_conversation_id & _is_safe_conv_id."""
    from alfred.services.agy_triage_parser import extract_conversation_id
    from alfred.services.specialist_trace_service import _is_safe_conv_id

    assert extract_conversation_id({"conversation_id": "abc\x00def"}) is None
    assert _is_safe_conv_id("abc\x00def") is False
