"""WP7 TDD suite (Part 1): native Antigravity .pbtxt auto-archiving and CLI backfill."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from alfred.domain.models import (
    ActiveSessionRecord,
    AlfredPaths,
    AlfredSettings,
    EmailMessage,
    InboxOutputType,
)
from alfred.main import build_controller
from alfred.repositories.state_store import StateStore
from alfred.services.agy_archive_store import AgyArchiveStore, format_annotations_pbtxt
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_service import DriveService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.workspace_service import WorkspaceService


class _FakeGmailStub:
    """Minimal GmailService stub for InboxTriageService silent-archive unit tests."""

    def __init__(self, owner_addrs: frozenset[str] = frozenset()) -> None:
        """Initialize fake Gmail state."""
        self.owner_addrs = owner_addrs
        self.starred: list[str] = []
        self.drafts: list[tuple[str, str, str, str]] = []
        self.messages_by_id: dict[str, EmailMessage] = {}

    def get_authenticated_addresses(self) -> frozenset[str]:
        """Return configured owner email addresses."""
        return self.owner_addrs

    def star_message(self, message_id: str) -> None:
        """Record a starred message ID."""
        self.starred.append(message_id)

    def create_draft(
        self,
        thread_id: str,
        to_addr: str,
        subject: str,
        body_text: str,
        in_reply_to: str = "",
        references: str = "",
    ) -> str:
        """Record a draft reply and return a synthetic draft ID."""
        self.drafts.append((thread_id, to_addr, subject, body_text))
        return f"draft_{len(self.drafts)}"

    def get_message(self, message_id: str) -> EmailMessage | None:
        """Look up a seeded message by ID."""
        return self.messages_by_id.get(message_id)


def _seed_summary_db(agy_dir: Path, conv_id: str, project_id: str) -> Path:
    """Create a realistic conversation_summaries.db and conversations/<id>.db."""
    (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
    conv_db = agy_dir / "conversations" / f"{conv_id}.db"
    conv_db.write_bytes(b"sqlite-trajectory")
    sum_db = agy_dir / "conversation_summaries.db"
    conn = sqlite3.connect(sum_db)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', "
            "project_id TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS conversations_fts ("
            "conversation_id TEXT, archived INTEGER DEFAULT 0, title TEXT DEFAULT '')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO conversation_summaries(conversation_id, title, project_id) "
            "VALUES (?, '', ?)",
            (conv_id, project_id),
        )
        conn.execute(
            "INSERT INTO conversations_fts(conversation_id, archived, title) VALUES (?, 0, '')",
            (conv_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return conv_db


def test_archive_store_writes_pbtxt_and_updates_sqlite(tmp_path: Path) -> None:
    """AgyArchiveStore writes native ConversationAnnotations .pbtxt and updates SQLite."""
    agy_dir = tmp_path / "agy"
    conv_id = "11111111-2222-3333-4444-555555555555"
    conv_db = _seed_summary_db(agy_dir, conv_id, "f09ae641-c66a-4203-9b10-577b8560b60f")
    conv_db_old_mtime = conv_db.stat().st_mtime_ns - 10_000_000_000
    os.utime(conv_db, ns=(conv_db_old_mtime, conv_db_old_mtime))

    store = AgyArchiveStore(agy_dir)
    pbtxt_path = store.archive_conversation(
        conv_id,
        title='[SILENT] Facture "mensuelle"\nTest',
        archived_at_iso="2026-11-11T20:13:06Z",
    )
    assert pbtxt_path is not None and pbtxt_path.is_file()
    text = pbtxt_path.read_text(encoding="utf-8")
    assert 'title:"[SILENT] Facture \\"mensuelle\\" Test"' in text
    assert "archived:true" in text
    assert "archival_status_timestamp:{seconds:" in text
    assert conv_db.stat().st_mtime_ns > conv_db_old_mtime

    conn = sqlite3.connect(agy_dir / "conversation_summaries.db")
    try:
        row = conn.execute(
            "SELECT title FROM conversation_summaries WHERE conversation_id = ?", (conv_id,)
        ).fetchone()
        fts = conn.execute(
            "SELECT archived, title FROM conversations_fts WHERE conversation_id = ?", (conv_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == '[SILENT] Facture "mensuelle" Test'
    assert fts == (1, '[SILENT] Facture "mensuelle" Test')
    assert "archived:false" in format_annotations_pbtxt("Active", archived=False)


def test_cognitive_silent_keeps_and_archives_alfred_inbox_conversation(tmp_path: Path) -> None:
    """Cognitive SILENT_NO_ACTION passes --project alfred-inbox and archives instead of purging."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings()
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    conv_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _seed_summary_db(agy_dir, conv_id, "proj-inbox")
    captured_cmds: list[list[str]] = []

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Simulate agy cognitive classifier returning SILENT_NO_ACTION."""
        captured_cmds.append(cmd)
        payload = {
            "conversation_id": conv_id,
            "structured_output": {
                "output_type": "SILENT_NO_ACTION",
                "reason": "Confirmation de commande passive",
            },
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    agy = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=fake_runner,
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )
    triage = InboxTriageService(
        store,
        _FakeGmailStub(frozenset({"owner1@example.test"})),  # type: ignore[arg-type]
        KnowledgeService(paths, settings, store),
        DriveService(paths, settings, store),
        agy,
        spawn_live_agy=True,
    )
    msg = EmailMessage(
        message_id="m_silent_1",
        thread_id="th_1",
        sender="shop@vendor.test",
        recipient="owner1@example.test",
        subject="Votre commande #42",
        body_text="Merci pour votre achat.",
    )
    decision = triage.process_message(msg, use_cognitive_agy=True)
    assert decision.output_type == InboxOutputType.SILENT_NO_ACTION
    assert decision.session_id == conv_id and decision.session_archived is True
    assert captured_cmds[0][captured_cmds[0].index("--project") + 1] == "alfred-inbox"
    assert (agy_dir / "conversations" / f"{conv_id}.db").is_file()
    pbtxt = (agy_dir / "annotations" / f"{conv_id}.pbtxt").read_text(encoding="utf-8")
    assert "archived:true" in pbtxt and "[SILENT] Votre commande #42" in pbtxt


def test_deterministic_silent_spawns_archived_trace_when_live(tmp_path: Path) -> None:
    """Deterministic SILENT_NO_ACTION materializes an archived trace in alfred-inbox when live."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings()
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    conv_id = "22222222-3333-4444-5555-666666666666"
    _seed_summary_db(agy_dir, conv_id, "proj-inbox")

    def fake_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Simulate agy trace conversation creation."""
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"conversation_id": conv_id}), stderr=""
        )

    agy = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=fake_runner,
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )
    triage = InboxTriageService(
        store,
        _FakeGmailStub(),  # type: ignore[arg-type]
        KnowledgeService(paths, settings, store),
        DriveService(paths, settings, store),
        agy,
        spawn_live_agy=True,
    )
    msg = EmailMessage(
        message_id="m_det_1",
        thread_id="th_det",
        sender="no-reply@portal.test",
        subject="Newsletter mensuelle",
        headers={"List-Unsubscribe": "<mailto:unsub@portal.test>"},
    )
    decision = triage.process_message(msg, use_cognitive_agy=True)
    assert decision.output_type == InboxOutputType.SILENT_NO_ACTION
    assert decision.session_id == conv_id
    assert (agy_dir / "annotations" / f"{conv_id}.pbtxt").is_file()


def test_prune_orphan_default_cli_project(tmp_path: Path) -> None:
    """init_workspaces(prune_legacy=True) removes orphan default-cli-project rows and JSON."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings()
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    (projects_dir / "default-cli-project.json").write_text(
        '{"id": "default-cli-project", "name": "CLI Project"}', encoding="utf-8"
    )
    _seed_summary_db(agy_dir, "dead-orphan-conv", "default-cli-project")
    (agy_dir / "conversations" / "dead-orphan-conv.db").unlink()
    agy = AgySessionManager(paths, settings, store, antigravity_data_dir=agy_dir)
    ws = WorkspaceService(
        paths,
        settings,
        agy,
        projects_dir=projects_dir,
        conversations_db=agy_dir / "conversation_summaries.db",
        runner_fn=lambda cmd, _cwd: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )
    ws.init_workspaces(prune_legacy=True)
    assert not (projects_dir / "default-cli-project.json").exists()


def test_backfill_silent_archives_cli(tmp_path: Path, capsys: Any) -> None:
    """backfill-inbox-archives materializes synthetic agy_* records idempotently."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings()
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    backfilled_conv = "99999999-8888-7777-6666-555555555555"
    _seed_summary_db(agy_dir, backfilled_conv, "proj-inbox")
    agy = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=lambda cmd: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"conversation_id": backfilled_conv}), stderr=""
        ),
        agy_locator_fn=lambda: "/usr/bin/agy",
        antigravity_data_dir=agy_dir,
    )
    gmail = _FakeGmailStub()
    gmail.messages_by_id["msg_hist_1"] = EmailMessage(
        message_id="msg_hist_1",
        thread_id="th_hist_1",
        sender="alerts@bank.test",
        subject="Relevé mensuel disponible",
    )
    synthetic = ActiveSessionRecord(
        session_id="agy_alfred_inbox_abc1234567",
        workspace="alfred-inbox",
        status="ARCHIVED",
        outcome="SILENT_NO_ACTION: Relevé passif",
        source_message_id="msg_hist_1",
        created_at="2026-11-11T20:13:06Z",
        archived_at="2026-11-11T20:13:06Z",
    )
    store.conversations.save(
        store.conversations.load().model_copy(
            update={"sessions": {synthetic.session_id: synthetic}}
        )
    )
    ctrl = build_controller(str(tmp_path))
    ctrl._inbox = InboxTriageService(
        store,
        gmail,  # type: ignore[arg-type]
        KnowledgeService(paths, settings, store),
        DriveService(paths, settings, store),
        agy,
    )
    assert ctrl.run(["backfill-inbox-archives"]) == 0
    out_first = json.loads(capsys.readouterr().out)
    assert out_first == {"materialized_conversations": [backfilled_conv], "count": 1}
    assert ctrl.run(["backfill-inbox-archives"]) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 0
    assert (agy_dir / "annotations" / f"{backfilled_conv}.pbtxt").is_file()
