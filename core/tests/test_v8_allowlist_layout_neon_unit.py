"""Unit tests for Carbon/Neon routing, timeouts, priority allowlists, PDF layout, & attendees."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from alfred.domain.calendar_models import (
    CalendarActionIntent,
    CalendarSlotProposal,
    CalendarTriageAction,
)
from alfred.domain.feature_models import InboxFilterConfig, ModelRoutingConfig
from alfred.domain.models import DEFAULT_WORKSPACES, AlfredPaths, AlfredSettings, EmailMessage
from alfred.repositories.state_store import StateStore
from alfred.services import attachment_extractor
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.attachment_extractor import extract_attachment_text
from alfred.services.delegation_service import DelegationService
from alfred.services.inbox_calendar_bridge import (
    enrich_calendar_action_attendees,
    extract_correspondent_emails,
)


def _build_two_column_schedule_pdf() -> bytes:
    """Create a 1-page PDF with two horizontal columns at y=700 and a lower row at y=640."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    content = (
        "BT /F1 12 Tf 1 0 0 1 50 700 Tm (Mon 09:00 Yoga) Tj ET\n"
        "BT /F1 12 Tf 1 0 0 1 50 640 Tm (Mon 18:00 Swim) Tj ET\n"
        "BT /F1 12 Tf 1 0 0 1 260 700 Tm (Tue 14:00 Piano) Tj ET\n"
    )
    stream = DecodedStreamObject()
    stream.set_data(content.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    font_obj = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    fonts = DictionaryObject({NameObject("/F1"): writer._add_object(font_obj)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_model_routing_config_defaults_and_from_dict() -> None:
    """Verify Carbon/Neon default fields and empty effort strings on ModelRoutingConfig."""
    candidates = (
        ModelRoutingConfig(),
        ModelRoutingConfig.from_dict({}),
        ModelRoutingConfig.from_dict(None),
    )
    for cfg in candidates:
        assert cfg.orchestrator_model == "gemini-pro-orchestrator"
        assert cfg.light_model == "gemini-pro-orchestrator-neon"
        assert cfg.light_effort == ""
        assert cfg.trace_model == "gemini-pro-orchestrator-neon"
        assert cfg.trace_effort == ""


def test_model_routing_is_brain_workspace_and_resolution(tmp_path: Path) -> None:
    """Verify is_brain_workspace, muscle Neon routing without --effort, and force_orchestrator."""
    cfg = ModelRoutingConfig()
    brain_roles = (
        "onboard", "life", "evolve",
        "alfred-onboard", "alfred-life", "alfred-evolve",
        "  ALFRED-LIFE  ", " OnBoard ",
    )
    for role in brain_roles:
        assert cfg.is_brain_workspace(role) is True
        assert cfg.resolve_for_workspace(role) == ("gemini-pro-orchestrator", None)

    muscle_roles = (
        "inbox", "calendar", "tasks", "drive",
        "alfred-inbox", "alfred-calendar", "alfred-tasks", "alfred-drive",
        "  ALFRED-INBOX  ",
    )
    for role in muscle_roles:
        assert cfg.is_brain_workspace(role) is False
        assert cfg.resolve_for_workspace(role) == ("gemini-pro-orchestrator-neon", None)

    assert cfg.resolve_for_workspace("alfred-inbox", force_orchestrator=True) == (
        "gemini-pro-orchestrator",
        None,
    )
    assert cfg.resolve_trace() == ("gemini-pro-orchestrator-neon", None)

    paths = AlfredPaths.from_env(tmp_path)
    agy = AgySessionManager(
        paths, AlfredSettings(), StateStore(paths), antigravity_data_dir=tmp_path / "agy"
    )
    assert agy.build_model_cli_flags("alfred-inbox") == ["--model", "gemini-pro-orchestrator-neon"]
    assert agy.build_model_cli_flags("alfred-inbox", force_orchestrator=True) == [
        "--model",
        "gemini-pro-orchestrator",
    ]
    assert agy.build_model_cli_flags("alfred-inbox", trace=True) == [
        "--model",
        "gemini-pro-orchestrator-neon",
    ]


def test_model_routing_custom_yaml_non_regression(tmp_path: Path) -> None:
    """Verify custom non-orchestrator YAML models preserve --effort unless force_orchestrator."""
    custom = ModelRoutingConfig.from_dict(
        {
            "light_model": "custom-non-orch-v1",
            "light_effort": "medium",
            "trace_model": "custom-trace-v1",
            "trace_effort": "low",
        }
    )
    assert custom.resolve_for_workspace("alfred-inbox") == ("custom-non-orch-v1", "medium")
    assert custom.resolve_trace() == ("custom-trace-v1", "low")
    assert custom.resolve_for_workspace("alfred-inbox", force_orchestrator=True) == (
        "gemini-pro-orchestrator",
        None,
    )
    assert custom.resolve_for_workspace("alfred-life") == ("gemini-pro-orchestrator", None)

    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings(models=custom)
    agy = AgySessionManager(
        paths, settings, StateStore(paths), antigravity_data_dir=tmp_path / "agy"
    )
    assert agy.build_model_cli_flags("alfred-inbox") == [
        "--model",
        "custom-non-orch-v1",
        "--effort",
        "medium",
    ]
    assert agy.build_model_cli_flags("alfred-inbox", force_orchestrator=True) == [
        "--model",
        "gemini-pro-orchestrator",
    ]
    assert agy.build_model_cli_flags("alfred-inbox", trace=True) == [
        "--model",
        "custom-trace-v1",
        "--effort",
        "low",
    ]


def test_spawn_timeouts_brain_vs_neon_muscle_and_custom_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify 600s Brain vs 180s Neon Muscle timeouts on DelegationService and AgySessionManager."""
    paths = AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)
    dlg = DelegationService(paths=paths, settings=AlfredSettings(), state_store=store)

    for brain_target in ("alfred-life", "evolve", "onboard", "alfred-evolve", "alfred-onboard"):
        assert dlg.resolve_spawn_timeout(brain_target) == 600
    for muscle_target in ("alfred-inbox", "alfred-calendar", "alfred-tasks", "alfred-drive"):
        assert dlg.resolve_spawn_timeout(muscle_target) == 180

    custom_ws = {**DEFAULT_WORKSPACES, "life": "custom-life", "inbox": "custom-inbox"}
    custom_settings = AlfredSettings(workspaces=custom_ws, agy_sync_daemon_on_changes=False)
    custom_dlg = DelegationService(paths=paths, settings=custom_settings, state_store=store)
    assert custom_dlg.resolve_spawn_timeout("custom-life") == 600
    assert custom_dlg.resolve_spawn_timeout("custom-inbox") == 180

    recorded: list[tuple[list[str], int]] = []
    agy = AgySessionManager(
        paths,
        custom_settings,
        store,
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=tmp_path / "agy_data",
    )

    def _spy_run(
        cmd: list[str], cwd: Path | None = None, timeout: int = 180
    ) -> subprocess.CompletedProcess[str]:
        """Record spawned CLI command and timeout, returning a synthetic conversation UUID."""
        _ = cwd
        recorded.append((list(cmd), timeout))
        cid = f"11111111-2222-3333-4444-{len(recorded):012d}"
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"conversation_id": cid}), "")

    monkeypatch.setattr(agy, "_run_agy_command", _spy_run)
    agy.create_session("alfred-inbox", "m1", "# Plan", spawn_live=True)
    agy.create_session("alfred-inbox", "m2", "# Plan", spawn_live=True, force_orchestrator=True)
    agy.create_session("custom-life", "m3", "# Plan", spawn_live=True)
    agy.create_session("custom-inbox", "m4", "# Plan", spawn_live=True)

    assert recorded[0][1] == 180 and "gemini-pro-orchestrator-neon" in recorded[0][0]
    assert recorded[1][1] == 600 and "gemini-pro-orchestrator" in recorded[1][0]
    assert recorded[2][1] == 600 and "gemini-pro-orchestrator" in recorded[2][0]
    assert recorded[3][1] == 180 and "gemini-pro-orchestrator-neon" in recorded[3][0]


def test_priority_allowlist_normalization_and_sender_matching() -> None:
    """Verify InboxFilterConfig priority allowlist normalization and subdomain matching."""
    from alfred.services.inbox_eligibility import is_allowlisted_priority_sender

    empty_cfg = InboxFilterConfig()
    assert empty_cfg.allowlist_senders == []
    assert empty_cfg.allowlist_domains == []
    assert is_allowlisted_priority_sender("scheduler@vendor.example", empty_cfg) is False

    cfg = InboxFilterConfig.from_dict(
        {
            "allowlist_senders": ["Scheduler <Scheduler@Vendor.Example>", "   "],
            "allowlist_domains": ["@Portal.Example", ".Sub.Example", ""],
        }
    )
    assert cfg.allowlist_senders == ["scheduler@vendor.example"]
    assert cfg.allowlist_domains == ["portal.example", "sub.example"]

    assert is_allowlisted_priority_sender("Vendor <scheduler@vendor.example>", cfg) is True
    assert is_allowlisted_priority_sender("info@portal.example", cfg) is True
    assert is_allowlisted_priority_sender("news@sub.portal.example", cfg) is True
    assert is_allowlisted_priority_sender("alert@deep.sub.example", cfg) is True
    assert is_allowlisted_priority_sender("stranger@other.example", cfg) is False
    assert is_allowlisted_priority_sender("", cfg) is False


def test_pdf_layout_extraction_preserves_horizontal_columns_and_normalizes_whitespace() -> None:
    """Verify PDF extraction preserves multi-column alignment and collapses blank lines."""
    pdf_bytes = _build_two_column_schedule_pdf()
    method, text, truncated = extract_attachment_text(pdf_bytes, "schedule.pdf", "application/pdf")

    assert method == "pypdf_native_text"
    assert truncated is False
    lines = text.splitlines()
    assert any("Mon 09:00 Yoga" in ln and "Tue 14:00 Piano" in ln for ln in lines)
    assert text.index("Tue 14:00 Piano") < text.index("Mon 18:00 Swim")
    assert "\n\n\n" not in text
    assert all(ln == ln.rstrip(" \t\r") for ln in lines)


def test_pdf_layout_extraction_fallback_when_layout_mode_raises_or_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify _extract_pdf_text falls back cleanly when layout mode raises or is blank."""

    class _StubPageRaise:
        """Page stub raising TypeError when extraction_mode is passed."""

        def extract_text(self, **kwargs: Any) -> str:
            """Raise TypeError on layout kwargs and return raw text on plain call."""
            if kwargs:
                raise TypeError("unexpected keyword argument 'extraction_mode'")
            return "Fallback text   \n\n\n\nLine 2   "

    class _StubPageEmptyLayout:
        """Page stub returning whitespace in layout mode and text in plain mode."""

        def extract_text(self, **kwargs: Any) -> str:
            """Return blank whitespace on layout mode and valid text on plain call."""
            if kwargs.get("extraction_mode") == "layout":
                return "   \n  "
            return "Plain fallback\n\n\n\nRow 2   "

    class _StubReader:
        """Reader wrapper exposing injected page stubs."""

        def __init__(self, pages: list[Any]) -> None:
            """Store stub pages list."""
            self.pages = pages

    monkeypatch.setattr(
        attachment_extractor, "PdfReader", lambda _s: _StubReader([_StubPageRaise()])
    )
    assert extract_attachment_text(b"%PDF-stub", "a.pdf", "application/pdf") == (
        "pypdf_native_text",
        "Fallback text\n\nLine 2",
        False,
    )

    monkeypatch.setattr(
        attachment_extractor, "PdfReader", lambda _s: _StubReader([_StubPageEmptyLayout()])
    )
    assert extract_attachment_text(b"%PDF-stub", "b.pdf", "application/pdf") == (
        "pypdf_native_text",
        "Plain fallback\n\nRow 2",
        False,
    )


def test_calendar_bulk_sender_attendee_guard_suppresses_fallback_guests() -> None:
    """Verify bulk headers without distinct human Reply-To yield empty fallback attendees."""
    owners = frozenset({"owner@example.com"})
    never_invite = ["alias@example.com"]
    empty_slot = CalendarSlotProposal(
        summary="Session",
        start_iso="2026-12-15T09:00:00+01:00",
        end_iso="2026-12-15T10:00:00+01:00",
        attendees=[],
        send_invites=True,
    )
    for hdrs in (
        {"List-Unsubscribe": "<mailto:unsub@provider.example>"},
        {"Precedence": "bulk", "Reply-To": "contact@provider.example"},
    ):
        bulk_msg = EmailMessage(
            message_id="m_bulk",
            thread_id="th_bulk",
            sender="Provider <contact@provider.example>",
            recipient="owner@example.com",
            subject="Schedule confirmation",
            headers=hdrs,
        )
        assert extract_correspondent_emails(bulk_msg, owners, frozenset()) == []
        act = CalendarTriageAction(intent=CalendarActionIntent.CONFIRM_EVENT, slots=[empty_slot])
        enriched = enrich_calendar_action_attendees(
            act, bulk_msg, owners, frozenset(), never_invite
        )
        assert enriched.slots[0].attendees == []


def test_calendar_explicit_attendees_isolation_and_human_one_on_one_non_regression() -> None:
    """Verify explicit slot.attendees ignore header fallback guests while 1:1 empty slots enrich."""
    owners = frozenset({"owner@example.com"})
    never_invite = ["alias@example.com"]
    base_slot = CalendarSlotProposal(
        summary="Project Sync",
        start_iso="2026-12-15T09:00:00+01:00",
        end_iso="2026-12-15T10:00:00+01:00",
        attendees=[],
        send_invites=True,
    )
    human_msg = EmailMessage(
        message_id="m_1on1",
        thread_id="th_1on1",
        sender="Colleague <colleague@partner.example>",
        recipient="owner@example.com",
        subject="Project sync",
        headers={"Cc": "alias@example.com, extra@partner.example"},
    )
    explicit_slot = base_slot.model_copy(
        update={"attendees": ["alice@example.com", "bob@household.example"]}
    )
    explicit_act = CalendarTriageAction(
        intent=CalendarActionIntent.CONFIRM_EVENT, slots=[explicit_slot]
    )
    res_exp = enrich_calendar_action_attendees(
        explicit_act, human_msg, owners, frozenset(), never_invite
    )
    assert res_exp.slots[0].attendees == ["alice@example.com", "bob@household.example"]

    for intent in (CalendarActionIntent.CREATE_HOLD, CalendarActionIntent.CONFIRM_EVENT):
        one_on_one = human_msg.model_copy(
            update={"headers": {"Cc": "owner@example.com, alias@example.com"}}
        )
        act_h = CalendarTriageAction(intent=intent, slots=[base_slot])
        res_h = enrich_calendar_action_attendees(
            act_h, one_on_one, owners, frozenset(), never_invite
        )
        assert res_h.slots[0].attendees == ["colleague@partner.example"]

    only_blocked = base_slot.model_copy(
        update={"attendees": ["owner@example.com", "noreply@partner.example"]}
    )
    act_fb = CalendarTriageAction(intent=CalendarActionIntent.CONFIRM_EVENT, slots=[only_blocked])
    res_fb = enrich_calendar_action_attendees(act_fb, human_msg, owners, frozenset(), never_invite)
    assert res_fb.slots[0].attendees == ["colleague@partner.example", "extra@partner.example"]
