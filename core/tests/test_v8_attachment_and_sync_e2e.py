"""End-to-end CLI, PreToolUse hook subprocess, and evolve governance contract tests."""

from __future__ import annotations

import base64
import io
import json
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from alfred.controllers.app_factory import build_cli_controller
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    DriveEntry,
    DriveMetadataIndex,
    fold_accents,
)
from alfred.main import main
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_service import FOLDER_MIME
from alfred.services.evolve_worktree_service import EvolveWorktreeService
from alfred.services.google_auth import GoogleOAuthService

REPO_ROOT = Path(__file__).resolve().parents[2]


def _b64u(raw: bytes) -> str:
    """Encode raw bytes into an unpadded URL-safe base64 string.

    Args:
        raw: Binary payload to encode.

    Returns:
        Unpadded base64url ASCII string.
    """
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _build_pdf_bytes(text: str) -> bytes:
    """Create a 1-page PDF containing extractable ASCII text.

    Args:
        text: Text rendered in the PDF stream.

    Returns:
        Serialized PDF bytes.
    """
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
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


def _write_e2e_settings(
    root_dir: Path, *, drive_sync: bool = True, agy_bin: str = "agy"
) -> AlfredPaths:
    """Write isolated instance/config/settings.yaml for E2E CLI runs.

    Args:
        root_dir: Isolated ALFRED_ROOT_DIR temporary directory.
        drive_sync: Toggle for features.drive_knowledge_sync.
        agy_bin: Configured antigravity.binary_path value.

    Returns:
        Resolved AlfredPaths for root_dir.
    """
    paths = AlfredPaths.from_env(root_dir)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "antigravity": {"binary_path": agy_bin, "sync_daemon_on_changes": False},
        "features": {
            "drive_knowledge_sync": drive_sync,
            "inbox_auto_triage": True,
            "gmail_drafts_and_send": True,
            "calendar_management": True,
            "tasks_management": True,
            "contacts_lookup": True,
        },
        "mcp": {"public_url": "https://alfred.domaine-perso.example"},
    }
    paths.settings_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return paths


def _build_fake_gmail_and_drive_clients(pdf_raw: bytes) -> tuple[MagicMock, MagicMock]:
    """Create mocked Gmail v1 and Drive v3 API discovery clients for attachment CLI E2E.

    Args:
        pdf_raw: Valid serialized PDF bytes served for attachment 'att-1'.

    Returns:
        Tuple of (fake_gmail_client, fake_drive_client).
    """
    gmail_client, drive_client = MagicMock(), MagicMock()
    users = gmail_client.users.return_value
    users.messages.return_value.get.return_value.execute.return_value = {
        "id": "m1",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "image/png",
                    "filename": "logo.png",
                    "headers": [{"name": "Content-ID", "value": "<logo>"}],
                    "body": {"data": _b64u(b"\x89PNG"), "size": 4},
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "accord.pdf",
                    "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                    "body": {"attachmentId": "att-1", "size": len(pdf_raw)},
                },
            ],
        },
    }
    att_api = users.messages.return_value.attachments.return_value
    att_api.get.return_value.execute.return_value = {"data": _b64u(pdf_raw), "size": len(pdf_raw)}
    files_api = drive_client.files.return_value
    files_api.get.return_value.execute.return_value = {
        "id": "dir1",
        "name": "01_Administration",
        "mimeType": FOLDER_MIME,
        "parents": [],
        "trashed": False,
    }
    files_api.list.return_value.execute.return_value = {"files": []}
    files_api.create.return_value.execute.return_value = {
        "id": "drv_e2e_1",
        "name": "2026-11 - Accord.pdf",
        "mimeType": "application/pdf",
        "webViewLink": "https://drive.example.com/drv_e2e_1",
    }
    return gmail_client, drive_client


def _setup_google_mocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AlfredPaths, MagicMock, MagicMock]:
    """Configure isolated ALFRED_ROOT_DIR, HOME, and mocked Gmail/Drive discovery clients.

    Args:
        tmp_path: Pytest temporary workspace root.
        monkeypatch: Active pytest MonkeyPatch fixture.

    Returns:
        Tuple of (AlfredPaths, fake_gmail_client, fake_drive_client).
    """
    paths = _write_e2e_settings(tmp_path, drive_sync=True)
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    StateStore(paths).drive_index.save(
        DriveMetadataIndex(
            entries={
                "dir1": DriveEntry(
                    file_id="dir1",
                    name="01_Administration",
                    mime_type=FOLDER_MIME,
                    path="/01_Administration",
                    is_folder=True,
                )
            },
            folder_count=1,
            file_count=0,
        )
    )
    gmail_client, drive_client = _build_fake_gmail_and_drive_clients(
        _build_pdf_bytes("Accord Cadre E2E 2026")
    )
    monkeypatch.setattr(GoogleOAuthService, "load_credentials", lambda self: MagicMock())
    monkeypatch.setattr(
        GoogleOAuthService,
        "build_api_client",
        lambda self, name, _ver: gmail_client if name == "gmail" else drive_client,
    )
    monkeypatch.setattr(
        "googleapiclient.discovery.build",
        lambda name, _ver, credentials=None: drive_client if name == "drive" else MagicMock(),
    )
    return paths, gmail_client, drive_client


def _init_e2e_summary_db(agy_dir: Path) -> Path:
    """Create a minimal conversation_summaries.db inside agy_dir for E2E daemon sync tests.

    Args:
        agy_dir: Target Antigravity CLI data directory.

    Returns:
        Path to conversation_summaries.db.
    """
    (agy_dir / "conversations").mkdir(parents=True)
    db_path = agy_dir / "conversation_summaries.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, title TEXT DEFAULT '', "
            "project_id TEXT DEFAULT '', status TEXT DEFAULT 'CASCADE_RUN_STATUS_IDLE', "
            "not_fully_idle INTEGER DEFAULT 0)"
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _invoke_hook(
    tool_name: str,
    tool_input: Mapping[str, object],
    cwd: str,
) -> dict[str, object]:
    """Execute worktree security_pre_tool_hook.py in a real subprocess.

    Args:
        tool_name: Antigravity tool identifier (e.g. 'run_command', 'read_file').
        tool_input: JSON-serializable tool_input mapping.
        cwd: Simulated workspace working directory.

    Returns:
        Parsed JSON verdict dictionary containing 'decision' and 'reason'.
    """
    hook_path = REPO_ROOT / "core" / "scripts" / "security_pre_tool_hook.py"
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool_name,
            "cwd": cwd,
            "tool_input": dict(tool_input),
        }
    )
    proc = subprocess.run(
        [sys.executable, str(hook_path)],
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert isinstance(parsed, dict)
    return parsed


def test_e2e_cli_attachment_commands_via_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify list-attachments, read-attachment, and save-attachment-to-drive via main().

    Args:
        tmp_path: Pytest temporary directory for isolated ALFRED_ROOT_DIR.
        monkeypatch: Pytest MonkeyPatch fixture.
        capsys: Pytest stdout/stderr capture fixture.
    """
    paths, _, drive_client = _setup_google_mocks(tmp_path, monkeypatch)
    assert main(["list-attachments", "--message-id", "m1"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 2
    assert listed[0]["filename"] == "logo.png" and listed[0]["is_inline"] is True
    assert listed[1]["filename"] == "accord.pdf" and listed[1]["is_inline"] is False
    assert main(["read-attachment", "--message-id", "m1", "--index", "1"]) == 0
    read_out = json.loads(capsys.readouterr().out)
    assert read_out["index"] == 1 and "Accord Cadre E2E 2026" in read_out["text"]

    save_cmd = [
        "save-attachment-to-drive", "--message-id", "m1",
        "--parent-id", "dir1", "--name", "2026-11 - Accord.pdf",
    ]
    assert main(save_cmd) == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["drive_file_id"] == "drv_e2e_1"
    assert "drv_e2e_1" in StateStore(paths).drive_index.load().entries

    _write_e2e_settings(tmp_path, drive_sync=False)
    assert main(["save-attachment-to-drive", "--message-id", "m1", "--folder-id", "dir1"]) == 0
    skipped = json.loads(capsys.readouterr().out)
    assert skipped["status"] == "SKIPPED_DISABLED" and skipped["feature"] == "drive_knowledge_sync"
    assert drive_client.files.return_value.create.call_count == 1


def test_e2e_cli_sync_agy_ui_wait_idle_synced_and_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify sync-agy-ui --wait-idle CLI returns SYNCED when idle and DEFERRED on timeout.

    Args:
        tmp_path: Pytest temporary directory for isolated state and SQLite DB.
        monkeypatch: Pytest MonkeyPatch fixture.
        capsys: Pytest stdout/stderr capture fixture.
    """
    paths = _write_e2e_settings(tmp_path, drive_sync=True)
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    wait_args = ["sync-agy-ui", "--wait-idle", "--timeout", "0.05", "--poll-interval", "0.01"]
    assert main(wait_args) == 0
    _ = json.loads(capsys.readouterr().out)

    agy_dir = tmp_path / "agy_data"
    db_path = _init_e2e_summary_db(agy_dir)
    store = StateStore(paths)
    cfg = AlfredSettings.load(paths).model_copy(update={"agy_sync_daemon_on_changes": True})
    agy = AgySessionManager(
        paths, cfg, store, antigravity_data_dir=agy_dir,
        systemctl_runner=lambda c: subprocess.CompletedProcess(c, 0, "active\n", ""),
    )
    own_cid = "88888888-8888-8888-8888-888888888888"
    (agy_dir / "conversations" / f"{own_cid}.db").write_bytes(b"db")
    agy.daemon_sync.mark_dirty(own_cid, reason="e2e")
    ctrl = build_cli_controller(paths=paths, settings=cfg, state_store=store, agy_manager=agy)
    sync_cmd = ["sync-agy-ui", "--wait-idle", "--timeout", "1", "--poll-interval", "0.01"]
    assert ctrl.run(sync_cmd) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "SYNCED"

    busy_cid = "99999999-9999-9999-9999-999999999999"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO conversation_summaries(conversation_id, status, not_fully_idle) "
            "VALUES (?, 'CASCADE_RUN_STATUS_RUNNING', 1)",
            (busy_cid,),
        )
    agy.daemon_sync.mark_dirty(own_cid, reason="e2e-busy")
    assert ctrl.run(wait_args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "DEFERRED_ACTIVE_TURN"


def test_e2e_cli_evolve_spawn_agy_preserves_title_in_state_and_pbtxt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify evolve --spawn-agy persists '[Evolve] <title>' in StateStore and .pbtxt.

    Args:
        tmp_path: Pytest temporary directory for isolated HOME and fake agy script.
        monkeypatch: Pytest MonkeyPatch fixture.
        capsys: Pytest stdout/stderr capture fixture.
    """
    live_uuid = "abababab-1212-3434-5656-cdcdcdcdcdcd"
    conv_dir = tmp_path / ".gemini" / "antigravity-cli" / "conversations"
    conv_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    fake_agy = bin_dir / "fake_agy.sh"
    fake_agy.write_text(
        "#!/usr/bin/env bash\n"
        f'touch "{conv_dir / (live_uuid + ".db")}"\n'
        f'echo \'{{"conversation_id": "{live_uuid}"}}\'\n',
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)
    paths = _write_e2e_settings(tmp_path, drive_sync=True, agy_bin=str(fake_agy))
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    cmd = ["evolve", "--id", "p1", "--title", "Mon Evo", "--description", "Spec", "--spawn-agy"]
    assert main(cmd) == 0
    _ = capsys.readouterr()
    sessions = StateStore(paths).conversations.load().sessions
    assert live_uuid in sessions and sessions[live_uuid].title == "[Evolve] Mon Evo"
    pbtxt = tmp_path / ".gemini" / "antigravity-cli" / "annotations" / f"{live_uuid}.pbtxt"
    assert pbtxt.is_file() and 'title:"[Evolve] Mon Evo"' in pbtxt.read_text(encoding="utf-8")


def test_e2e_security_pre_tool_hook_attachment_commands_and_guards() -> None:
    """Verify PreToolUse hook allows attachment CLI in inbox and blocks chaining/secrets."""
    inbox_cwd = str(REPO_ROOT / "core" / "workspaces" / "alfred-inbox")

    for allowed_cmd in (
        "alfred list-attachments --message-id m1",
        "alfred read-attachment --message-id m1 --index 1",
        "alfred save-attachment-to-drive --message-id m1 --parent-id dir1 --name doc.pdf",
    ):
        verdict = _invoke_hook("run_command", {"CommandLine": allowed_cmd}, inbox_cwd)
        assert verdict["decision"] == "allow", f"Expected allow for {allowed_cmd}: {verdict}"

    for chained_cmd in (
        "alfred list-attachments --message-id m1 && cat token.json",
        "alfred read-attachment --message-id m1 ; id",
        "alfred save-attachment-to-drive --message-id m1 --parent-id dir1 | cat",
    ):
        verdict = _invoke_hook("run_command", {"CommandLine": chained_cmd}, inbox_cwd)
        assert verdict["decision"] == "deny", f"Expected deny for {chained_cmd}: {verdict}"

    for secret_path in (
        "instance/secrets/token.json",
        "instance/secrets/signature_vault/signature.pdf",
    ):
        verdict = _invoke_hook("read_file", {"Path": secret_path}, str(REPO_ROOT))
        assert verdict["decision"] == "deny", f"Expected deny for {secret_path}: {verdict}"


def test_e2e_evolve_governance_pillars_and_manifests_contract(tmp_path: Path) -> None:
    """Verify evolve docs, subagent manifests, and prepare_evolution_plan enforce 3-tier+swarm.

    Args:
        tmp_path: Pytest temporary directory for EvolveWorktreeService StateStore.
    """
    evolve_docs = [
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "core" / "workspaces" / "alfred-evolve" / "AGENTS.md",
        REPO_ROOT / ".agents" / "agents" / "tdd-red.md",
        REPO_ROOT / ".agents" / "agents" / "tdd-green.md",
        REPO_ROOT / ".agents" / "agents" / "tdd-verifier.md",
        REPO_ROOT / ".agents" / "agents" / "plan-synthesizer.md",
    ]
    paths = AlfredPaths.from_env(tmp_path)
    evolve_svc = EvolveWorktreeService(paths, AlfredSettings(), StateStore(paths))
    record, plan_md = evolve_svc.prepare_evolution_plan("gov_1", "Gov", "Spec")
    assert record.status == "PENDING_GATE" and "alfred-core" in plan_md

    corpus_items = [(p.name, p.read_text(encoding="utf-8")) for p in evolve_docs]
    corpus_items.append(("prepare_evolution_plan", plan_md))
    for label, text in corpus_items:
        folded = fold_accents(text)
        assert "unit" in folded, f"{label} missing unit test requirement"
        assert "functional" in folded, f"{label} missing functional test requirement"
        assert (
            "e2e" in folded or "bout en bout" in folded or "bout-en-bout" in folded
        ), f"{label} missing E2E test requirement"
        assert "regression" in folded, f"{label} missing non-regression requirement"
        assert "adversarial" in folded and "swarm" in folded, f"{label} missing swarm adversarial"

    root_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    evolve_md = (REPO_ROOT / "core" / "workspaces" / "alfred-evolve" / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    for required in ("tdd-red", "tdd-green", "tdd-verifier", "implementation_plan.md"):
        assert required in root_md and required in evolve_md
    assert "plan-executor" in root_md and "neon" in root_md.lower()
    for banned in ("nou" + "nou", "pajem" + "ploi"):
        assert banned not in root_md.lower() and banned not in evolve_md.lower()


def test_e2e_operational_manifests_document_attachment_commands() -> None:
    """Verify operational manifests document list-attachments, read-attachment, & save-to-drive."""
    res_gmail = (REPO_ROOT / ".agents" / "agents" / "research-gmail-cal.md").read_text("utf-8")
    assert "list-attachments" in res_gmail and "read-attachment" in res_gmail

    plan_exec = (REPO_ROOT / ".agents" / "agents" / "plan-executor.md").read_text("utf-8")
    assert "save-attachment-to-drive" in plan_exec

    plan_synth = (REPO_ROOT / ".agents" / "agents" / "plan-synthesizer.md").read_text("utf-8")
    assert "save-attachment-to-drive" in plan_synth

    inbox_md = (REPO_ROOT / "core" / "workspaces" / "alfred-inbox" / "AGENTS.md").read_text("utf-8")
    for cmd in ("list-attachments", "read-attachment", "save-attachment-to-drive"):
        assert cmd in inbox_md
