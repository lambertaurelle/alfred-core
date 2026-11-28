"""E2E tests for Carbon/Neon settings, conflict prompts, manifests, and PDF schedule triage."""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from alfred.controllers.app_factory import build_cli_controller
from alfred.controllers.cli_controller import CliController
from alfred.domain.feature_models import FeatureFlagsConfig, InboxFilterConfig
from alfred.domain.models import AlfredPaths, AlfredSettings, EmailMessage
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.agy_triage_parser import build_cognitive_triage_prompt
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


def _b64u(raw: bytes) -> str:
    """Encode raw bytes to unpadded URL-safe base64 ASCII."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _box(payload: Any) -> SimpleNamespace:
    """Wrap an API response object behind execute()."""
    return SimpleNamespace(execute=lambda: payload)


def _build_schedule_grid_pdf() -> bytes:
    """Build a 1-page multi-column PDF schedule with two horizontal cells on row y=700."""
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


class _E2EGmailScheduleClient:
    """In-memory Gmail v1 client serving a bulk provider message with a PDF attachment."""

    def __init__(self, msg_id: str, raw_msg: dict[str, Any], pdf_bytes: bytes) -> None:
        """Store message payload, attachment bytes, and draft/star ledgers."""
        self.history_id = "200"
        self.history_events: list[dict[str, Any]] = []
        self.messages = {msg_id: raw_msg}
        self.attachments = {"att-sched": pdf_bytes}
        self.draft_creations: list[dict[str, Any]] = []
        self.starred_ids: list[str] = []

    def _modify(self, *, id: str, body: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        """Record STARRED additions."""
        if "STARRED" in body.get("addLabelIds", []):
            self.starred_ids.append(id)
        return _box({"id": id})

    def _create_draft(self, *, body: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        """Record draft creation attempts."""
        self.draft_creations.append(body)
        return _box({"id": f"draft_{len(self.draft_creations)}"})

    def users(self) -> SimpleNamespace:
        """Return simulated Gmail v1 users resource."""
        return SimpleNamespace(
            getProfile=lambda **_kw: _box(
                {"emailAddress": "owner@example.com", "historyId": self.history_id}
            ),
            settings=lambda: SimpleNamespace(
                sendAs=lambda: SimpleNamespace(list=lambda **_kw: _box({}))
            ),
            history=lambda: SimpleNamespace(
                list=lambda **_kw: _box(
                    {"historyId": self.history_id, "history": list(self.history_events)}
                )
            ),
            messages=lambda: SimpleNamespace(
                get=lambda *, id, **_kw: _box(self.messages[id]),
                modify=self._modify,
                attachments=lambda: SimpleNamespace(
                    get=lambda *, id, **_kw: _box(
                        {
                            "data": _b64u(self.attachments.get(id, b"")),
                            "size": len(self.attachments.get(id, b"")),
                        }
                    )
                ),
            ),
            drafts=lambda: SimpleNamespace(create=self._create_draft),
        )


def test_e2e_settings_examples_declare_carbon_neon_and_priority_allowlists() -> None:
    """Verify both settings.example.yaml files declare Carbon/Neon defaults and allowlists."""
    example_paths = (
        WORKTREE_ROOT / "core" / "config" / "settings.example.yaml",
        WORKTREE_ROOT / "instance" / "config" / "settings.example.yaml",
    )
    for yaml_path in example_paths:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict), f"Invalid YAML in {yaml_path}"
        models = raw["models"]
        assert models["orchestrator_model"] == "gemini-pro-orchestrator"
        assert models["light_model"] == "gemini-pro-orchestrator-neon"
        assert models["light_effort"] == ""
        assert models["trace_model"] == "gemini-pro-orchestrator-neon"
        assert models["trace_effort"] == ""

        inbox = raw["inbox"]
        assert inbox["allowlist_senders"] == []
        assert inbox["allowlist_domains"] == []


def test_e2e_cognitive_triage_prompt_renders_neon_carbon_and_conflict_rules() -> None:
    """Verify build_cognitive_triage_prompt references Neon/Carbon and calendar conflict checks."""
    msg = EmailMessage(
        message_id="m_prompt_1",
        thread_id="th_prompt_1",
        sender="Colleague <colleague@partner.example>",
        recipient="owner@example.com",
        subject="Proposed meeting Tuesday 14:00",
        body_text="Can we confirm Tuesday at 14:00?",
    )
    rendered = build_cognitive_triage_prompt(
        msg=msg,
        docs=[],
        user_addresses=frozenset({"owner@example.com"}),
        calendar_context="2026-12-22 (All-day): Family Vacation",
        paths=AlfredPaths.from_env(WORKTREE_ROOT),
    )
    assert "Orchestrator (Neon)" in rendered
    assert "Orchestrator (Carbon)" in rendered or "Orchestrator Carbon" in rendered
    assert "hydrogen" not in rendered.lower()
    assert "CONFIRM_EVENT" in rendered and "CREATE_HOLD" in rendered
    lowered = rendered.lower()
    assert "all-day" in lowered
    assert "overlap" in lowered or "conflict" in lowered
    assert "reason" in rendered and "research_brief" in rendered


def test_e2e_templates_manifests_and_docs_purge_hydrogen_for_neon() -> None:
    """Verify prompt templates, plan-executor, root docs, and workspace AGENTS.md use Neon."""
    operational_ws = (
        "alfred-onboard",
        "alfred-life",
        "alfred-inbox",
        "alfred-calendar",
        "alfred-tasks",
        "alfred-drive",
    )
    ws_manifests = [
        WORKTREE_ROOT / "core" / "workspaces" / ws / "AGENTS.md" for ws in operational_ws
    ]
    checked_files = [
        WORKTREE_ROOT / "core" / "templates" / "prompts" / "inbox_gate_simple.md",
        WORKTREE_ROOT / "core" / "templates" / "prompts" / "inbox_gate_complex.md",
        WORKTREE_ROOT / ".agents" / "agents" / "plan-executor.md",
        WORKTREE_ROOT / "AGENTS.md",
        WORKTREE_ROOT / "README.md",
        *ws_manifests,
        WORKTREE_ROOT / "core" / "workspaces" / "alfred-evolve" / "AGENTS.md",
    ]
    for path in checked_files:
        low = path.read_text(encoding="utf-8").lower()
        assert "gemini-4-hydrogen" not in low, f"Legacy model ID remains in {path}"
        assert "hydrogen" not in low, f"Legacy Hydrogen reference remains in {path}"
        assert "model: flash" not in low, f"Legacy 'model: flash' remains in {path}"

    must_mention_neon = [
        WORKTREE_ROOT / "core" / "templates" / "prompts" / "inbox_gate_simple.md",
        WORKTREE_ROOT / "core" / "templates" / "prompts" / "inbox_gate_complex.md",
        WORKTREE_ROOT / ".agents" / "agents" / "plan-executor.md",
        WORKTREE_ROOT / "AGENTS.md",
        WORKTREE_ROOT / "README.md",
        *ws_manifests,
    ]
    for path in must_mention_neon:
        text = path.read_text(encoding="utf-8")
        assert (
            "Orchestrator (Neon)" in text or "gemini-pro-orchestrator-neon" in text
        ), f"Expected Neon routing reference in {path}"


def _build_schedule_raw_payload(pdf_bytes: bytes) -> dict[str, Any]:
    """Construct raw Gmail message payload from contact@provider.example with PDF attachment."""
    return {
        "id": "m_sched_e2e",
        "threadId": "th_sched_e2e",
        "labelIds": ["INBOX", "CATEGORY_FORUMS"],
        "snippet": "Updated schedule attached.",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Provider Desk <contact@provider.example>"},
                {"name": "To", "value": "owner@example.com"},
                {"name": "Subject", "value": "Updated Weekly Schedule"},
                {"name": "List-Unsubscribe", "value": "<mailto:unsub@provider.example>"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64u(b"Schedule attached.")}},
                {
                    "mimeType": "application/pdf",
                    "filename": "schedule.pdf",
                    "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                    "body": {"attachmentId": "att-sched", "size": len(pdf_bytes)},
                },
            ],
        },
    }


def _make_schedule_agy_runner(captured_prompts: list[str]) -> Any:
    """Create a fake agy runner capturing prompts and emitting household CONFIRM_EVENT JSON."""

    def _agy_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Capture cognitive triage prompt and emit USER_ACTION_REQUIRED + CONFIRM_EVENT."""
        if "--print" in cmd:
            captured_prompts.append(cmd[cmd.index("--print") + 1])
        out = {
            "conversation_id": "conv-sched-e2e",
            "structured_output": {
                "output_type": "USER_ACTION_REQUIRED",
                "reason": "Confirm Tuesday Piano slot from attached schedule.",
                "research_brief": ["Verify Piano slot at 14:00 against household calendar."],
                "calendar_action": {
                    "intent": "CONFIRM_EVENT",
                    "slots": [
                        {
                            "summary": "Piano Lesson",
                            "start_iso": "2026-12-15T14:00:00+01:00",
                            "end_iso": "2026-12-15T15:00:00+01:00",
                            "attendees": ["alice@example.com"],
                            "send_invites": True,
                        }
                    ],
                },
            },
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    return _agy_runner


def _build_schedule_e2e_controller(
    tmp_path: Path, captured_prompts: list[str]
) -> tuple[CliController, _E2EGmailScheduleClient]:
    """Wire CliController with allowlisted contact@provider.example and multi-column PDF."""
    paths = AlfredPaths.from_env(tmp_path)
    shutil.copytree(WORKTREE_ROOT / "core" / "templates", paths.core_dir / "templates")
    pdf_bytes = _build_schedule_grid_pdf()
    raw_msg = _build_schedule_raw_payload(pdf_bytes)
    cfg = AlfredSettings(
        features=FeatureFlagsConfig(
            inbox_auto_triage=True, gmail_drafts_and_send=True,
            calendar_management=True, drive_knowledge_sync=False,
        ),
        inbox_filters=InboxFilterConfig.from_dict(
            {"allowlist_senders": ["contact@provider.example"]}
        ),
        agy_sync_daemon_on_changes=False,
    )
    store = StateStore(paths)
    gm_cli = _E2EGmailScheduleClient("m_sched_e2e", raw_msg, pdf_bytes)
    gmail = GmailService(paths, cfg, gmail_client=gm_cli)
    agy = AgySessionManager(
        paths, cfg, store, runner_fn=_make_schedule_agy_runner(captured_prompts),
        agy_locator_fn=lambda: "/usr/bin/agy", antigravity_data_dir=tmp_path / "agy_data",
    )
    ctrl = build_cli_controller(
        paths=paths, settings=cfg, state_store=store,
        drive_service=DriveService(paths, cfg, store), gmail_service=gmail, agy_manager=agy,
    )
    return ctrl, gm_cli


def test_e2e_allowlisted_bulk_pdf_schedule_triage_and_household_attendee_isolation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify E2E poll-inbox extracts PDF layout, keeps alice@example.com, and skips provider."""
    captured_prompts: list[str] = []
    ctrl, gm_cli = _build_schedule_e2e_controller(tmp_path, captured_prompts)

    assert ctrl.run(["poll-inbox", "--cognitive-agy"]) == 0
    _ = capsys.readouterr()
    gm_cli.history_id = "201"
    gm_cli.history_events.append(
        {
            "messagesAdded": [
                {"message": {"id": "m_sched_e2e", "labelIds": ["INBOX", "CATEGORY_FORUMS"]}}
            ]
        }
    )
    assert ctrl.run(["poll-inbox", "--cognitive-agy"]) == 0
    decisions = json.loads(capsys.readouterr().out)

    assert len(decisions) == 1
    dec = decisions[0]
    assert dec["output_type"] == "USER_ACTION_REQUIRED"
    assert dec["draft_id"] is None and gm_cli.draft_creations == []

    assert len(captured_prompts) == 1
    prompt_lines = captured_prompts[0].splitlines()
    assert any("Mon 09:00 Yoga" in ln and "Tue 14:00 Piano" in ln for ln in prompt_lines)

    plan_md = str(dec["implementation_plan_markdown"])
    assert "alfred confirm-event" in plan_md
    assert "--attendee alice@example.com" in plan_md
    confirm_lines = [ln for ln in plan_md.splitlines() if "alfred confirm-event" in ln]
    assert confirm_lines and "contact@provider.example" not in confirm_lines[0]
