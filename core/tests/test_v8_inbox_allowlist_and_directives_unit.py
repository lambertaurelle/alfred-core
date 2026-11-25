"""Unit tests for inbox automated-sender allowlist, directives, plan renderer, and cursor gating."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from alfred.controllers.attachment_cli import dispatch_attachment_command
from alfred.domain import models as dom
from alfred.domain.feature_models import FeatureFlagsConfig, InboxFilterConfig
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.delegation_service import DelegationService
from alfred.services.drive_service import DriveService
from alfred.services.email_security_service import wrap_untrusted_email_block
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_booking_detector import is_passive_or_automated_email
from alfred.services.inbox_eligibility import InboxEligibilityPolicy
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.routine_service import RoutineService

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


def test_inbox_filter_config_allowlist_and_settings_example() -> None:
    """Verify InboxFilterConfig defaults, normalization, and settings.example.yaml keys."""
    default_cfg = InboxFilterConfig()
    assert default_cfg.allowlist_automated_senders == []
    assert default_cfg.allowlist_automated_domains == []
    from_none = InboxFilterConfig.from_dict(None)
    assert from_none.allowlist_automated_senders == []
    assert from_none.allowlist_automated_domains == []

    parsed = InboxFilterConfig.from_dict({
        "allowlist_automated_senders": [
            " Portal <NoReply@Portal.Example.COM> ", " Alerts@Example.ORG ", "   ",
        ],
        "allowlist_automated_domains": ["@.Portal.Example.COM ", " @Sub.Example.ORG", ""],
    })
    assert parsed.allowlist_automated_senders == [
        "noreply@portal.example.com", "alerts@example.org",
    ]
    assert parsed.allowlist_automated_domains == ["portal.example.com", "sub.example.org"]

    raw_example = yaml.safe_load(
        (WORKTREE_ROOT / "core" / "config" / "settings.example.yaml").read_text(encoding="utf-8")
    )
    assert raw_example["inbox"]["allowlist_automated_senders"] == []
    assert raw_example["inbox"]["allowlist_automated_domains"] == []


def test_attachment_and_domain_models_directives_and_bookmarks() -> None:
    """Verify frozen directive models, models.__all__ export, bookmarks, and decision fields."""
    from alfred.domain.attachment_models import AttachmentFilingTarget, CognitiveTriageDirectives

    assert "AttachmentFilingTarget" in dom.__all__
    assert dom.AttachmentFilingTarget is AttachmentFilingTarget
    target = AttachmentFilingTarget(filename="statement_2026_11.pdf")
    assert target.parent_id == "" and target.target_name == ""
    with pytest.raises(ValidationError):
        target.filename = "other.pdf"

    directives = CognitiveTriageDirectives()
    assert not directives.keep_in_inbox and not directives.needs_orchestrator_study
    assert directives.research_brief == [] and not directives.attachments_specified
    assert directives.attachments_to_save == []
    with pytest.raises(ValidationError):
        directives.keep_in_inbox = True

    bm = dom.GoogleSyncBookmarks()
    assert bm.drive_watcher_page_token is None and bm.drive_watcher_last_polled_at == ""
    decision = dom.InboxTriageDecision(
        message_id="m_1", output_type=dom.InboxOutputType.SILENT_NO_ACTION, reason="ok"
    )
    assert decision.keep_in_inbox is False and decision.attachments_to_save == []


def test_collect_message_mime_summary_and_gmail_delegation(tmp_path: Path) -> None:
    """Verify collect_message_mime_summary skips inline/CID images and GmailService delegates."""
    from alfred.services.attachment_extractor import collect_message_mime_summary

    payload: dict[str, Any] = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/related", "parts": [
                {"mimeType": "text/html", "body": {"data": "PGI-aGVsbG88L2I-"}},
                {"mimeType": "image/png", "filename": "logo.png",
                 "headers": [{"name": "Content-Disposition", "value": "inline"}],
                 "body": {"attachmentId": "att-inline"}},
                {"mimeType": "image/png", "filename": "cid_banner.png",
                 "headers": [{"name": "Content-ID", "value": "<banner@example.com>"}],
                 "body": {"attachmentId": "att-cid"}},
            ]},
            {"mimeType": "application/pdf", "filename": "statement_2026_11.pdf",
             "headers": [{"name": "Content-Disposition", "value": "attachment"}],
             "body": {"attachmentId": "att-pdf"}},
            {"mimeType": "text/calendar", "filename": "invite.ics",
             "headers": [{"name": "Content-Disposition", "value": "attachment"}],
             "body": {"attachmentId": "att-ics"}},
        ],
    }
    mimes, filenames = collect_message_mime_summary(payload)
    assert set(mimes) == {
        "multipart/mixed", "multipart/related", "text/html",
        "image/png", "application/pdf", "text/calendar",
    }
    assert filenames == ["statement_2026_11.pdf", "invite.ics"]

    gmail = GmailService(dom.AlfredPaths.from_env(tmp_path), dom.AlfredSettings())
    parsed = gmail._parse_raw_message(
        {
            "id": "m_mime_1",
            "threadId": "th_mime_1",
            "labelIds": ["INBOX"],
            "payload": {**payload, "headers": [{"name": "From", "value": "a@example.com"}]},
        }
    )
    assert "image/png" in parsed.mime_types and "text/calendar" in parsed.mime_types
    assert parsed.attachment_filenames == ["statement_2026_11.pdf", "invite.ics"]


def test_allowlisted_automated_sender_and_document_extensions() -> None:
    """Verify DOCUMENT_ATTACHMENT_EXTENSIONS and allowlisted sender/domain matching."""
    from alfred.services.inbox_eligibility import (
        DOCUMENT_ATTACHMENT_EXTENSIONS,
        has_allowlisted_automated_attachment,
        has_eligible_document_attachment,
        is_allowlisted_automated_sender,
    )

    expected_docs = {
        ".pdf", ".docx", ".xlsx", ".doc", ".xls", ".csv", ".tsv", ".odt",
        ".ods", ".rtf", ".txt", ".xml", ".json", ".html", ".htm", ".md",
    }
    assert expected_docs.issubset(DOCUMENT_ATTACHMENT_EXTENSIONS)
    for excluded in (".ics", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp"):
        assert excluded not in DOCUMENT_ATTACHMENT_EXTENSIONS

    base = dom.EmailMessage(message_id="m1", thread_id="t1", sender="noreply@portal.example.com",
                            subject="Notice")
    assert has_eligible_document_attachment(base) is False
    assert has_eligible_document_attachment(
        base.model_copy(update={"attachment_filenames": ["logo.png", "invite.ics"]})
    ) is False
    assert has_eligible_document_attachment(
        base.model_copy(update={"attachment_filenames": ["logo.png", "report.CSV"]})
    ) is True

    cfg = InboxFilterConfig(
        allowlist_automated_senders=["alerts@billing.example.org"],
        allowlist_automated_domains=["portal.example.com"],
        denylist_senders=["blocked@portal.example.com"],
        denylist_domains=["blocked-sub.portal.example.com"],
    )
    assert is_allowlisted_automated_sender("Billing <alerts@billing.example.org>", cfg) is True
    assert is_allowlisted_automated_sender("noreply@portal.example.com", cfg) is True
    assert is_allowlisted_automated_sender("noreply@notif.portal.example.com", cfg) is True
    assert is_allowlisted_automated_sender("noreply@evil-portal.example.com", cfg) is False
    assert is_allowlisted_automated_sender("blocked@portal.example.com", cfg) is False
    assert is_allowlisted_automated_sender("a@blocked-sub.portal.example.com", cfg) is False
    assert is_allowlisted_automated_sender("", cfg) is False

    with_pdf = base.model_copy(update={"attachment_filenames": ["statement_2026_11.pdf"]})
    assert has_allowlisted_automated_attachment(with_pdf, cfg) is True
    assert has_allowlisted_automated_attachment(base, cfg) is False


def test_inbox_eligibility_policy_allowlist_and_priority_order() -> None:
    """Verify 0-token allowlist exemptions and strict priority order in InboxEligibilityPolicy."""
    cfg = InboxFilterConfig(
        allowlist_automated_domains=["portal.example.com"],
        denylist_senders=["denied@portal.example.com"],
    )
    policy = InboxEligibilityPolicy(cfg)
    auto_hdrs = {
        "List-Unsubscribe": "<https://portal.example.com/unsub>",
        "Auto-Submitted": "auto-generated",
    }
    base = dom.EmailMessage(
        message_id="m_el", thread_id="th_el", sender="noreply@notif.portal.example.com",
        subject="New document", headers=auto_hdrs, label_ids=["INBOX", "CATEGORY_PROMOTIONS"],
        attachment_filenames=["statement_2026_11.pdf"],
    )
    assert policy.evaluate(base).reason == "ELIGIBLE"
    no_att = base.model_copy(update={"attachment_filenames": []})
    assert policy.evaluate(no_att).reason == "SKIP_GMAIL_CATEGORY"
    no_cat = base.model_copy(update={"label_ids": ["INBOX"], "attachment_filenames": ["logo.png"]})
    assert policy.evaluate(no_cat).reason == "SKIP_BULK_AUTOMATED"
    non_allow = base.model_copy(
        update={"sender": "noreply@other.example.net", "label_ids": ["INBOX"]}
    )
    assert policy.evaluate(non_allow).reason == "SKIP_BULK_AUTOMATED"

    assert policy.evaluate(base.model_copy(update={"label_ids": ["SENT"]})).reason == "NOT_IN_INBOX"
    cal = base.model_copy(update={"attachment_filenames": ["invite.ics", "statement_2026_11.pdf"]})
    assert policy.evaluate(cal).reason == "BYPASS_CALENDAR_INVITE"
    denied = base.model_copy(update={"sender": "denied@portal.example.com"})
    assert policy.evaluate(denied).reason == "SKIP_DENYLIST"


def test_is_passive_or_automated_email_with_allowlist_and_override() -> None:
    """Verify is_passive_or_automated_email honors inbox_filters and has_doc_attachment_override."""
    cfg = InboxFilterConfig(allowlist_automated_domains=["portal.example.com"])
    msg = dom.EmailMessage(
        message_id="m_p", thread_id="th_p", sender="noreply@portal.example.com",
        subject="New document available in portal",
        body_text="New document available on your portal. Click unsubscribe.",
        headers={
            "List-Unsubscribe": "<https://portal.example.com/u>",
            "Auto-Submitted": "auto-generated",
        },
        attachment_filenames=["statement_2026_11.pdf"],
    )
    folded = "new document available in portal\nnew document available on your portal. unsubscribe"
    assert is_passive_or_automated_email(msg, folded) is True
    assert is_passive_or_automated_email(msg, folded, inbox_filters=cfg) is False
    assert is_passive_or_automated_email(
        msg, folded, inbox_filters=cfg, has_doc_attachment_override=False
    ) is True
    no_att = msg.model_copy(update={"attachment_filenames": []})
    assert is_passive_or_automated_email(
        no_att, folded, inbox_filters=cfg, has_doc_attachment_override=True
    ) is False


def test_wrap_untrusted_email_block_and_prompt_template_attachments() -> None:
    """Verify spotlighting adds Attachments header when non-empty and inbox_triage.md updates."""
    msg = dom.EmailMessage(
        message_id="m_sp", thread_id="th_sp", sender="a@example.com", subject="Docs",
        attachment_filenames=["statement_2026_11.pdf", "general_terms.pdf"],
    )
    wrapped, _ = wrap_untrusted_email_block(msg, "Body text")
    assert "Thread-ID: th_sp\nAttachments: statement_2026_11.pdf, general_terms.pdf" in wrapped

    plain_wrapped, _ = wrap_untrusted_email_block(
        msg.model_copy(update={"attachment_filenames": []}), "Body text"
    )
    assert "Attachments:" not in plain_wrapped

    tpl = (WORKTREE_ROOT / "core" / "templates" / "prompts" / "inbox_triage.md").read_text(
        encoding="utf-8"
    )
    for tok in ("CONFIRM_EVENT", "{{custom_inbox_rules}}", "keep_in_inbox", "attachments_to_save"):
        assert tok in tpl


def test_parse_triage_directives_schema_and_validation() -> None:
    """Verify cognitive schema keys and parse_triage_directives canonicalization/dedup."""
    from alfred.services.agy_triage_parser import COGNITIVE_TRIAGE_SCHEMA, parse_triage_directives

    props: dict[str, Any] = COGNITIVE_TRIAGE_SCHEMA["properties"]  # type: ignore[assignment]
    for key in ("keep_in_inbox", "research_brief", "attachments_to_save"):
        assert key in props
    att_item = props["attachments_to_save"]["items"]
    assert att_item["required"] == ["filename"] and att_item["additionalProperties"] is False

    unwrapped: dict[str, object] = {
        "output_type": "USER_ACTION_REQUIRED", "reason": "Monthly statement",
        "keep_in_inbox": True, "needs_orchestrator_study": True,
        "research_brief": [f"Step {i}" for i in range(10)] + ["   ", ""],
        "attachments_to_save": [
            {"filename": "STATEMENT_2026_11.PDF", "parent_id": "fld_stmt", "target_name": "26.pdf"},
            {"filename": "statement_2026_11.pdf", "parent_id": "fld_dup"},
            {"filename": "ghost.pdf", "parent_id": "fld_x"},
        ],
    }
    parsed = parse_triage_directives(
        unwrapped, available_filenames=["statement_2026_11.pdf", "general_terms.pdf"]
    )
    assert parsed.keep_in_inbox is True and parsed.needs_orchestrator_study is True
    assert len(parsed.research_brief) == 8 and parsed.attachments_specified is True
    assert len(parsed.attachments_to_save) == 1
    assert parsed.attachments_to_save[0].filename == "statement_2026_11.pdf"
    assert parsed.attachments_to_save[0].parent_id == "fld_stmt"
    assert parsed.attachments_to_save[0].target_name == "26.pdf"
    omitted = parse_triage_directives({"output_type": "DRAFT_REPLY", "reason": "ok"})
    assert omitted.attachments_specified is False and omitted.attachments_to_save == []


def test_agy_manager_last_triage_directives_and_reset(tmp_path: Path) -> None:
    """Verify AgySessionManager.last_triage_directives populates and resets on fallback."""
    import json

    paths = dom.AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)

    def _ok_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return structured JSON triage response."""
        out = {"conversation_id": "conv-dir-1", "structured_output": {
            "output_type": "USER_ACTION_REQUIRED", "reason": "Save PDF", "keep_in_inbox": True,
            "attachments_to_save": [{"filename": "STATEMENT_2026_11.PDF", "parent_id": "fld_s"}],
        }}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    mgr = AgySessionManager(
        paths, dom.AlfredSettings(), store, runner_fn=_ok_runner,
        agy_locator_fn=lambda: "/usr/bin/agy", antigravity_data_dir=tmp_path / "agy",
    )
    msg = dom.EmailMessage(message_id="m_d1", thread_id="th_d1", sender="a@example.com",
                           subject="Doc", attachment_filenames=["statement_2026_11.pdf"])
    mgr.classify_email_full(msg, [])
    assert mgr.last_triage_directives.keep_in_inbox is True
    assert mgr.last_triage_directives.attachments_to_save[0].filename == "statement_2026_11.pdf"

    mgr_fail = AgySessionManager(paths, dom.AlfredSettings(), store, agy_locator_fn=lambda: None,
                                 antigravity_data_dir=tmp_path / "agy")
    mgr_fail.classify_email_full(msg, [])
    assert mgr_fail.last_triage_directives.keep_in_inbox is False
    assert mgr_fail.last_triage_directives.attachments_to_save == []


def test_inbox_plan_renderer_and_calendar_bridge_reexports() -> None:
    """Verify inbox_plan_renderer options, selective attachments, drive_watcher, & re-exports."""
    from alfred.domain.attachment_models import AttachmentFilingTarget
    from alfred.domain.calendar_models import (
        CalendarActionIntent,
        CalendarSlotProposal,
        CalendarTriageAction,
    )
    from alfred.services import inbox_calendar_bridge as bridge
    from alfred.services import inbox_plan_renderer as renderer

    assert bridge.render_draft_reply_plan is renderer.render_draft_reply_plan
    assert bridge.render_action_required_plan is renderer.render_action_required_plan
    assert bridge.render_silent_trace_markdown is renderer.render_silent_trace_markdown
    assert callable(renderer.build_action_research_brief)

    msg = dom.EmailMessage(
        message_id="m_plan", thread_id="th_plan", sender="noreply@portal.example.com",
        subject="Statement", attachment_filenames=["statement_2026_11.pdf", "general_terms.pdf"],
    )
    sel = [AttachmentFilingTarget(filename="statement_2026_11.pdf", parent_id="fld_statements",
                                  target_name="2026-11_Statement.pdf")]
    plan_save = renderer.render_action_required_plan(
        msg, "Monthly statement", ["Check balance"], "02_finance.md",
        keep_in_inbox=True, drive_watcher_enabled=False, attachments_to_save=sel,
    )
    assert "alfred archive-email" not in plan_save and "--keep-in-inbox" in plan_save
    assert "alfred save-attachment-to-drive --message-id m_plan" in plan_save
    assert "--filename statement_2026_11.pdf" in plan_save
    assert "--name 2026-11_Statement.pdf" in plan_save
    assert "--parent-id fld_statements" in plan_save and "general_terms.pdf" not in plan_save

    plan_none = renderer.render_action_required_plan(
        msg, "No save", ["Review"], "doc1", attachments_to_save=[], archive_by_default=False
    )
    assert "save-attachment-to-drive" not in plan_none
    assert "alfred delegate --to drive" not in plan_none and "alfred archive-email" not in plan_none

    plan_watch = renderer.render_action_required_plan(
        msg, "Save via drive", ["Review"], "02_finance.md",
        drive_watcher_enabled=True, attachments_to_save=sel,
    )
    assert "alfred delegate --to drive --mode execute-approved" in plan_watch
    assert "--source-workspace alfred-inbox" in plan_watch
    assert "statement_2026_11.pdf" in plan_watch and "02_finance.md" in plan_watch
    assert "general_terms.pdf" not in plan_watch and "save-attachment-to-drive" not in plan_watch

    draft_md = renderer.render_draft_reply_plan(
        "alice@example.com", "Re: Hi", "d_1", "Hello", "", thread_id="th_plan", keep_in_inbox=True
    )
    assert "alfred send-draft --draft-id d_1 --thread-id th_plan --keep-in-inbox" in draft_md

    slot = CalendarSlotProposal(summary="RDV", start_iso="2026-12-01T10:00:00+01:00",
                                end_iso="2026-12-01T11:00:00+01:00")
    act = CalendarTriageAction(intent=CalendarActionIntent.CONFIRM_EVENT, slots=[slot])
    assert "--keep-in-inbox" in bridge.render_draft_calendar_section(
        act, "th_plan", "m_plan", frozenset(), keep_in_inbox=True
    )
    assert "--keep-in-inbox" in bridge.render_calendar_confirm_gate_plan(
        msg, "Confirmed", act, [], keep_in_inbox=True
    )


def test_delegation_and_attachment_cli_allow_drive_watcher_only(tmp_path: Path) -> None:
    """Verify DelegationTarget.DRIVE and save-attachment-to-drive honor drive_watcher alone."""
    import argparse
    from unittest.mock import MagicMock

    paths = dom.AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)
    watcher_only = dom.AlfredSettings(
        features=FeatureFlagsConfig(drive_knowledge_sync=False, drive_watcher=True)
    )
    agy = AgySessionManager(
        paths, watcher_only, store, agy_locator_fn=lambda: None,
        antigravity_data_dir=tmp_path / "agy",
    )
    dlg = DelegationService(paths, watcher_only, store, agy)
    assert dlg._is_target_enabled("drive") is True
    assert dlg.delegate(target="drive", title="Save", brief="File PDF").status != "SKIPPED_DISABLED"

    bridge = MagicMock()
    bridge.transfer_attachment_to_drive.return_value = MagicMock(
        model_dump=lambda mode="json": {"ok": True}
    )
    ns = argparse.Namespace(
        message_id="m_1", parent_id="fld_1", filename="statement.pdf",
        index=None, attachment_id="", target_name="", overwrite=False,
    )
    rc = dispatch_attachment_command("save-attachment-to-drive", bridge, watcher_only, ns)
    assert rc == 0 and bridge.transfer_attachment_to_drive.called

    both_off = dom.AlfredSettings(
        features=FeatureFlagsConfig(drive_knowledge_sync=False, drive_watcher=False)
    )
    assert DelegationService(paths, both_off, store, agy)._is_target_enabled("drive") is False


def test_routine_service_drive_delta_sync_delegation_and_status(tmp_path: Path) -> None:
    """Verify _job_drive_delta_sync delegates when drive_watcher=True and status checks token."""
    from unittest.mock import MagicMock

    paths = dom.AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)

    def _build_routine(sync_on: bool, watcher_on: bool) -> RoutineService:
        """Build RoutineService with isolated DriveService and mocked InboxTriageService."""
        cfg = dom.AlfredSettings(
            features=FeatureFlagsConfig(drive_knowledge_sync=sync_on, drive_watcher=watcher_on)
        )
        drive = DriveService(paths, cfg, store, drive_client=MagicMock())
        inbox = InboxTriageService(
            store, GmailService(paths, cfg), KnowledgeService(paths, cfg, store), drive,
            AgySessionManager(paths, cfg, store, antigravity_data_dir=tmp_path / "agy"),
            settings=cfg,
        )
        return RoutineService(paths, cfg, store, inbox, drive)

    assert _build_routine(False, False)._job_drive_delta_sync().status == "SKIPPED_DISABLED"
    assert (
        _build_routine(True, True)._job_drive_delta_sync().status == "SKIPPED_DELEGATED_TO_WATCHER"
    )
    assert _build_routine(True, False)._job_drive_delta_sync().status == "SKIPPED_UNINITIALIZED"

    store.bookmarks.save(dom.GoogleSyncBookmarks(drive_watcher_page_token="tok_w1"))
    report = _build_routine(True, True).get_status(systemd_user_dir=tmp_path / "no_systemd")
    assert report.drive_page_token_initialized is True
