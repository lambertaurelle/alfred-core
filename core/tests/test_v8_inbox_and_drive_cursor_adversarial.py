"""Adversarial red-team and non-regression tests for inbox directives and Drive cursors."""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alfred.domain import models as dom
from alfred.domain.feature_models import FeatureFlagsConfig, InboxFilterConfig
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_governance_service import DriveGovernanceService
from alfred.services.drive_service import FOLDER_MIME, DriveService
from alfred.services.drive_watcher_service import DriveWatcherService
from alfred.services.gmail_drive_bridge_service import GmailDriveBridgeService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_booking_detector import is_passive_or_automated_email
from alfred.services.inbox_eligibility import InboxEligibilityPolicy
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService


def _b64u(raw: bytes) -> str:
    """Encode bytes to unpadded URL-safe base64 string."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _box(payload: Any) -> SimpleNamespace:
    """Wrap payload behind a .execute() callable."""
    return SimpleNamespace(execute=lambda: payload)


def _wire_triage(
    tmp_path: Path,
    payload: dict[str, Any],
    runner_fn: Callable[[list[str]], subprocess.CompletedProcess[str]] | None,
    *,
    allowlist_domains: list[str] | None = None,
    custom_rules: str = "",
) -> tuple[InboxTriageService, GmailService, StateStore]:
    """Build isolated InboxTriageService with custom rules and mocked Gmail/Agy."""
    paths = dom.AlfredPaths.from_env(tmp_path)
    if custom_rules:
        rp = paths.instance_dir / "config" / "prompts" / "inbox_custom_rules.md"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(custom_rules, encoding="utf-8")
    store = StateStore(paths)
    cfg = dom.AlfredSettings(
        inbox_filters=InboxFilterConfig(allowlist_automated_domains=allowlist_domains or [])
    )
    gm_cli = SimpleNamespace(
        users=lambda: SimpleNamespace(
            getProfile=lambda **_kw: _box({"emailAddress": "owner@example.com", "historyId": "1"}),
            settings=lambda: SimpleNamespace(
                sendAs=lambda: SimpleNamespace(list=lambda **_kw: _box({}))
            ),
            messages=lambda: SimpleNamespace(
                get=lambda **_kw: _box(payload),
                modify=lambda **_kw: _box({"id": payload.get("id", "m1")}),
                attachments=lambda: SimpleNamespace(
                    get=lambda **_kw: _box({"data": "", "size": 0})
                ),
            ),
        )
    )
    gmail = GmailService(paths, cfg, gmail_client=gm_cli)
    drive = DriveService(paths, cfg, store, drive_client=SimpleNamespace())
    agy = AgySessionManager(
        paths, cfg, store, runner_fn=runner_fn,
        agy_locator_fn=(lambda: "/usr/bin/agy") if runner_fn is not None else (lambda: None),
        antigravity_data_dir=tmp_path / "agy_data",
    )
    bridge = GmailDriveBridgeService(cfg, store, gmail, drive)
    triage = InboxTriageService(
        store, gmail, KnowledgeService(paths, cfg, store), drive, agy,
        settings=cfg, attachment_bridge=bridge,
    )
    return triage, gmail, store


def test_adversarial_directives_hallucination_traversal_and_shell_injection() -> None:
    """Verify parse_triage_directives drops hallucinations/traversals and quotes shell args."""
    from alfred.services import inbox_plan_renderer
    from alfred.services.agy_triage_parser import parse_triage_directives

    unwrapped: dict[str, object] = {
        "output_type": "USER_ACTION_REQUIRED",
        "reason": "Test security",
        "attachments_to_save": [
            {"filename": "hallucinated.pdf", "parent_id": "fld_1"},
            {"filename": "../../secrets/token.json", "parent_id": "fld_1"},
            {
                "filename": "STATEMENT_2026_11.PDF",
                "parent_id": "fld_ok; touch /tmp/pwned\x00\n",
                "target_name": "../escape\x00.pdf",
            },
            {"filename": "statement_2026_11.pdf", "parent_id": "dup", "target_name": "../../.."},
        ],
    }
    parsed = parse_triage_directives(unwrapped, available_filenames=["statement_2026_11.pdf"])
    assert len(parsed.attachments_to_save) == 1
    item = parsed.attachments_to_save[0]
    assert item.filename == "statement_2026_11.pdf"
    assert "\x00" not in item.parent_id and "\n" not in item.parent_id
    assert not any(tok in item.target_name for tok in ("..", "/", "\\", "\x00"))

    msg = dom.EmailMessage(
        message_id="m_sec", thread_id="th_sec", sender="noreply@portal.example.com",
        subject="Security", attachment_filenames=["statement_2026_11.pdf"],
    )
    plan = inbox_plan_renderer.render_action_required_plan(
        msg, "Check", ["Point 1"], "01_doc.md", attachments_to_save=parsed.attachments_to_save
    )
    cmd_line = next(
        ln.split("`", 1)[1].rsplit("`", 1)[0]
        for ln in plan.splitlines()
        if "alfred save-attachment-to-drive" in ln
    )
    tokens = shlex.split(cmd_line)
    assert ";" not in tokens and "touch" not in tokens
    assert tokens[tokens.index("--parent-id") + 1] == item.parent_id


def test_adversarial_silent_with_attachments_forced_to_action_required(tmp_path: Path) -> None:
    """Verify SILENT_NO_ACTION with valid attachments_to_save promotes to USER_ACTION_REQUIRED."""
    payload: dict[str, Any] = {
        "id": "m_prom", "threadId": "th_prom", "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "alice@example.com"},
                {"name": "Subject", "value": "Doc"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64u(b"FYI attached.")}},
                {"mimeType": "application/pdf", "filename": "statement_2026_11.pdf",
                 "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                 "body": {"data": _b64u(b"%PDF-1.4")}},
            ],
        },
    }

    def _silent_real(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return SILENT_NO_ACTION paired with a real attachment filing target."""
        out = {"conversation_id": "conv-prom-1", "structured_output": {
            "output_type": "SILENT_NO_ACTION", "reason": "FYI only",
            "attachments_to_save": [
                {"filename": "statement_2026_11.pdf", "parent_id": "fld_statements"}
            ],
        }}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    triage_a, gmail_a, _ = _wire_triage(tmp_path / "prom", payload, _silent_real)
    dec_a = triage_a.process_message(gmail_a._parse_raw_message(payload), use_cognitive_agy=True)
    assert dec_a.output_type == dom.InboxOutputType.USER_ACTION_REQUIRED and dec_a.starred is True
    assert [t.filename for t in dec_a.attachments_to_save] == ["statement_2026_11.pdf"]

    def _silent_ghost(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return SILENT_NO_ACTION paired only with a hallucinated attachment name."""
        out = {"conversation_id": "conv-prom-2", "structured_output": {
            "output_type": "SILENT_NO_ACTION", "reason": "Newsletter",
            "attachments_to_save": [{"filename": "ghost.pdf", "parent_id": "fld_statements"}],
        }}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    triage_b, gmail_b, _ = _wire_triage(tmp_path / "hall", payload, _silent_ghost)
    dec_b = triage_b.process_message(gmail_b._parse_raw_message(payload), use_cognitive_agy=True)
    assert dec_b.output_type == dom.InboxOutputType.SILENT_NO_ACTION
    assert dec_b.starred is False and dec_b.attachments_to_save == []


def test_adversarial_domain_prefix_spoof_disguised_image_and_inline_pdf(tmp_path: Path) -> None:
    """Verify domain-prefix spoofing, image-only PJ, and inline PDF cannot bypass 0-token."""
    from alfred.services.inbox_eligibility import (
        has_eligible_document_attachment,
        is_allowlisted_automated_sender,
    )

    cfg = InboxFilterConfig(allowlist_automated_domains=["portal.example.com"])
    policy = InboxEligibilityPolicy(cfg)
    spoof = dom.EmailMessage(
        message_id="m_spf", thread_id="th_spf", sender="noreply@evil-portal.example.com",
        subject="Statement", headers={"List-Unsubscribe": "<https://evil-portal.example.com/u>"},
        label_ids=["INBOX"], attachment_filenames=["statement_2026_11.pdf"],
    )
    assert is_allowlisted_automated_sender(spoof.sender, cfg) is False
    assert policy.evaluate(spoof).reason == "SKIP_BULK_AUTOMATED"
    assert is_passive_or_automated_email(spoof, "statement", inbox_filters=cfg) is True

    img_only = spoof.model_copy(
        update={"sender": "noreply@portal.example.com", "attachment_filenames": ["logo.png"]}
    )
    assert has_eligible_document_attachment(img_only) is False
    assert policy.evaluate(img_only).reason == "SKIP_BULK_AUTOMATED"

    inline_payload: dict[str, Any] = {
        "id": "m_inl", "threadId": "th_inl", "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "noreply@portal.example.com"},
                {"name": "Subject", "value": "Notice"},
                {"name": "List-Unsubscribe", "value": "<https://portal.example.com/u>"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64u(b"Passive notice.")}},
                {"mimeType": "application/pdf", "filename": "inline_notice.pdf",
                 "headers": [{"name": "Content-Disposition", "value": "inline"}],
                 "body": {"data": _b64u(b"%PDF-1.4")}},
            ],
        },
    }
    calls: list[list[str]] = []

    def _rec_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Record agy invocation."""
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "{}", "")

    triage, gmail, _ = _wire_triage(
        tmp_path, inline_payload, _rec_runner, allowlist_domains=["portal.example.com"]
    )
    parsed_inl = gmail._parse_raw_message(inline_payload)
    assert parsed_inl.attachment_filenames == []
    assert policy.evaluate(parsed_inl).reason == "SKIP_BULK_AUTOMATED"

    forged = parsed_inl.model_copy(
        update={"attachment_filenames": ["inline_notice.pdf"], "headers": {}}
    )
    dec = triage.process_message(forged, use_cognitive_agy=True)
    assert dec.output_type == dom.InboxOutputType.SILENT_NO_ACTION and calls == []


def test_adversarial_denylist_and_calendar_invite_override_allowlist() -> None:
    """Verify denylist and calendar invite bypass take precedence over automated allowlist."""
    cfg = InboxFilterConfig(
        allowlist_automated_domains=["portal.example.com"],
        denylist_senders=["noreply@portal.example.com"],
    )
    assert cfg.allowlist_automated_domains == ["portal.example.com"]
    msg = dom.EmailMessage(
        message_id="m_prio", thread_id="th_prio", sender="noreply@portal.example.com",
        subject="Doc", label_ids=["INBOX"], attachment_filenames=["statement_2026_11.pdf"],
    )
    assert InboxEligibilityPolicy(cfg).evaluate(msg).reason == "SKIP_DENYLIST"

    cfg_cal = InboxFilterConfig(allowlist_automated_domains=["portal.example.com"])
    cal_msg = msg.model_copy(
        update={
            "attachment_filenames": ["invite.ics", "statement_2026_11.pdf"],
            "mime_types": ["text/calendar", "application/pdf"],
        }
    )
    assert InboxEligibilityPolicy(cfg_cal).evaluate(cal_msg).reason == "BYPASS_CALENDAR_INVITE"


def test_non_regression_cognitive_mode_and_offline_action_signal_fallback(
    tmp_path: Path,
) -> None:
    """Verify cognitive mode executes on ACTION_REQUIRED_PATTERNS & keeps offline fallback."""
    payload: dict[str, Any] = {
        "id": "m_act", "threadId": "th_act", "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "Portal <noreply@portal.example.com>"},
                {"name": "Subject", "value": "Action requise : releve mensuel disponible"},
            ],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64u(b"Action requise avant jeu.")}},
                {"mimeType": "application/pdf", "filename": "statement_2026_11.pdf",
                 "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                 "body": {"data": _b64u(b"%PDF-1.4")}},
            ],
        },
    }
    calls: list[list[str]] = []

    def _ok_cog(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return USER_ACTION_REQUIRED with keep_in_inbox=True from custom rules."""
        calls.append(cmd)
        out = {"conversation_id": "conv-cog-1", "structured_output": {
            "output_type": "USER_ACTION_REQUIRED", "reason": "Action + custom keep_in_inbox",
            "keep_in_inbox": True,
            "attachments_to_save": [
                {"filename": "statement_2026_11.pdf", "parent_id": "fld_statements"}
            ],
        }}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(out), "")

    t_cog, g_cog, _ = _wire_triage(
        tmp_path / "cog", payload, _ok_cog, allowlist_domains=["portal.example.com"],
        custom_rules="- Keep monthly statements in inbox.\n",
    )
    dec_cog = t_cog.process_message(g_cog._parse_raw_message(payload), use_cognitive_agy=True)
    assert len(calls) == 1 and dec_cog.keep_in_inbox is True
    assert dec_cog.attachments_to_save[0].filename == "statement_2026_11.pdf"

    t_off, g_off, _ = _wire_triage(
        tmp_path / "off", payload, None, allowlist_domains=["portal.example.com"]
    )
    dec_off = t_off.process_message(g_off._parse_raw_message(payload), use_cognitive_agy=False)
    assert dec_off.output_type == dom.InboxOutputType.USER_ACTION_REQUIRED and dec_off.starred

    t_err, g_err, _ = _wire_triage(
        tmp_path / "err", payload, lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "boom"),
        allowlist_domains=["portal.example.com"],
    )
    dec_err = t_err.process_message(g_err._parse_raw_message(payload), use_cognitive_agy=True)
    assert dec_err.output_type == dom.InboxOutputType.USER_ACTION_REQUIRED and dec_err.starred


def _drive_change(fid: str, name: str, mtime: str) -> dict[str, Any]:
    """Construct a Google Drive v3 change dict for tests."""
    return {"fileId": fid, "removed": False, "file": {
        "id": fid, "name": name, "mimeType": "application/pdf",
        "parents": ["fld_1"], "modifiedTime": mtime, "ownedByMe": True,
    }}


def test_adversarial_drive_hot_bootstrap_and_multipage_crash_safety(tmp_path: Path) -> None:
    """Verify hot-bootstrap from legacy drive_page_token and multi-page crash safety."""
    paths = dom.AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)
    cfg = dom.AlfredSettings(features=FeatureFlagsConfig(drive_watcher=True))
    fld = dom.DriveEntry(
        file_id="fld_1", name="01_Admin", mime_type=FOLDER_MIME, path="/01_Admin", is_folder=True
    )
    store.drive_index.save(dom.DriveMetadataIndex(start_page_token="tok_5", entries={"fld_1": fld}))
    store.bookmarks.save(
        dom.GoogleSyncBookmarks(drive_page_token="tok_5", drive_watcher_page_token=None)
    )
    fail_flag = {"drop": True}

    def _changes_list(*, pageToken: str, **_kw: Any) -> SimpleNamespace:  # noqa: N803
        """Serve multi-page changes with simulated page-2 failure."""
        def _exec() -> dict[str, Any]:
            """Execute simulated Drive changes.list page fetch."""
            if pageToken == "tok_5":
                return {"changes": [_drive_change("f_p1", "P1.pdf", "2026-11-25T10:01:00Z")],
                        "nextPageToken": "page_2"}
            if pageToken == "page_2" and fail_flag["drop"]:
                raise RuntimeError("transient network drop")
            if pageToken == "page_2":
                return {"changes": [_drive_change("f_p2", "P2.pdf", "2026-11-25T10:02:00Z")],
                        "newStartPageToken": "tok_9"}
            return {"changes": [], "newStartPageToken": pageToken}

        return SimpleNamespace(execute=_exec)

    drv_cli = SimpleNamespace(changes=lambda: SimpleNamespace(list=_changes_list))
    drive = DriveService(paths, cfg, store, drive_client=drv_cli)
    watcher = DriveWatcherService(
        paths, cfg, store, drive, DriveGovernanceService(cfg, store, drive),
        KnowledgeService(paths, cfg, store),
        AgySessionManager(paths, cfg, store, antigravity_data_dir=tmp_path / "agy"),
    )
    with pytest.raises(RuntimeError, match="transient network drop"):
        watcher.collect_delta_report()
    assert store.bookmarks.load().drive_watcher_page_token is None

    fail_flag["drop"] = False
    rep = watcher.collect_delta_report()
    assert [it.file_id for it in rep.eligible_items] == ["f_p1", "f_p2"]
    bm = store.bookmarks.load()
    assert bm.drive_watcher_page_token == "tok_9" and bm.drive_watcher_last_polled_at != ""


def test_adversarial_sync_changes_cursor_isolation_and_atomic_index(tmp_path: Path) -> None:
    """Verify sync_changes never alters drive_watcher_page_token & preserves concurrent entries."""
    paths = dom.AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)
    cfg = dom.AlfredSettings(
        features=FeatureFlagsConfig(drive_knowledge_sync=True, drive_watcher=True)
    )
    fld = dom.DriveEntry(
        file_id="fld_1", name="01_Admin", mime_type=FOLDER_MIME, path="/01_Admin", is_folder=True
    )
    store.drive_index.save(dom.DriveMetadataIndex(start_page_token="tok_2", entries={"fld_1": fld}))
    store.bookmarks.save(
        dom.GoogleSyncBookmarks(drive_page_token="tok_2", drive_watcher_page_token="tok_9")
    )

    def _changes_list(*, pageToken: str, **_kw: Any) -> SimpleNamespace:  # noqa: N803
        """Inject concurrent attachment entry into drive_index before sync_changes finishes."""
        def _exec() -> dict[str, Any]:
            """Execute Drive changes.list and simulate concurrent index update."""
            conc = dom.DriveEntry(
                file_id="att_concurrent", name="concurrent.pdf", mime_type="application/pdf",
                path="/01_Admin/concurrent.pdf",
            )
            store.drive_index.update(
                lambda idx: idx.model_copy(
                    update={"entries": {**idx.entries, "att_concurrent": conc}}
                )
            )
            return {
                "changes": [_drive_change("f_sync", "Synced.pdf", "2026-11-25T10:03:00Z")],
                "newStartPageToken": "tok_3",
            }

        assert pageToken == "tok_2"
        return SimpleNamespace(execute=_exec)

    drv_cli = SimpleNamespace(changes=lambda: SimpleNamespace(list=_changes_list))
    drive = DriveService(paths, cfg, store, drive_client=drv_cli)
    drive.sync_changes()
    bm_after = store.bookmarks.load()
    assert bm_after.drive_page_token == "tok_3" and bm_after.drive_watcher_page_token == "tok_9"
    entries = store.drive_index.load().entries
    assert "att_concurrent" in entries and "f_sync" in entries
