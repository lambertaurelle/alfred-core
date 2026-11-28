"""Functional and non-regression tests for priority allowlists and force_orchestrator routing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alfred.domain.feature_models import FeatureFlagsConfig, InboxFilterConfig
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    EmailMessage,
    InboxOutputType,
    KnowledgeDocument,
    fold_accents,
)
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_booking_detector import is_passive_or_automated_email
from alfred.services.inbox_eligibility import InboxEligibilityPolicy
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService


def _box(payload: Any) -> SimpleNamespace:
    """Wrap an in-memory API response behind an execute() method."""
    return SimpleNamespace(execute=lambda: payload)


class _MinimalGmailClient:
    """Lightweight in-memory Gmail v1 client recording stars and created drafts."""

    def __init__(self) -> None:
        """Initialize empty star and draft audit lists."""
        self.starred_ids: list[str] = []
        self.draft_creations: list[dict[str, Any]] = []

    def _modify(self, *, id: str, body: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        """Record STARRED label additions."""
        if "STARRED" in body.get("addLabelIds", []):
            self.starred_ids.append(id)
        return _box({"id": id})

    def _create_draft(self, *, body: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        """Record created draft payloads."""
        self.draft_creations.append(body)
        return _box({"id": f"draft_{len(self.draft_creations)}"})

    def users(self) -> SimpleNamespace:
        """Return Gmail users resource stub."""
        return SimpleNamespace(
            getProfile=lambda **_kw: _box(
                {"emailAddress": "owner@example.com", "historyId": "10"}
            ),
            settings=lambda: SimpleNamespace(
                sendAs=lambda: SimpleNamespace(list=lambda **_kw: _box({}))
            ),
            messages=lambda: SimpleNamespace(
                get=lambda *, id, **_kw: _box({"id": id, "payload": {"mimeType": "text/plain"}}),
                modify=self._modify,
            ),
            drafts=lambda: SimpleNamespace(create=self._create_draft),
        )


def _make_forum_bulk_email(
    message_id: str = "m_sched_1",
    attachments: list[str] | None = None,
) -> EmailMessage:
    """Build a CATEGORY_FORUMS + List-Unsubscribe email from scheduler@sub.vendor.example."""
    return EmailMessage(
        message_id=message_id,
        thread_id=f"th_{message_id}",
        sender="Vendor Scheduler <scheduler@sub.vendor.example>",
        recipient="owner@example.com",
        subject="Weekly activity schedule update",
        snippet="Click to unsubscribe / se désabonner",
        body_text="Hello, the weekly schedule is updated. Click to unsubscribe / se désabonner.",
        headers={"List-Unsubscribe": "<https://vendor.example/unsub>"},
        label_ids=["INBOX", "CATEGORY_FORUMS"],
        attachment_filenames=list(attachments or []),
    )


def _wire_triage_stack(
    tmp_path: Path,
    inbox_filters: InboxFilterConfig,
    *,
    spawn_live_agy: bool = False,
    cognitive_fn: Any = None,
) -> tuple[InboxTriageService, AgySessionManager, _MinimalGmailClient]:
    """Wire InboxTriageService with isolated StateStore, Gmail stub, and AgySessionManager."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings(
        features=FeatureFlagsConfig(
            inbox_auto_triage=True,
            gmail_drafts_and_send=True,
            calendar_management=True,
            drive_knowledge_sync=False,
        ),
        inbox_filters=inbox_filters,
        agy_sync_daemon_on_changes=False,
    )
    store = StateStore(paths)
    gm_cli = _MinimalGmailClient()
    gmail = GmailService(paths, settings, gmail_client=gm_cli)
    drive = DriveService(paths, settings, store)
    agy = AgySessionManager(
        paths,
        settings,
        store,
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=tmp_path / "agy_data",
    )
    triage = InboxTriageService(
        state_store=store,
        gmail_service=gmail,
        knowledge_service=KnowledgeService(paths, settings, store),
        drive_service=drive,
        agy_manager=agy,
        cognitive_fn=cognitive_fn,
        spawn_live_agy=spawn_live_agy,
        settings=settings,
    )
    return triage, agy, gm_cli


def test_priority_allowlist_bypasses_zero_token_and_passive_filters_without_attachments(
    tmp_path: Path,
) -> None:
    """Verify allowlist_domains admits attachment-less bulk/forum mail and blocks DRAFT_REPLY."""
    msg = _make_forum_bulk_email("m_no_att", attachments=[])
    folded = fold_accents(f"{msg.subject}\n{msg.snippet}\n{msg.body_text}")

    default_cfg = InboxFilterConfig()
    assert InboxEligibilityPolicy(default_cfg).evaluate(msg).reason == "SKIP_GMAIL_CATEGORY"
    inbox_only_msg = msg.model_copy(update={"label_ids": ["INBOX"]})
    assert (
        InboxEligibilityPolicy(default_cfg).evaluate(inbox_only_msg).reason
        == "SKIP_BULK_AUTOMATED"
    )
    assert is_passive_or_automated_email(msg, folded, inbox_filters=default_cfg) is True

    triage_default, _, _ = _wire_triage_stack(tmp_path / "default", default_cfg)
    dec_skip = triage_default.process_message(msg)
    assert dec_skip.output_type == InboxOutputType.SILENT_NO_ACTION
    assert dec_skip.skipped_pre_filter is True

    priority_cfg = InboxFilterConfig.from_dict({"allowlist_domains": ["vendor.example"]})
    elig = InboxEligibilityPolicy(priority_cfg).evaluate(msg)
    assert elig.eligible is True and elig.reason == "ELIGIBLE"
    assert is_passive_or_automated_email(msg, folded, inbox_filters=priority_cfg) is False

    cog_calls: list[str] = []

    def _propose_draft(
        incoming: EmailMessage, _docs: list[KnowledgeDocument]
    ) -> tuple[InboxOutputType, str, str | None]:
        """Simulate cognitive triage proposing DRAFT_REPLY on a bulk-header email."""
        cog_calls.append(incoming.message_id)
        return (InboxOutputType.DRAFT_REPLY, "Attempt reply", "Thank you for the update.")

    triage_prio, _, gm_prio = _wire_triage_stack(
        tmp_path / "prio_cog", priority_cfg, cognitive_fn=_propose_draft
    )
    dec_prio = triage_prio.process_message(msg)
    assert cog_calls == ["m_no_att"]
    assert dec_prio.output_type == InboxOutputType.USER_ACTION_REQUIRED
    assert dec_prio.draft_id is None and dec_prio.starred is True
    assert gm_prio.draft_creations == []

    triage_fb, _, gm_fb = _wire_triage_stack(tmp_path / "prio_fb", priority_cfg)
    dec_fb = triage_fb.process_message(_make_forum_bulk_email("m_no_att_fb", attachments=[]))
    assert dec_fb.output_type == InboxOutputType.USER_ACTION_REQUIRED
    assert dec_fb.draft_id is None and dec_fb.starred is True and gm_fb.draft_creations == []


def test_allowlist_automated_domains_still_requires_attachment_non_regression() -> None:
    """Verify allowlist_automated_domains skips without attachment and passes with .pdf."""
    auto_cfg = InboxFilterConfig(allowlist_automated_domains=["vendor.example"])
    policy = InboxEligibilityPolicy(auto_cfg)

    no_att = _make_forum_bulk_email("m_auto_no_att", attachments=[])
    folded = fold_accents(f"{no_att.subject}\n{no_att.snippet}\n{no_att.body_text}")
    assert policy.evaluate(no_att).eligible is False
    assert policy.evaluate(no_att).reason == "SKIP_GMAIL_CATEGORY"
    assert (
        policy.evaluate(no_att.model_copy(update={"label_ids": ["INBOX"]})).reason
        == "SKIP_BULK_AUTOMATED"
    )
    assert is_passive_or_automated_email(no_att, folded, inbox_filters=auto_cfg) is True

    with_pdf = _make_forum_bulk_email("m_auto_pdf", attachments=["schedule.pdf"])
    assert policy.evaluate(with_pdf).eligible is True
    assert is_passive_or_automated_email(with_pdf, folded, inbox_filters=auto_cfg) is False


def _attach_agy_spy(
    agy: AgySessionManager, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[list[str], Path | None, int]]:
    """Spy on AgySessionManager._run_agy_command and capture (cmd, cwd, timeout)."""
    captured: list[tuple[list[str], Path | None, int]] = []

    def _spy_run_agy(
        cmd: list[str], cwd: Path | None = None, timeout: int = 180
    ) -> subprocess.CompletedProcess[str]:
        """Record CLI argv, cwd, and timeout while returning a live conversation UUID."""
        captured.append((list(cmd), cwd, timeout))
        cid = f"22222222-3333-4444-5555-{len(captured):012d}"
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"conversation_id": cid}), "")

    monkeypatch.setattr(agy, "_run_agy_command", _spy_run_agy)
    return captured


def test_routine_vs_escalated_action_required_live_session_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify routine USER_ACTION_REQUIRED uses Neon (180s) while complex uses Carbon (600s)."""
    triage, agy, _ = _wire_triage_stack(tmp_path, InboxFilterConfig(), spawn_live_agy=True)
    captured = _attach_agy_spy(agy, monkeypatch)

    routine_action = EmailMessage(
        message_id="m_routine_act",
        thread_id="th_routine_act",
        sender="Advisor <advisor@partner.example>",
        recipient="owner@example.com",
        subject="Action required: monthly filing validation",
        body_text="Please validate the monthly filing before Friday.",
        label_ids=["INBOX"],
    )
    dec_routine = triage.process_message(routine_action, spawn_live_agy=True)
    assert dec_routine.output_type == InboxOutputType.USER_ACTION_REQUIRED
    assert dec_routine.needs_orchestrator_study is False
    cmd_r, _, timeout_r = captured[-1]
    assert cmd_r[cmd_r.index("--model") + 1] == "gemini-pro-orchestrator-neon"
    assert "--effort" not in cmd_r and timeout_r == 180

    complex_action = EmailMessage(
        message_id="m_complex_act",
        thread_id="th_complex_act",
        sender="Tax Office <inspector@agency.example>",
        recipient="owner@example.com",
        subject="Action required: Mise en demeure / tax audit",
        body_text="Formal notice regarding multi-year tax audit review.",
        label_ids=["INBOX"],
    )
    dec_complex = triage.process_message(complex_action, spawn_live_agy=True)
    assert dec_complex.output_type == InboxOutputType.USER_ACTION_REQUIRED
    assert dec_complex.needs_orchestrator_study is True
    cmd_c, _, timeout_c = captured[-1]
    assert cmd_c[cmd_c.index("--model") + 1] == "gemini-pro-orchestrator"
    assert "--effort" not in cmd_c and timeout_c == 600


def test_escalated_draft_reply_spawns_carbon_orchestrator_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify escalated DRAFT_REPLY ('avenant au contrat') spawns Carbon (600s, no --effort)."""
    triage, agy, _ = _wire_triage_stack(tmp_path, InboxFilterConfig(), spawn_live_agy=True)
    captured = _attach_agy_spy(agy, monkeypatch)

    escalated_draft = EmailMessage(
        message_id="m_esc_draft",
        thread_id="th_esc_draft",
        sender="Counsel <counsel@partner.example>",
        recipient="owner@example.com",
        subject="Proposition avenant au contrat",
        body_text="Bonjour, voici un projet d'avenant au contrat pour avis.",
        label_ids=["INBOX"],
    )
    dec_draft = triage.process_message(escalated_draft, spawn_live_agy=True)
    assert dec_draft.output_type == InboxOutputType.DRAFT_REPLY
    assert dec_draft.needs_orchestrator_study is True
    cmd_d, _, timeout_d = captured[-1]
    assert cmd_d[cmd_d.index("--model") + 1] == "gemini-pro-orchestrator"
    assert "--effort" not in cmd_d
    assert timeout_d == 600
