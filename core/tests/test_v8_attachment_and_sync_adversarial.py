"""Adversarial Red-Team regression suite for V8 attachments, security hook, and daemon sync."""

from __future__ import annotations

import base64
import io
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from alfred.controllers.app_factory import build_cli_controller
from alfred.domain import models as dom
from alfred.domain.feature_models import SecurityConfig
from alfred.repositories.state_store import StateStore
from alfred.services import agy_daemon_sync as ads
from alfred.services import attachment_extractor as ax
from alfred.services.agy_archive_store import AgyArchiveStore, build_cognitive_triage_prompt
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.delegation_service import DelegationService
from alfred.services.drive_service import FOLDER_MIME, DriveService
from alfred.services.email_security_service import EmailSanitizer, InboxCapabilityFirewall
from alfred.services.evolve_worktree_service import EvolveWorktreeService
from alfred.services.gmail_drive_bridge_service import GmailDriveBridgeService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_calendar_bridge import render_action_required_plan
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService

REPO_ROOT = Path(__file__).resolve().parents[2]


def _hook(tool_name: str, tool_input: Mapping[str, object], cwd: str) -> dict[str, object]:
    """Run security_pre_tool_hook.py subprocess and return parsed JSON decision.

    Args:
        tool_name: Simulated Antigravity tool name.
        tool_input: Tool input arguments mapping.
        cwd: Simulated workspace working directory.
    """
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": tool_name,
                          "cwd": cwd, "tool_input": tool_input})
    cmd = [sys.executable, str(REPO_ROOT / "core" / "scripts" / "security_pre_tool_hook.py")]
    proc = subprocess.run(cmd, input=payload, text=True, capture_output=True, check=False)
    assert proc.returncode == 0, f"Hook crashed ({proc.returncode}): {proc.stderr}"
    return dict(json.loads(proc.stdout))


def _att(mime: str, fname: str, raw: bytes = b"", disp: str = "attachment") -> dict[str, Any]:
    """Build a Gmail MIME attachment part dictionary.

    Args:
        mime: Attachment MIME type.
        fname: Attachment filename.
        raw: Inline attachment bytes.
        disp: Content-Disposition header value.
    """
    b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return {"mimeType": mime, "filename": fname, "body": {"data": b64, "size": len(raw)},
            "headers": [{"name": "Content-Disposition", "value": disp}]}


def _db(agy_dir: Path, *cids: str, title: str = "", running: bool = False,
        bad_fts: bool = False) -> Path:
    """Create or update conversation_summaries.db and optional FTS/.db row artifacts.

    Args:
        agy_dir: Antigravity CLI data directory.
        *cids: Optional conversation UUIDs to upsert.
        title: Conversation summary title.
        running: Mark conversation rows as CASCADE_RUN_STATUS_RUNNING.
        bad_fts: Omit archived column from conversations_fts and skip FTS insert.
    """
    (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
    db_path = agy_dir / "conversation_summaries.db"
    fts = "(conversation_id TEXT,title TEXT" + ("" if bad_fts else ",archived INT DEFAULT 0") + ")"
    st = f"CASCADE_RUN_STATUS_{'RUNNING' if running else 'IDLE'}"
    with sqlite3.connect(db_path) as c:
        c.execute("CREATE TABLE IF NOT EXISTS conversation_summaries(conversation_id TEXT "
                  "PRIMARY KEY,title TEXT DEFAULT '',project_id TEXT DEFAULT '',status TEXT "
                  "DEFAULT 'CASCADE_RUN_STATUS_IDLE',not_fully_idle INTEGER DEFAULT 0,"
                  "step_count INTEGER DEFAULT 0)")
        c.execute(f"CREATE TABLE IF NOT EXISTS conversations_fts{fts}")
        for cid in cids:
            (agy_dir / "conversations" / f"{cid}.db").write_bytes(b"db")
            c.execute("INSERT OR REPLACE INTO conversation_summaries VALUES(?,?,?,?,?,0)",
                      (cid, title, "p1", st, int(running)))
            if not bad_fts:
                c.execute("INSERT INTO conversations_fts VALUES(?,?,0)", (cid, title))
    return db_path


def _triage_env(
    tmp_path: Path, payload: object = None, prot_ids: list[str] | None = None,
    *, with_gmail: bool = True,
) -> tuple[InboxTriageService, GmailDriveBridgeService, StateStore, MagicMock, MagicMock]:
    """Wire InboxTriageService, GmailDriveBridgeService, and mocked Google clients.

    Args:
        tmp_path: Pytest temporary root path.
        payload: Raw Gmail message dict returned by messages().get().
        prot_ids: Protected Drive vault IDs.
        with_gmail: When False, construct GmailService with gmail_client=None (no OAuth).
    """
    paths = dom.AlfredPaths.from_env(tmp_path)
    store, sec = StateStore(paths), SecurityConfig(protected_signature_drive_ids=prot_ids or [])
    cfg, gm_cli, drv_cli = dom.AlfredSettings(security=sec), MagicMock(), MagicMock()
    gm_cli.users().getProfile().execute.return_value = {"emailAddress": "owner@example.com"}
    gm_cli.users().messages().get().execute.return_value = payload
    gmail = GmailService(paths, cfg, gmail_client=gm_cli if with_gmail else None)
    drive = DriveService(paths, cfg, store, drive_client=drv_cli)
    agy = AgySessionManager(paths, cfg, store, agy_locator_fn=lambda: None)
    bridge = GmailDriveBridgeService(cfg, store, gmail, drive)
    return (InboxTriageService(store, gmail, KnowledgeService(paths, cfg, store),
                               drive, agy, settings=cfg, attachment_bridge=bridge),
            bridge, store, gm_cli, drv_cli)


def test_hook_evasion_evolve_secrets_quote_split_and_null_path() -> None:
    """Verify hook blocks ANSI-C/proc-sub, evolve bypass, secrets, quote-split, and null path."""
    ws = REPO_ROOT / "core" / "workspaces"
    inbox, life, mcp = str(ws / "alfred-inbox"), str(ws / "alfred-life"), str(ws / "alfred-mcp")
    up = "alfred upsert-knowledge --file k.md --id k --title T --domain D --summary S --body-file "
    for cmd in (
        "alfred list-attachments --message-id $'\\'' >(id) #'",
        "alfred read-attachment --message-id m1 --filename <(id)",
        "alfred delegate --to evolve --title T --brief S",
        "alfred delegate --to evolve --title T --brief S --source-workspace alfred-life",
        f"{up}/tmp/instance/config/settings.yaml", f"{up}/tmp/instance/secrets/custom.json",
    ):
        assert _hook("run_command", {"CommandLine": cmd}, inbox)["decision"] == "deny"
    for cmd in ("alfred read-drive --file-id d1", "alfred scan-knowledge",
                "alfred load-knowledge --doc 01.md", "alfred list-tasklists"):
        assert _hook("run_command", {"CommandLine": cmd}, inbox)["decision"] == "allow"
    sig = "alfred apply''-signature --request-id req_1"
    for wd, want in ((life, "force_ask"), (inbox, "deny"), (mcp, "deny")):
        assert _hook("run_command", {"CommandLine": sig}, wd)["decision"] == want
    for t, k, p, wd in (("read_file", "Path", "/tmp/instance/secrets/client_secret.json", life),
                        ("write_file", "TargetFile", "/tmp/.gemini/brain/evil.py", inbox),
                        ("write_file", "TargetFile", "/tmp/.gemini/brain/a\x00/plan.md", inbox)):
        assert _hook(t, {k: p}, wd)["decision"] == "deny"


def test_sanitizer_firewall_and_extractor_security() -> None:
    """Verify EmailSanitizer, Firewall SPF/domain/CRLF, and extractor XXE/XHTML/BiDi guards."""
    san = EmailSanitizer()
    raw_html = ('<b style="display:none">LEAK_B</b><h1 style="display:none">LEAK_H1</h1>'
                '<li style="display:none">LEAK_LI</li><div style="display&#58;none">LEAK_ENT</div>'
                '<div style="display:none"><div>inner</div>LEAK_NEST</div><p>Visible</p>')
    res = san.sanitize(raw_html, "Bon\u00adjour\u034f")
    assert res.suspicious_injection is True
    for leak in ("LEAK_B", "LEAK_H1", "LEAK_LI", "LEAK_NEST", "LEAK_ENT", "\u00ad", "\u034f"):
        assert leak not in res.clean_text
    ok = san.sanitize('<div style="opacity: 0.9">OK9</div><p class="not-hidden x">OKC</p>', "")
    assert not ok.suspicious_injection and "OK9" in ok.clean_text and "OKC" in ok.clean_text
    fw, owners = InboxCapabilityFirewall(), frozenset({"owner@domaine-perso.example"})
    base = dom.EmailMessage(message_id="m", thread_id="t",
                            sender="owner@domaine-perso.example", subject="Evo")
    au, mc, de = "Authentication-Results", base.model_copy, fw.can_delegate_to_evolve
    for hdr in ('mx; spf=fail; x-note="spf=pass"', 'mx; dkim=fail; x-note="dkim=pass"',
                "mx; spf=pass smtp.mailfrom=bounce@attacker.com",
                "mx; dkim=pass header.d=attacker.com"):
        assert not de(mc(update={"headers": {au: hdr}}), owners)
    assert de(mc(update={"headers": {au: "mx; spf=pass; dkim=none"}}), owners)
    p_rt = fw.pin_draft_recipient(mc(update={"headers": {"Reply-To": "u@example.com\r\nBcc: x"}}))
    p_sd = fw.pin_draft_recipient(mc(update={"sender": "u@example.com\nBcc: x"}))
    assert "\r" not in p_rt and "\n" not in p_rt and "\r" not in p_sd and "\n" not in p_sd
    xxe = ('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE foo [<!ENTITY xxe "leak">]>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body></w:document>')
    for raw_xml in (b"\xff\xfe" + xxe.encode("utf-16-le"), xxe.encode("utf-16-le")):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", raw_xml)
        method, txt, _ = ax.extract_attachment_text(buf.getvalue(), "x.docx", ax.DOCX_MIME)
        assert method == "corrupted_or_unsafe_archive" and "leak" not in txt
    html_raw = '<div style="display:none">HIDDEN_HTML</div><p>Bon\u200bjour\u00ad</p>'.encode()
    for fname, mime in (("a.html", "text/html"), ("a.xhtml", "application/octet-stream")):
        meth, txt, _ = ax.extract_attachment_text(html_raw, fname, mime)
        assert meth == "html_plain_text" and "HIDDEN_HTML" not in txt
        assert "\u200b" not in txt and "\u00ad" not in txt and "Bonjour" in txt
    cell = ET.fromstring('<c t="s"><v>\u00b2</v></c>')
    assert ax._extract_xlsx_cell_value(cell, ["s"]) == "\u00b2"
    assert all(ax.decode_base64url_bytes(b) == b"" for b in ("abcde", "ab!@", "ab\xe9"))
    parts = ax.collect_attachment_parts("m1", {"mimeType": "multipart/mixed", "parts": [
        {"mimeType": "text/plain", "filename": "a.txt", "body": {"size": "abc", "data": ""}},
        {"mimeType": "text/plain", "filename": "b.txt", "body": {"size": {"a": 1}, "data": ""}},
    ]})
    assert [m.size_bytes for m, _ in parts] == [0, 0]
    fn = "..\uff0fdir\u2215a\u202e\u2028\u2029\u200b\x7f`b.pdf"
    assert ax.sanitize_attachment_filename(fn) == "ab.pdf"
    with pytest.raises(ValueError):
        ax.extract_attachment_text(b"hello", "a.txt", "text/plain", max_chars=-1)


def test_bridge_html_charset_no_oauth_http503_human_mail_and_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify charset HTML, absent OAuth, HTTP 503 resilience, newsletter PJ, and prompt.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch helper.
    """
    att = _att("text/html; charset=utf-8", "part", b'<div style="display:none">LEAK</div><p>OK</p>')
    triage, bridge, _, gm_h, _ = _triage_env(tmp_path / "h", {
        "id": "m_h", "payload": {"mimeType": "multipart/mixed", "parts": [att]}})
    msg = dom.EmailMessage(message_id="m_h", thread_id="th", sender="a@example.com",
                           subject="Note", body_text="Intro", attachment_filenames=["part"])
    enr, bullets, inj = bridge.enrich_email_with_attachments(msg, triage._sanitizer)
    assert inj and "LEAK" not in (enr.body_text + " ".join(bullets)) and "OK" in enr.body_text
    _, no_auth, _, _, _ = _triage_env(tmp_path / "na", with_gmail=False)
    assert no_auth.enrich_email_with_attachments(msg, triage._sanitizer) == (msg, [], False)
    t503, _, store, gm_cli, _ = _triage_env(tmp_path / "e503")
    store.bookmarks.update(lambda b: b.model_copy(update={"gmail_history_id": "100"}))
    gm_cli.users().messages().get().execute.side_effect = RuntimeError("HTTP 503")
    monkeypatch.setattr(t503._gmail, "list_new_messages_since", lambda _h: ([msg], "250"))
    t503.poll_new_messages()
    assert store.bookmarks.load().gmail_history_id == "250"
    att_nl = _att("text/plain", "a.txt", b"Annexe : newsletter et se desabonner en pied.")
    gm_h.users().messages().get().execute.return_value = {"id": "m_nl", "payload": {
        "mimeType": "multipart/mixed", "parts": [att_nl]}}
    m_nl = msg.model_copy(update={"message_id": "m_nl", "sender": "c@example.com",
                                  "body_text": "Ok ?", "attachment_filenames": ["a.txt"]})
    assert triage.process_message(m_nl).output_type == dom.InboxOutputType.DRAFT_REPLY
    enr_nl, _, _ = bridge.enrich_email_with_attachments(m_nl, triage._sanitizer)
    assert triage._classify_message(enr_nl, [])[0] != dom.InboxOutputType.SILENT_NO_ACTION
    b_att = ("A" * 4500) + "\n\n[Pi\u00e8ce jointe: devis.pdf]\nMontant"
    prompt = build_cognitive_triage_prompt(m_nl.model_copy(update={"body_text": b_att}), [])
    assert "[Pi\u00e8ce jointe: devis.pdf]" in prompt and "Montant" in prompt


def test_transfer_guards_plan_flag_and_cli_controller(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify transfer guards, --filename multi-PJ plan hint, and CliController _gmail update.

    Args:
        tmp_path: Pytest temporary directory.
        capsys: Pytest stdout/stderr capture fixture.
    """
    pl_t = {"id": "m_t", "payload": {"mimeType": "multipart/mixed",
                                      "parts": [_att("text/plain", "f.txt", b"ok")]}}
    _, bridge, _, gm_t, drv = _triage_env(tmp_path / "t", pl_t, ["prot-parent", "prot-dup"])
    fapi, tx = drv.files.return_value, bridge.transfer_attachment_to_drive
    fl, fc, fg = fapi.list().execute, fapi.create().execute, fapi.get().execute
    fg.return_value = (fld := {"id": "d1", "mimeType": FOLDER_MIME, "trashed": False})
    for pid, tname in (("d1", "../../"), ("", "")):
        with pytest.raises(ValueError):
            tx("m_t", pid, target_name=tname)
    fl.return_value = {"files": [{"id": "fld1", "name": "f.txt", "mimeType": FOLDER_MIME}]}
    fc.return_value = {"id": "c1", "name": "f.txt", "size": ""}
    assert tx("m_t", "d1").size_bytes == 2
    for fn, ret, exc, over in (
        (fc, {"name": "f.txt"}, ValueError, False),
        (fl, {"files": [{"id": "1", "name": "f.txt"}, {"id": "prot-dup", "name": "f.txt"}]},
         PermissionError, True),
        (fg, {**fld, "parents": ["prot-parent"]}, PermissionError, False),
    ):
        fn.return_value = ret
        with pytest.raises(exc):
            tx("m_t", "d1", overwrite=over)
    gm_t.users().messages().get().execute.return_value = {"id": "m_i", "payload": {
        "mimeType": "multipart/mixed", "parts": [_att("image/png", "l.png", b"p", disp="inline")]}}
    with pytest.raises(ValueError):
        bridge.read_message_attachment("m_i")
    msg2 = dom.EmailMessage(message_id="m2", thread_id="th2", sender="a@example.com",
                            subject="S", attachment_filenames=["a.pdf", "b.pdf"])
    assert "--filename" in render_action_required_plan(msg2, "Raison", ["bullet"], "doc1")
    paths, gm_cli = dom.AlfredPaths.from_env(tmp_path / "cli"), MagicMock()
    ctrl = build_cli_controller(paths=paths)
    gm_cli.users().messages().get().execute.return_value = pl_t
    ctrl._gmail = GmailService(paths, dom.AlfredSettings(), gmail_client=gm_cli)
    ctrl._drive = bridge._drive
    assert ctrl.run(["list-attachments", "--message-id", "m_t"]) == 0
    assert "f.txt" in capsys.readouterr().out


def test_daemon_sync_concurrent_dirty_fail_closed_busy_and_corrupt_json(tmp_path: Path) -> None:
    """Verify interleaved dirty preservation, SQLITE_BUSY fail-closed, and corrupt JSON.

    Args:
        tmp_path: Pytest temporary directory.
    """
    paths, agy_dir = dom.AlfredPaths.from_env(tmp_path), tmp_path / "agy_data"
    c1, c2 = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
    db_path = _db(agy_dir, c1, c2)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.state_dir / "agy_daemon_sync_pending.json").write_text("{corrupt", encoding="utf-8")
    svc = ads.AgyDaemonSyncService(paths.state_dir, agy_dir)

    def _run_sys(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Inject concurrent mark_dirty(c2) on daemon restart and return active status.

        Args:
            cmd: Systemctl argv tokens.
        """
        if "restart" in cmd:
            svc.mark_dirty(c2, reason="interleaved")
        return subprocess.CompletedProcess(cmd, 0, "active\n", "")

    svc._systemctl_runner = _run_sys
    assert svc.load_state().pending is False and svc.mark_dirty(c1) is True
    assert svc.sync_if_needed().status == ads.DaemonSyncStatus.SYNCED
    assert svc.load_state().pending is True and svc.load_state().conversation_ids == [c2]
    lock_conn = sqlite3.connect(db_path)
    lock_conn.execute("BEGIN EXCLUSIVE")
    try:
        assert svc.sync_if_needed().status == ads.DaemonSyncStatus.DEFERRED_ACTIVE_TURN
    finally:
        lock_conn.close()


def test_daemon_sync_stale_traversal_brain_mtime_grace_and_worker_env(tmp_path: Path) -> None:
    """Verify traversal guard, recursive brain mtime, >=700s default grace, bounds, and env.

    Args:
        tmp_path: Pytest temporary directory.
    """
    paths, agy_dir = dom.AlfredPaths.from_env(tmp_path), tmp_path / "agy_data"
    c620, old = "33333333-3333-3333-3333-333333333333", time.time() - 900
    _db(agy_dir, c620)
    esc = agy_dir / "escape.db"
    esc.write_bytes(b"esc")
    os.utime(esc, (old, old))
    svc = ads.AgyDaemonSyncService(paths.state_dir, agy_dir)
    assert svc._stale_running_grace_sec >= 700
    assert not svc._is_stale_running_conversation("../escape")
    c620_db = agy_dir / "conversations" / f"{c620}.db"
    os.utime(c620_db, (time.time() - 620, time.time() - 620))
    assert svc._is_stale_running_conversation(c620) is False
    sub = agy_dir / "brain" / c620 / "sub"
    sub.mkdir(parents=True)
    (sub / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    for p in (c620_db, agy_dir / "brain" / c620):
        os.utime(p, (old, old))
    s300 = ads.AgyDaemonSyncService(paths.state_dir, agy_dir, stale_running_grace_sec=300)
    assert s300._is_stale_running_conversation(c620) is False
    for bad in (math.inf, math.nan, -1.0):
        with pytest.raises(ValueError):
            svc.wait_until_idle_and_sync(timeout_sec=bad)
        with pytest.raises(ValueError):
            svc.wait_until_idle_and_sync(poll_interval_sec=bad)
    envs: list[dict[str, str]] = []

    def _rec_popen(_cmd: list[str], **kw: Any) -> Any:
        """Capture Popen env dict for deferred sync worker assertions.

        Args:
            _cmd: Worker argv tokens.
            **kw: Popen keyword arguments.
        """
        envs.append(dict(kw.get("env") or {}))
        return MagicMock()

    assert svc.spawn_deferred_sync_worker(alfred_root=tmp_path, popen_fn=_rec_popen) is True
    assert envs[0].get("ALFRED_ROOT_DIR") == str(tmp_path.resolve())
    assert str((tmp_path / "core").resolve()) in envs[0].get("PYTHONPATH", "")


def test_delegation_step_count_snapshot_guard_envelope_and_plan_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify step_count schema gate, failed snapshot guard, rc=0 error purge, and timeout=600.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch helper.
    """
    paths, agy_dir = dom.AlfredPaths.from_env(tmp_path), tmp_path / "agy_data"
    settings, store = dom.AlfredSettings(), StateStore(paths)
    live_id = "44444444-4444-4444-4444-444444444444"
    db_path = _db(agy_dir, live_id, running=True)
    with sqlite3.connect(db_path) as cn:
        cn.execute("ALTER TABLE conversation_summaries DROP COLUMN step_count")
    arc = AgyArchiveStore(agy_dir)
    assert arc.purge_incomplete_stubs({"other"}) == []
    assert live_id in arc.list_summary_conversation_ids()
    db_path.write_bytes(b"corrupt")
    agy = AgySessionManager(paths, settings, store, agy_locator_fn=lambda: "/usr/bin/agy",
                            antigravity_data_dir=agy_dir)
    q: list[tuple[str, str | None]] = [("restore", None), ("stub1", '{"status":"ERROR"'),
                                       ("stub2", '{"status":"TIMEOUT","conversation_id":"s2"}'),
                                       ("plan", '{"conversation_id":"p1"}')]
    timeouts: list[int] = []

    def _mock_agy(*_args: Any, timeout: int = 180, **_kw: Any) -> Any:
        """Simulate agy outcomes for snapshot guard, rc=0 error stubs, and timeout capture.

        Args:
            *_args: Positional command/cwd args.
            timeout: Subprocess timeout in seconds.
            **_kw: Additional keyword arguments.
        """
        timeouts.append(timeout)
        tag, raw = q.pop(0)
        if tag == "restore":
            db_path.unlink(missing_ok=True)
            _db(agy_dir, live_id, running=True)
        elif tag.startswith("stub"):
            _db(agy_dir, tag, running=True)
        return None if raw is None else subprocess.CompletedProcess(["agy"], 0, raw, "")

    monkeypatch.setattr(agy, "_run_agy_command", _mock_agy)
    dlg = DelegationService(paths, settings, store, agy_manager=agy)
    for title in ("Snap", "E1", "E2"):
        dlg.delegate(target="evolve", title=title, brief="B")
    rem = arc.list_summary_conversation_ids()
    assert live_id in rem and "stub1" not in rem and "stub2" not in rem
    agy._spawn_live_plan_session("alfred-evolve", "m1", "# Plan")
    assert timeouts[-1] == 600


def test_archive_projects_fts_dirty_all_and_evolve_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify archive JSON/FTS/dirty-all resilience and EvolveWorktreeService mypy/env gates.

    Args:
        tmp_path: Pytest temporary directory.
        monkeypatch: Pytest monkeypatch helper.
    """
    paths, agy_dir, pd = dom.AlfredPaths.from_env(tmp_path), tmp_path / "agy_data", tmp_path / "p"
    pd.mkdir(parents=True)
    (pd / "bad.json").write_bytes(b"\xff\xfe\x00")
    (pd / "blank.json").write_text('{"id":"p1","name":"   ","slug":"alfred-evolve"}')
    (pd / "empty.json").write_text('{"id":"p2","name":"   "}')
    arc = AgyArchiveStore(agy_dir, pd)
    assert arc._load_project_names() == {"p1": "alfred-evolve"}
    c1, c2 = "55555555-5555-5555-5555-555555555555", "66666666-6666-6666-6666-666666666666"
    db = _db(agy_dir, c1, c2, title="Workspace: /tmp/alfred-evolve", bad_fts=True)
    ann = agy_dir / "annotations" / f"{c1}.pbtxt"
    ann.parent.mkdir(parents=True, exist_ok=True)
    ann.write_text('title:"Old"  archived:true', "utf-8")
    assert set(arc.repair_blank_or_boilerplate_titles()) == {c1, c2}
    assert "archived:true" in ann.read_text("utf-8")
    with sqlite3.connect(db) as cn:
        t1 = cn.execute("SELECT title FROM conversation_summaries WHERE conversation_id=?",
                        (c1,)).fetchone()
    assert t1 is not None and "alfred-evolve" in str(t1[0])
    state_store = StateStore(paths)
    agy_mgr = AgySessionManager(paths, dom.AlfredSettings(), state_store,
                                antigravity_data_dir=agy_dir, agy_locator_fn=lambda: None)
    _db(agy_dir, c1, c2, title="", bad_fts=True)
    agy_mgr.repair_session_titles()
    assert {c1, c2}.issubset(set(agy_mgr.daemon_sync.load_state().conversation_ids))
    wt, vbin = tmp_path / "wt", tmp_path / ".venv" / "bin"
    for p in (wt / "core" / "alfred", wt / "core" / "tests", vbin):
        p.mkdir(parents=True)
    for exe in ("ruff", "mypy", "pytest"):
        (vbin / exe).touch()
    runs: list[tuple[list[str], dict[str, str]]] = []

    def _cap_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        """Record TDD gate commands and environment dicts without running external tools.

        Args:
            cmd: Subprocess argv list.
            **kw: Keyword args forwarded to subprocess.run.
        """
        runs.append((list(cmd), dict(kw.get("env") or {})))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _cap_run)
    EvolveWorktreeService(paths, dom.AlfredSettings(), state_store)._run_tdd_and_leak_gates(wt)
    assert any("core/tests" in c and any("mypy" in t for t in c) for c, _ in runs)
    assert all(env.get("ALFRED_ROOT_DIR") == str(wt) for _, env in runs)
