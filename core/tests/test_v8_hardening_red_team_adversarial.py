"""Hardening red-team adversarial tests for inbox security, MIME, and Drive concurrency."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alfred.domain import models as dom
from alfred.domain.feature_models import FeatureFlagsConfig, InboxFilterConfig
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_governance_service import DriveGovernanceService
from alfred.services.drive_service import FOLDER_MIME, DriveService
from alfred.services.drive_watcher_service import DriveWatcherService
from alfred.services.gmail_drive_bridge_service import GmailDriveBridgeService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_booking_detector import is_automated_correspondent_address
from alfred.services.inbox_eligibility import (
    InboxEligibilityPolicy,
    has_eligible_document_attachment,
    is_allowlisted_automated_sender,
)
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
HOOK_SCRIPT = WORKTREE_ROOT / "core" / "scripts" / "security_pre_tool_hook.py"


def _b64u(raw: bytes) -> str:
    """Encode bytes to unpadded URL-safe base64 string."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _box(payload: Any) -> SimpleNamespace:
    """Wrap payload behind an API .execute() stub."""
    return SimpleNamespace(execute=lambda: payload)


def _wire_triage(
    tmp_path: Path,
    raw_msg: dict[str, Any],
    runner_fn: Any,
    *,
    allowlist_domains: list[str] | None = None,
    blobs: dict[str, bytes] | None = None,
) -> tuple[InboxTriageService, GmailService, list[dict[str, Any]]]:
    """Wire InboxTriageService with fake Gmail client and mocked AgySessionManager."""
    paths = dom.AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)
    cfg = dom.AlfredSettings(
        inbox_filters=InboxFilterConfig(allowlist_automated_domains=allowlist_domains or [])
    )
    drafts: list[dict[str, Any]] = []
    blob_map = dict(blobs or {})

    def _create_draft(*, body: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        """Record created draft body and return fake Gmail draft stub."""
        drafts.append(body)
        return _box({"id": "d_new"})

    gm_cli = SimpleNamespace(
        users=lambda: SimpleNamespace(
            getProfile=lambda **_kw: _box({"emailAddress": "owner@example.com", "historyId": "1"}),
            settings=lambda: SimpleNamespace(
                sendAs=lambda: SimpleNamespace(list=lambda **_kw: _box({}))
            ),
            messages=lambda: SimpleNamespace(
                get=lambda **_kw: _box(raw_msg),
                modify=lambda **_kw: _box({"id": raw_msg.get("id", "m1")}),
                attachments=lambda: SimpleNamespace(
                    get=lambda *, id, **_kw: _box(
                        {"data": _b64u(blob_map.get(id, b"")), "size": len(blob_map.get(id, b""))}
                    )
                ),
            ),
            drafts=lambda: SimpleNamespace(create=_create_draft),
        )
    )
    gmail = GmailService(paths, cfg, gmail_client=gm_cli)
    drive = DriveService(paths, cfg, store, drive_client=SimpleNamespace())
    agy = AgySessionManager(
        paths, cfg, store, runner_fn=runner_fn,
        agy_locator_fn=lambda: "/usr/bin/agy", antigravity_data_dir=tmp_path / "agy_data",
    )
    bridge = GmailDriveBridgeService(cfg, store, gmail, drive)
    triage = InboxTriageService(
        store, gmail, KnowledgeService(paths, cfg, store), drive, agy,
        settings=cfg, attachment_bridge=bridge,
    )
    return triage, gmail, drafts


def test_red_team_multi_address_from_and_subdomain_denylist() -> None:
    """Verify multi-address From bypasses and denylisted subdomains are blocked."""
    cfg = InboxFilterConfig(
        allowlist_automated_domains=["portal.example.com"],
        denylist_senders=["attacker@evil.example.org"],
        denylist_domains=["evil.example.org", "denied-portal.example.com"],
    )
    policy = InboxEligibilityPolicy(cfg)
    base = dom.EmailMessage(
        message_id="m_ma", thread_id="th_ma",
        sender="attacker@evil.example.org, noreply@sub.portal.example.com",
        subject="Statement", headers={"List-Unsubscribe": "<https://portal.example.com/u>"},
        label_ids=["INBOX", "CATEGORY_PROMOTIONS"], attachment_filenames=["statement.pdf"],
    )
    assert is_allowlisted_automated_sender(base.sender, cfg) is False
    assert policy.evaluate(base).reason == "SKIP_DENYLIST"
    for bad_sender in ("attacker@evil.example.org, .portal.example.com", "invalid-no-at"):
        assert is_allowlisted_automated_sender(bad_sender, cfg) is False
    sub_deny = base.model_copy(update={"sender": "noreply@app.denied-portal.example.com"})
    assert is_allowlisted_automated_sender(sub_deny.sender, cfg) is False
    assert policy.evaluate(sub_deny).reason == "SKIP_DENYLIST"


def test_red_team_mime_sanitization_and_parser_rejections() -> None:
    """Verify MIME filename sanitization and strict parser rejection of unsafe characters."""
    from alfred.services.agy_triage_parser import parse_triage_directives
    from alfred.services.attachment_extractor import collect_message_mime_summary

    payload: dict[str, Any] = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "application/pdf", "filename": "../../statement.pdf\u200b",
             "headers": [{"name": "Content-Disposition", "value": "attachment"}], "body": {}},
            {"mimeType": "application/pdf", "filename": "evil`\n.pdf",
             "headers": [{"name": "Content-Disposition", "value": "attachment"}], "body": {}},
        ],
    }
    _, fnames = collect_message_mime_summary(payload)
    assert len(fnames) == 2
    for fn in fnames:
        assert not any(bad in fn for bad in ("..", "\u200b", "\n", "`", "/", "\\"))
    assert has_eligible_document_attachment(
        dom.EmailMessage(message_id="m1", thread_id="t1", sender="a@example.com",
                         subject="S", attachment_filenames=fnames)
    )
    bad_dir = parse_triage_directives({"attachments_to_save": [
        {"filename": "foo\uff0fbar.pdf", "parent_id": "f1"},
        {"filename": "foo\nbar.pdf", "parent_id": "f1"},
        {"filename": "foo`bar.pdf", "parent_id": "f1"},
        {"filename": "foo\u202ebar.pdf", "parent_id": "f1"},
    ]})
    assert bad_dir.attachments_to_save == []
    good = parse_triage_directives({
        "attachments_to_save": [{"filename": "statement.pdf", "parent_id": "fld`id\n1"}]
    })
    assert len(good.attachments_to_save) == 1
    assert "`" not in good.attachments_to_save[0].parent_id
    assert "\n" not in good.attachments_to_save[0].parent_id


def test_red_team_plan_renderer_newline_backtick_and_hook(tmp_path: Path) -> None:
    """Verify plan renderer produces single-line backtick-safe commands accepted by PreToolUse."""
    from alfred.domain.attachment_models import AttachmentFilingTarget
    from alfred.services import inbox_plan_renderer

    dirty_msg = dom.EmailMessage(
        message_id="m_hk", thread_id="th_hk", sender="alice@example.com",
        subject="Alert\n`evil`", attachment_filenames=["doc\n`bad`.pdf"],
    )
    plan = inbox_plan_renderer.render_action_required_plan(
        dirty_msg, "Check", ["Point"], "01_doc.md", drive_watcher_enabled=True,
        attachments_to_save=[AttachmentFilingTarget(filename="doc\n`bad`.pdf", parent_id="fld_1")],
    )
    del_line = next(ln for ln in plan.splitlines() if "alfred delegate --to drive" in ln)
    assert del_line.endswith("`") and ": `" in del_line
    cmd = del_line.split(": `", 1)[1][:-1]
    assert "\n" not in cmd and "\r" not in cmd and "`" not in cmd
    paths = dom.AlfredPaths.from_env(tmp_path)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path.write_text("features:\n  drive_watcher: true\n", encoding="utf-8")
    hook_in = json.dumps({
        "hook_event_name": "PreToolUse", "tool_name": "run_command",
        "cwd": str(WORKTREE_ROOT / "core" / "workspaces" / "alfred-inbox"),
        "tool_input": {"CommandLine": cmd},
    })
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)], input=hook_in, text=True, capture_output=True,
        env={**os.environ, "ALFRED_ROOT_DIR": str(tmp_path)}, check=False,
    )
    assert proc.returncode == 0 and json.loads(proc.stdout)["decision"] == "allow"


def test_red_team_hardened_noreply_guard_and_reply_to_exception(tmp_path: Path) -> None:
    """Verify expanded automated local-part detection, header guard, & Reply-To exception."""
    for addr in (
        "no.reply@example.com", "do.not.reply@example.com", "ne.pas.repondre@example.com",
        "noreply123@example.com", "notifications-bot@example.com", "alerts@example.com",
        "human@example.com, noreply@portal.example.com",
    ):
        assert is_automated_correspondent_address(addr) is True, addr

    def _raw(headers: list[dict[str, str]], msg_id: str) -> dict[str, Any]:
        """Build raw Gmail message dict with PDF attachment."""
        parts = [
            {"mimeType": "text/plain", "body": {"data": _b64u(b"Statement ready.")}},
            {"mimeType": "application/pdf", "filename": "statement.pdf",
             "headers": [{"name": "Content-Disposition", "value": "attachment"}],
             "body": {"data": _b64u(b"%PDF-1.4")}},
        ]
        return {"id": msg_id, "threadId": f"th_{msg_id}", "labelIds": ["INBOX"],
                "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}}

    def _draft_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return DRAFT_REPLY decision from cognitive classifier."""
        p = {"conversation_id": "c-nr", "structured_output": {
            "output_type": "DRAFT_REPLY", "reason": "Reply", "draft_body": "Received, thanks."}}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(p), "")

    b_hdrs = [{"name": "From", "value": "billing@portal.example.com"},
              {"name": "Subject", "value": "Monthly billing"},
              {"name": "Auto-Submitted", "value": "auto-generated"}]
    b_raw = _raw(b_hdrs, "m_bill")
    t_bill, g_bill, d_bill = _wire_triage(
        tmp_path / "bill", b_raw, _draft_runner, allowlist_domains=["portal.example.com"]
    )
    dec_bill = t_bill.process_message(g_bill._parse_raw_message(b_raw), use_cognitive_agy=True)
    assert dec_bill.output_type == dom.InboxOutputType.USER_ACTION_REQUIRED
    assert dec_bill.draft_id is None and dec_bill.starred is True and d_bill == []

    rt_hdrs = [{"name": "From", "value": "noreply@portal.example.com"},
               {"name": "Reply-To", "value": "human@partner.example.org"},
               {"name": "Subject", "value": "Partner question"}]
    rt_raw = _raw(rt_hdrs, "m_rt")
    t_rt, g_rt, d_rt = _wire_triage(
        tmp_path / "rt", rt_raw, _draft_runner, allowlist_domains=["portal.example.com"]
    )
    dec_rt = t_rt.process_message(g_rt._parse_raw_message(rt_raw), use_cognitive_agy=True)
    assert dec_rt.output_type == dom.InboxOutputType.DRAFT_REPLY
    assert dec_rt.draft_id is not None and len(d_rt) == 1


def test_red_team_human_non_doc_attachment_cognitive_action_and_template(tmp_path: Path) -> None:
    """Verify human non-doc attachment retention, impaye promotion, and template uniqueness."""
    jpg_raw: dict[str, Any] = {
        "id": "m_jpg", "threadId": "th_jpg", "labelIds": ["INBOX"],
        "payload": {"mimeType": "multipart/mixed", "headers": [
            {"name": "From", "value": "alice@example.com"},
            {"name": "Subject", "value": "Action requise receipt"}], "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64u(b"Action requise.")}},
            {"mimeType": "image/jpeg", "filename": "receipt_photo.jpg",
             "headers": [{"name": "Content-Disposition", "value": "attachment"}],
             "body": {"attachmentId": "att-jpg", "size": 6}}]}}
    j_out = json.dumps({"conversation_id": "c-jpg", "structured_output": {
        "output_type": "USER_ACTION_REQUIRED", "reason": "Save photo"}})
    t_jpg, g_jpg, _ = _wire_triage(
        tmp_path / "jpg", jpg_raw, lambda c: subprocess.CompletedProcess(c, 0, j_out, ""),
        blobs={"att-jpg": b"\xff\xd8\xff\xe0\x00\x10"},
    )
    parsed_jpg = g_jpg._parse_raw_message(jpg_raw)
    assert parsed_jpg.attachment_filenames == ["receipt_photo.jpg"]
    dec_jpg = t_jpg.process_message(parsed_jpg, use_cognitive_agy=True)
    plan_jpg = dec_jpg.implementation_plan_markdown or ""
    assert "receipt_photo.jpg" in plan_jpg and "alfred save-attachment-to-drive" in plan_jpg

    impaye_raw: dict[str, Any] = {
        "id": "m_imp", "threadId": "th_imp", "labelIds": ["INBOX"],
        "payload": {"mimeType": "text/plain", "body": {"data": _b64u(b"Avis impaye facture.")},
                    "headers": [{"name": "From", "value": "alice@example.com"},
                                {"name": "Subject", "value": "Notification impaye"}]}}
    i_out = json.dumps({"conversation_id": "c-imp", "structured_output": {
        "output_type": "SILENT_NO_ACTION", "reason": "Misclassified"}})
    t_imp, g_imp, _ = _wire_triage(
        tmp_path / "imp", impaye_raw, lambda c: subprocess.CompletedProcess(c, 0, i_out, ""),
    )
    dec_imp = t_imp.process_message(g_imp._parse_raw_message(impaye_raw), use_cognitive_agy=True)
    assert dec_imp.output_type == dom.InboxOutputType.USER_ACTION_REQUIRED and dec_imp.starred
    tpl_path = WORKTREE_ROOT / "core" / "templates" / "prompts" / "inbox_triage.md"
    assert tpl_path.read_text(encoding="utf-8").count("{{custom_inbox_rules}}") == 1


def _drive_env(
    root: Path, client: Any
) -> tuple[
    StateStore, dom.AlfredSettings, DriveService, DriveGovernanceService, DriveWatcherService
]:
    """Build isolated DriveService, DriveGovernanceService, and DriveWatcherService."""
    paths = dom.AlfredPaths.from_env(root)
    store = StateStore(paths)
    cfg = dom.AlfredSettings(
        features=FeatureFlagsConfig(drive_knowledge_sync=True, drive_watcher=True)
    )
    drive = DriveService(paths, cfg, store, drive_client=client)
    gov = DriveGovernanceService(cfg, store, drive)
    watcher = DriveWatcherService(
        paths, cfg, store, drive, gov, KnowledgeService(paths, cfg, store),
        AgySessionManager(paths, cfg, store, antigravity_data_dir=root / "agy"),
    )
    return store, cfg, drive, gov, watcher


def test_red_team_drive_mutual_lock_anti_resurrection(tmp_path: Path) -> None:
    """Verify mutual lock serializes sync_changes and watcher so f1 cannot resurrect."""
    sync_entered, allow_finish = threading.Event(), threading.Event()
    seq, guard = {"n": 0}, threading.Lock()
    act_f1 = {"fileId": "f1", "removed": False, "file": {
        "id": "f1", "name": "Doc.pdf", "mimeType": "application/pdf",
        "parents": ["fld_1"], "modifiedTime": "2026-11-25T10:00:00Z", "ownedByMe": True,
    }}
    del_f1 = {"fileId": "f1", "removed": True, "file": {"id": "f1", "trashed": True}}

    def _changes_list(**_kw: Any) -> SimpleNamespace:
        """Return active f1 for first call (blocking until released), trashed f1 for second."""
        def _exec() -> dict[str, Any]:
            """Execute synchronized fake Drive changes.list call."""
            with guard:
                seq["n"] += 1
                num = seq["n"]
            if num == 1:
                sync_entered.set()
                allow_finish.wait(timeout=5.0)
                return {"changes": [act_f1], "newStartPageToken": "tok_2"}
            return {"changes": [act_f1, del_f1], "newStartPageToken": "tok_3"}

        return SimpleNamespace(execute=_exec)

    store, _, drive, _, watcher = _drive_env(
        tmp_path, SimpleNamespace(changes=lambda: SimpleNamespace(list=_changes_list))
    )
    fld = dom.DriveEntry(file_id="fld_1", name="01_Admin", mime_type=FOLDER_MIME,
                         path="/01_Admin", is_folder=True)
    f1 = dom.DriveEntry(file_id="f1", name="Doc.pdf", mime_type="application/pdf",
                        parents=["fld_1"], path="/01_Admin/Doc.pdf")
    store.drive_index.save(dom.DriveMetadataIndex(start_page_token="tok_1",
                                                  entries={"fld_1": fld, "f1": f1}))
    store.bookmarks.save(dom.GoogleSyncBookmarks(drive_page_token="tok_1",
                                                 drive_watcher_page_token="tok_1"))
    t1, t2 = threading.Thread(target=drive.sync_changes), threading.Thread(
        target=watcher.collect_delta_report
    )
    t1.start()
    assert sync_entered.wait(timeout=5.0)
    t2.start()
    time.sleep(0.05)
    allow_finish.set()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert not t1.is_alive() and not t2.is_alive()
    final_idx = store.drive_index.load()
    assert "f1" not in final_idx.entries and final_idx.start_page_token == "tok_3"


def test_red_team_drive_reentrant_init_no_deadlock(tmp_path: Path) -> None:
    """Verify collect_delta_report & sync_changes calling init_drive() never deadlock."""
    init_cli = SimpleNamespace(
        files=lambda: SimpleNamespace(list=lambda **_kw: _box({"files": []})),
        changes=lambda: SimpleNamespace(
            getStartPageToken=lambda **_kw: _box({"startPageToken": "t0"})
        ),
    )
    s_in, _, _, _, w_in = _drive_env(tmp_path / "re", init_cli)
    t_re = threading.Thread(target=w_in.collect_delta_report)
    t_re.start()
    t_re.join(timeout=3.0)
    assert not t_re.is_alive() and s_in.drive_index.load().start_page_token == "t0"

    s_sy, _, d_sy, _, _ = _drive_env(tmp_path / "re_sync", init_cli)
    t_sy = threading.Thread(target=d_sy.sync_changes)
    t_sy.start()
    t_sy.join(timeout=3.0)
    assert not t_sy.is_alive() and s_sy.drive_index.load().start_page_token == "t0"


def test_red_team_drive_governance_atomic_and_none_tokens(tmp_path: Path) -> None:
    """Verify governance index update atomicity and None page token loop protection."""
    def _gov_update(**_kw: Any) -> SimpleNamespace:
        """Inject concurrent attachment & start_page_token before governance saves index."""
        def _exec() -> dict[str, Any]:
            """Execute update and concurrent drive_index write."""
            conc = dom.DriveEntry(file_id="att_conc", name="conc.pdf", mime_type="application/pdf",
                                  parents=["fld_1"], path="/01_Admin/conc.pdf")
            store.drive_index.update(lambda cur: cur.model_copy(
                update={"start_page_token": "tok_9", "entries": {**cur.entries, "att_conc": conc}}
            ))
            return {"id": "f1", "name": "New.pdf", "mimeType": "application/pdf",
                    "parents": ["fld_1"], "modifiedTime": "2026-11-25T10:05:00Z"}
        return SimpleNamespace(execute=_exec)

    gov_cli = SimpleNamespace(files=lambda: SimpleNamespace(
        update=_gov_update, get=lambda **_kw: _box({"id": "fld_1"})))
    store, _, _, gov, _ = _drive_env(tmp_path, gov_cli)
    ents = {
        "fld_1": dom.DriveEntry(file_id="fld_1", name="01_Admin", mime_type=FOLDER_MIME,
                                path="/01_Admin", is_folder=True),
        "f1": dom.DriveEntry(file_id="f1", name="Old.pdf", mime_type="application/pdf",
                             parents=["fld_1"], path="/01_Admin/Old.pdf"),
    }
    store.drive_index.save(dom.DriveMetadataIndex(start_page_token="tok_1", entries=ents))
    gov.rename_file("f1", "New.pdf")
    idx_gov = store.drive_index.load()
    assert "att_conc" in idx_gov.entries and idx_gov.start_page_token == "tok_9"

    none_calls = {"n": 0}

    def _none_list(**_kw: Any) -> SimpleNamespace:
        """Return None tokens and guard against infinite loops in unfixed code."""
        none_calls["n"] += 1
        assert none_calls["n"] <= 3, "infinite loop on nextPageToken=None"
        return _box({"changes": [], "nextPageToken": None, "newStartPageToken": None})

    none_cli = SimpleNamespace(changes=lambda: SimpleNamespace(list=_none_list))
    _, _, none_drv, _, _ = _drive_env(tmp_path / "none", none_cli)
    chgs, tok = none_drv._fetch_changes_pages(none_cli, "tok_1")
    assert none_calls["n"] == 1 and chgs == [] and tok == "tok_1"


def test_red_team_drive_empty_sync_cursor_and_orphan_filter(tmp_path: Path) -> None:
    """Verify sync_changes on empty index preserves watcher token & drops external orphans."""
    emp_cli = SimpleNamespace(
        files=lambda: SimpleNamespace(list=lambda **_kw: _box({"files": []})),
        changes=lambda: SimpleNamespace(
            getStartPageToken=lambda **_kw: _box({"startPageToken": "tok_8"})),
    )
    s_emp, _, d_emp, _, _ = _drive_env(tmp_path / "empty_sync", emp_cli)
    s_emp.bookmarks.save(dom.GoogleSyncBookmarks(drive_watcher_page_token="tok_5"))
    d_emp.sync_changes()
    assert s_emp.bookmarks.load().drive_watcher_page_token == "tok_5"

    orp_cli = SimpleNamespace(changes=lambda: SimpleNamespace(list=lambda **_kw: _box({"changes": [
        {"fileId": "f_ext_orphan", "removed": False, "file": {
            "id": "f_ext_orphan", "name": "Ext.pdf", "mimeType": "application/pdf",
            "parents": ["unmounted_ext"], "ownedByMe": False, "shared": True, "trashed": False}},
        {"fileId": "f_owned", "removed": False, "file": {
            "id": "f_owned", "name": "Own.pdf", "mimeType": "application/pdf",
            "parents": ["fld_1"], "ownedByMe": True, "trashed": False}},
    ], "newStartPageToken": "tok_2"})))
    s_orp, _, d_orp, _, _ = _drive_env(tmp_path / "orphan", orp_cli)
    fld = dom.DriveEntry(file_id="fld_1", name="01_Admin", mime_type=FOLDER_MIME,
                         path="/01_Admin", is_folder=True)
    s_orp.drive_index.save(dom.DriveMetadataIndex(start_page_token="tok_1", entries={"fld_1": fld}))
    d_orp.sync_changes()
    orp_entries = s_orp.drive_index.load().entries
    assert "f_ext_orphan" not in orp_entries and "f_owned" in orp_entries
