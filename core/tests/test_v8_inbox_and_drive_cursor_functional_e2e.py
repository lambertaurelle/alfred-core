"""Functional and E2E CLI tests for allowlisted inbox triage and independent Drive cursors."""

from __future__ import annotations

import base64
import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pypdf import PdfWriter

from alfred.controllers.app_factory import build_cli_controller
from alfred.controllers.cli_controller import CliController
from alfred.domain import models as dom
from alfred.domain.feature_models import FeatureFlagsConfig, InboxFilterConfig
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_service import FOLDER_MIME, DriveService
from alfred.services.gmail_drive_bridge_service import GmailDriveBridgeService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService


def _b64u(raw: bytes) -> str:
    """Encode bytes to unpadded URL-safe base64 string."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _blank_scanned_pdf() -> bytes:
    """Build a valid 1-page PDF containing zero extractable native text."""
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _att_part(
    mime: str, fname: str, raw: bytes, *, disp: str = "attachment", att_id: str = "", cid: str = ""
) -> dict[str, Any]:
    """Construct a Gmail MIME part with explicit Content-Disposition and optional Content-ID."""
    hdrs = [{"name": "Content-Disposition", "value": disp}] + (
        [{"name": "Content-ID", "value": cid}] if cid else []
    )
    body: dict[str, Any] = (
        {"attachmentId": att_id, "size": len(raw)} if att_id else {"data": _b64u(raw)}
    )
    return {"mimeType": mime, "filename": fname, "headers": hdrs, "body": body}


def _json_proc(
    cmd: list[str], conv_id: str, output: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    """Build a zero-exit subprocess result containing structured_output JSON."""
    payload = {"conversation_id": conv_id, "structured_output": output}
    return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")


def _box(payload: Any) -> SimpleNamespace:
    """Wrap an in-memory API response behind .execute()."""
    return SimpleNamespace(execute=lambda: payload)


class FakeDriveChangesLog:
    """Non-destructive offset-indexed Drive client modeling independent multi-cursor reads."""

    def __init__(self, page_size: int | None = None) -> None:
        """Initialize file registry, text/bytes maps, and append-only change log."""
        self.items: dict[str, dict[str, Any]] = {}
        self.contents: dict[str, str] = {}
        self.raw_contents: dict[str, bytes] = {}
        self.changes_log: list[dict[str, Any]] = []
        self.seq = 10
        self.page_size = page_size

    def deposit_file(
        self, fid: str, name: str, mime: str, parent_id: str, raw: bytes | str = ""
    ) -> dict[str, Any]:
        """Add a file record on Drive and append a non-destructive change event."""
        self.seq += 1
        rec: dict[str, Any] = {
            "id": fid, "name": name, "mimeType": mime,
            "parents": [parent_id] if parent_id else [],
            "modifiedTime": f"2026-11-25T10:{self.seq:02d}:00Z", "md5Checksum": f"md5_{fid}",
            "headRevisionId": f"rev_{fid}", "ownedByMe": True, "shared": False,
            "trashed": False, "size": 128, "webViewLink": f"https://drive.example.test/{fid}",
        }
        self.items[fid] = rec
        if isinstance(raw, bytes):
            self.raw_contents[fid] = raw
        elif raw:
            self.contents[fid] = raw
        self.changes_log.append({"fileId": fid, "removed": False, "file": dict(rec)})
        return dict(rec)

    def _list_files(self, *, q: str = "", **_kw: Any) -> SimpleNamespace:
        """Filter non-trashed Drive items matching folder, mimeType, and parent predicates."""
        matched: list[dict[str, Any]] = []
        for it in self.items.values():
            if ("trashed = false" in q and it.get("trashed")) or (
                f"mimeType = '{FOLDER_MIME}'" in q and it.get("mimeType") != FOLDER_MIME
            ) or (f"mimeType != '{FOLDER_MIME}'" in q and it.get("mimeType") == FOLDER_MIME):
                continue
            if " in parents" in q and q.split(" in parents")[0].rsplit("'", 1)[-1] not in it.get(
                "parents", []
            ):
                continue
            if "name = '" in q and it.get("name") != q.split("name = '", 1)[1].split("'", 1)[0]:
                continue
            matched.append(dict(it))
        return _box({"files": matched})

    def _create_file(
        self, *, body: dict[str, Any], media_body: Any = None, **_kw: Any
    ) -> SimpleNamespace:
        """Create a Drive file/folder and append a non-destructive change event."""
        fid = f"drv_{self.seq + 1}"
        raw = (
            bytes(media_body.getbytes(0, media_body.size()))
            if media_body is not None and hasattr(media_body, "getbytes")
            else b""
        )
        parents = [str(p) for p in body.get("parents", [])]
        rec = self.deposit_file(
            fid, str(body.get("name", fid)),
            str(body.get("mimeType", "application/octet-stream")),
            parents[0] if parents else "", raw,
        )
        rec["parents"] = parents
        self.items[fid]["parents"] = parents
        return _box(rec)

    def _list_changes(self, *, pageToken: str, pageSize: int = 100, **_kw: Any) -> SimpleNamespace:  # noqa: N803
        """Return change slice starting at offset pageToken without clearing the log."""
        offset = int(pageToken.removeprefix("tok_"))
        eff_size = self.page_size or pageSize
        batch = [dict(ev) for ev in self.changes_log[offset : offset + eff_size]]
        next_off = offset + len(batch)
        if next_off < len(self.changes_log):
            return _box({"changes": batch, "nextPageToken": f"tok_{next_off}"})
        return _box({"changes": batch, "newStartPageToken": f"tok_{len(self.changes_log)}"})

    def files(self) -> SimpleNamespace:
        """Return the Drive files resource facade."""
        return SimpleNamespace(
            get=lambda **kw: _box(dict(self.items[str(kw["fileId"])])),
            get_media=lambda **kw: _box(
                self.raw_contents.get(
                    str(kw["fileId"]), self.contents.get(str(kw["fileId"]), "").encode("utf-8")
                )
            ),
            list=self._list_files,
            create=self._create_file,
        )

    def changes(self) -> SimpleNamespace:
        """Return the Drive changes resource facade."""
        return SimpleNamespace(
            getStartPageToken=lambda **_kw: _box(
                {"startPageToken": f"tok_{len(self.changes_log)}"}
            ),
            list=self._list_changes,
        )


class FakeGmailClient:
    """In-memory Gmail v1 client supporting history polling, raw MIME fetch, drafts, & stars."""

    def __init__(self) -> None:
        """Initialize mailbox profile, messages, attachments, and audit lists."""
        self.history_id = "100"
        self.history_events: list[dict[str, Any]] = []
        self.messages: dict[str, dict[str, Any]] = {}
        self.attachments: dict[str, bytes] = {}
        self.draft_creations: list[dict[str, Any]] = []
        self.starred_ids: list[str] = []

    def _modify_msg(self, *, id: str, body: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        """Record STARRED additions and return message stub."""
        if "STARRED" in body.get("addLabelIds", []):
            self.starred_ids.append(id)
        return _box({"id": id})

    def _create_draft(self, *, body: dict[str, Any], **_kw: Any) -> SimpleNamespace:
        """Record draft creation payload and return draft stub."""
        self.draft_creations.append(body)
        return _box({"id": f"draft_{len(self.draft_creations)}"})

    def users(self) -> SimpleNamespace:
        """Return the Gmail users API resource facade."""
        return SimpleNamespace(
            getProfile=lambda **_kw: _box(
                {"emailAddress": "owner@example.com", "historyId": self.history_id}
            ),
            settings=lambda: SimpleNamespace(
                sendAs=lambda: SimpleNamespace(list=lambda **_kw: _box({}))
            ),
            history=lambda: SimpleNamespace(list=lambda **_kw: _box(
                {"historyId": self.history_id, "history": list(self.history_events)}
            )),
            messages=lambda: SimpleNamespace(
                get=lambda *, id, **_kw: _box(self.messages[id]),
                modify=self._modify_msg,
                attachments=lambda: SimpleNamespace(get=lambda *, id, **_kw: _box(
                    {"data": _b64u(self.attachments.get(id, b"")),
                     "size": len(self.attachments.get(id, b""))}
                )),
            ),
            drafts=lambda: SimpleNamespace(create=self._create_draft),
        )


def _build_portal_raw_msg(msg_id: str, pdf_bytes: bytes) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Build a raw multipart Gmail payload with inline logo.png, scanned PDF, and terms PDF."""
    terms_bytes = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
    html_raw = "<p>Your monthly statement is ready on your portal. Unsubscribe anytime.</p>"
    parts = [
        {"mimeType": "multipart/related", "parts": [
            {"mimeType": "text/html", "body": {"data": _b64u(html_raw.encode("utf-8"))}},
            _att_part(
                "image/png", "logo.png", b"\x89PNG\r\n", disp="inline", cid="<l@example.com>"
            ),
        ]},
        _att_part("application/pdf", "statement_2026_11.pdf", pdf_bytes, att_id="att-stmt"),
        _att_part("application/pdf", "general_terms.pdf", terms_bytes, att_id="att-terms"),
    ]
    raw_msg = {
        "id": msg_id, "threadId": f"th_{msg_id}", "labelIds": ["INBOX", "CATEGORY_UPDATES"],
        "snippet": "Your monthly statement is ready on your portal.",
        "payload": {"mimeType": "multipart/mixed", "parts": parts, "headers": [
            {"name": "From", "value": "Portal <noreply@portal.example.com>"},
            {"name": "To", "value": "owner@example.com"},
            {"name": "Subject", "value": "Monthly statement available"},
            {"name": "List-Unsubscribe", "value": "<https://portal.example.com/unsub>"},
            {"name": "Auto-Submitted", "value": "auto-generated"},
        ]},
    }
    return raw_msg, {"att-stmt": pdf_bytes, "att-terms": terms_bytes}


def _write_custom_rules(paths: dom.AlfredPaths) -> None:
    """Write instance/config/prompts/inbox_custom_rules.md for selective PDF filing."""
    rp = paths.instance_dir / "config" / "prompts" / "inbox_custom_rules.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(
        "- Always set keep_in_inbox=true for portal monthly statements.\n"
        "- Save only statement_2026_11.pdf into fld_statements; ignore general_terms.pdf.\n",
        encoding="utf-8",
    )


def _build_wired_env(
    tmp_path: Path,
    raw_msg: dict[str, Any],
    att_blobs: dict[str, bytes],
    runner_fn: Callable[[list[str]], subprocess.CompletedProcess[str]],
    *,
    drive_watcher: bool = False,
) -> tuple[
    dom.AlfredPaths, dom.AlfredSettings, StateStore, FakeDriveChangesLog, FakeGmailClient,
    DriveService, GmailService, AgySessionManager,
]:
    """Initialize isolated paths, custom rules, settings, fake clients, and core services."""
    paths = dom.AlfredPaths.from_env(tmp_path)
    _write_custom_rules(paths)
    store = StateStore(paths)
    cfg = dom.AlfredSettings(
        features=FeatureFlagsConfig(
            drive_knowledge_sync=True, drive_watcher=drive_watcher, inbox_auto_triage=True
        ),
        inbox_filters=InboxFilterConfig(allowlist_automated_domains=["portal.example.com"]),
        agy_sync_daemon_on_changes=False,
    )
    drv_cli = FakeDriveChangesLog()
    gm_cli = FakeGmailClient()
    gm_cli.messages[str(raw_msg["id"])] = raw_msg
    gm_cli.attachments.update(att_blobs)
    drive = DriveService(paths, cfg, store, drive_client=drv_cli)
    gmail = GmailService(paths, cfg, gmail_client=gm_cli)
    agy = AgySessionManager(
        paths, cfg, store, runner_fn=runner_fn,
        agy_locator_fn=lambda: "/usr/bin/agy", antigravity_data_dir=tmp_path / "agy_data",
    )
    return paths, cfg, store, drv_cli, gm_cli, drive, gmail, agy


def _setup_inbox_triage(
    tmp_path: Path,
    raw_msg: dict[str, Any],
    att_blobs: dict[str, bytes],
    runner_fn: Callable[[list[str]], subprocess.CompletedProcess[str]],
    *,
    drive_watcher: bool = False,
) -> tuple[InboxTriageService, FakeGmailClient, dom.EmailMessage]:
    """Wire InboxTriageService with custom rules, allowlisted domain, and mocked agy."""
    paths, cfg, store, _, gm_cli, drive, gmail, agy = _build_wired_env(
        tmp_path, raw_msg, att_blobs, runner_fn, drive_watcher=drive_watcher
    )
    bridge = GmailDriveBridgeService(cfg, store, gmail, drive)
    triage = InboxTriageService(store, gmail, KnowledgeService(paths, cfg, store), drive, agy,
                                settings=cfg, attachment_bridge=bridge)
    return triage, gm_cli, gmail._parse_raw_message(raw_msg)


def _build_e2e_cli(
    tmp_path: Path,
) -> tuple[dom.AlfredPaths, StateStore, FakeDriveChangesLog, FakeGmailClient, CliController]:
    """Build wired CliController with FakeDriveChangesLog and FakeGmailClient."""
    raw_msg, blobs = _build_portal_raw_msg("m_e2e_1", _blank_scanned_pdf())

    def _agy_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return valid triage, drive-watcher, or delegation JSON depending on invocation."""
        if "--json-schema" in cmd and "alfred-drive" in cmd:
            return _json_proc(cmd, "conv-dw", {
                "output_type": "SILENT_NO_ACTION", "reason": "OK", "operations": [],
            })
        if "--json-schema" in cmd:
            return _json_proc(cmd, "conv-cls", {
                "output_type": "USER_ACTION_REQUIRED", "reason": "Save PDF", "keep_in_inbox": True,
                "attachments_to_save": [
                    {"filename": "statement_2026_11.pdf", "parent_id": "fld_statements"}
                ],
            })
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"conversation_id": "conv-drv"}), "")

    paths, cfg, store, drv_cli, gm_cli, drive, gmail, agy = _build_wired_env(
        tmp_path, raw_msg, blobs, _agy_runner, drive_watcher=True
    )
    drv_cli.items = {
        "fld_statements": {
            "id": "fld_statements", "name": "Statements", "mimeType": FOLDER_MIME, "parents": [],
        },
        "fld_ref": {
            "id": "fld_ref", "name": "_ALFRED_REFERENTIEL_", "mimeType": FOLDER_MIME, "parents": [],
        },
    }
    store.bookmarks.save(dom.GoogleSyncBookmarks(referentiel_folder_id="fld_ref"))
    ctrl = build_cli_controller(
        paths=paths, settings=cfg, state_store=store,
        drive_service=drive, gmail_service=gmail, agy_manager=agy,
    )
    return paths, store, drv_cli, gm_cli, ctrl


def test_functional_inbox_allowlisted_scanned_pdf_selective_filing_and_keep_in_inbox(
    tmp_path: Path,
) -> None:
    """Verify allowlisted portal scanned PDF runs cognitive triage with custom rules."""
    raw_msg, blobs = _build_portal_raw_msg("m_scan_1", _blank_scanned_pdf())
    prompts: list[str] = []

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Capture prompt and return USER_ACTION_REQUIRED with keep_in_inbox and 1 PJ."""
        prompts.append(cmd[cmd.index("--print") + 1])
        return _json_proc(cmd, "conv-scan-1", {
            "output_type": "USER_ACTION_REQUIRED", "reason": "Review & file statement.",
            "keep_in_inbox": True, "research_brief": ["Check statement_2026_11.pdf"],
            "attachments_to_save": [
                {"filename": "statement_2026_11.pdf", "parent_id": "fld_statements"}
            ],
        })

    triage, gm_cli, parsed = _setup_inbox_triage(tmp_path, raw_msg, blobs, _runner)
    assert parsed.attachment_filenames == ["statement_2026_11.pdf", "general_terms.pdf"]
    dec = triage.process_message(parsed, use_cognitive_agy=True)

    assert prompts and "Always set keep_in_inbox=true" in prompts[0]
    assert "Attachments: statement_2026_11.pdf, general_terms.pdf" in prompts[0]
    assert "logo.png" not in prompts[0] and dec.starred and "m_scan_1" in gm_cli.starred_ids
    assert dec.output_type == dom.InboxOutputType.USER_ACTION_REQUIRED and dec.keep_in_inbox is True
    assert [t.filename for t in dec.attachments_to_save] == ["statement_2026_11.pdf"]
    plan = dec.implementation_plan_markdown or ""
    assert "alfred save-attachment-to-drive --message-id m_scan_1" in plan
    assert "--parent-id fld_statements" in plan and "general_terms.pdf" not in plan
    assert "alfred archive-email" not in plan and "--keep-in-inbox" in plan


def test_functional_inbox_drive_watcher_mode_delegates_selective_attachments(
    tmp_path: Path,
) -> None:
    """Verify drive_watcher=True delegates only selected attachment to alfred-drive."""
    raw_msg, blobs = _build_portal_raw_msg("m_dw_1", _blank_scanned_pdf())

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return selective attachment filing directive."""
        return _json_proc(cmd, "conv-dw-1", {
            "output_type": "USER_ACTION_REQUIRED", "reason": "Archive via watcher.",
            "attachments_to_save": [
                {"filename": "statement_2026_11.pdf", "parent_id": "fld_statements"}
            ],
        })

    triage, _, parsed = _setup_inbox_triage(tmp_path, raw_msg, blobs, _runner, drive_watcher=True)
    plan = triage.process_message(parsed, use_cognitive_agy=True).implementation_plan_markdown or ""
    assert "alfred delegate --to drive --mode execute-approved" in plan
    assert "--source-workspace alfred-inbox" in plan and "--source-message-id m_dw_1" in plan
    assert "statement_2026_11.pdf" in plan and "general_terms.pdf" not in plan
    assert "save-attachment-to-drive" not in plan


def test_functional_inbox_anti_noreply_guard_on_draft_reply_and_agy_failure(
    tmp_path: Path,
) -> None:
    """Verify DRAFT_REPLY to an automated sender is blocked on both LLM draft and fallback."""
    raw_msg, blobs = _build_portal_raw_msg("m_nr_1", _blank_scanned_pdf())

    def _draft_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Erroneously propose DRAFT_REPLY to noreply@portal.example.com."""
        return _json_proc(cmd, "conv-nr-1", {
            "output_type": "DRAFT_REPLY", "reason": "Reply", "draft_body": "Thanks.",
        })

    t_a, gm_a, p_a = _setup_inbox_triage(tmp_path / "a", raw_msg, blobs, _draft_runner)
    dec_a = t_a.process_message(p_a, use_cognitive_agy=True)
    assert dec_a.output_type == dom.InboxOutputType.USER_ACTION_REQUIRED
    assert dec_a.draft_id is None and dec_a.starred is True and gm_a.draft_creations == []

    t_b, gm_b, p_b = _setup_inbox_triage(
        tmp_path / "b", raw_msg, blobs, lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "err")
    )
    dec_b = t_b.process_message(p_b, use_cognitive_agy=True)
    assert dec_b.output_type == dom.InboxOutputType.USER_ACTION_REQUIRED
    assert dec_b.draft_id is None and dec_b.starred is True and gm_b.draft_creations == []


def test_e2e_cli_drive_cursor_isolation_and_concurrent_attachment_transfer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify E2E init-drive, periodic skip, sync-drive isolation, attachment save, & watcher."""
    _, store, drv_cli, _, ctrl = _build_e2e_cli(tmp_path)
    assert ctrl.run(["init-drive", "--execute"]) == 0
    _ = capsys.readouterr()
    assert store.bookmarks.load().drive_page_token == "tok_0"
    assert store.bookmarks.load().drive_watcher_page_token == "tok_0"

    drv_cli.deposit_file(
        "f_tax", "Avis_2026.pdf", "application/pdf", "fld_statements", _blank_scanned_pdf()
    )
    assert ctrl.run(["run-routines", "--only", "periodic"]) == 0
    per_job = json.loads(capsys.readouterr().out)["jobs"][0]
    assert per_job["status"] == "SKIPPED_DELEGATED_TO_WATCHER"

    assert ctrl.run(["sync-drive"]) == 0
    _ = capsys.readouterr()
    assert store.bookmarks.load().drive_page_token == "tok_1"
    assert store.bookmarks.load().drive_watcher_page_token == "tok_0"

    assert ctrl.run(["save-attachment-to-drive", "--message-id", "m_e2e_1",
                     "--parent-id", "fld_statements", "--filename", "statement_2026_11.pdf"]) == 0
    saved_id = json.loads(capsys.readouterr().out)["drive_file_id"]

    assert ctrl.run(["run-routines", "--only", "drive"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["status"] == "OK"
    assert store.bookmarks.load().drive_watcher_page_token == "tok_2"
    ledger = store.drive_watcher_ledger.load().events
    assert "f_tax" in ledger and saved_id in ledger
    entries = store.drive_index.load().entries
    assert "f_tax" in entries and saved_id in entries

    assert ctrl.run(["status-routines", "--systemd-dir", str(tmp_path / "no_systemd")]) == 0
    assert json.loads(capsys.readouterr().out)["drive_page_token_initialized"] is True


def test_e2e_cli_poll_inbox_cognitive_and_delegate_to_drive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify E2E poll-inbox --cognitive-agy and delegate --to drive --mode execute-approved."""
    _, _, _, gm_cli, ctrl = _build_e2e_cli(tmp_path)
    assert ctrl.run(["delegate", "--to", "drive", "--mode", "execute-approved",
                     "--source-workspace", "alfred-inbox", "--source-message-id", "m_e2e_1",
                     "--title", "Save PDF", "--brief", "File statement_2026_11.pdf"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "EXECUTED"

    assert ctrl.run(["poll-inbox", "--cognitive-agy"]) == 0
    _ = capsys.readouterr()
    gm_cli.history_id = "101"
    gm_cli.history_events.append({"messagesAdded": [{"message": {
        "id": "m_e2e_1", "labelIds": ["INBOX", "CATEGORY_UPDATES"],
    }}]})
    assert ctrl.run(["poll-inbox", "--cognitive-agy"]) == 0
    poll_out = json.loads(capsys.readouterr().out)
    assert len(poll_out) == 1 and poll_out[0]["keep_in_inbox"] is True
    assert poll_out[0]["attachments_to_save"][0]["filename"] == "statement_2026_11.pdf"
