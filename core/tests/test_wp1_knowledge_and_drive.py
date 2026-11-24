"""WP1 tests for atomic JSON/JSONL persistence, 3-tier YAML knowledge, and DriveService."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pypdf import PdfWriter

from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    FrontmatterSchema,
    GoogleSyncBookmarks,
    ProcessedItemEvent,
)
from alfred.repositories.json_repository import JsonlEventLog, JsonStateRepository
from alfred.repositories.state_store import StateStore
from alfred.services.drive_service import DriveService
from alfred.services.google_auth import GoogleOAuthService
from alfred.services.knowledge_service import KnowledgeService


def _make_paths(tmp_path: Path) -> AlfredPaths:
    """Create isolated AlfredPaths in tmp_path populated with clean knowledge templates."""
    paths = AlfredPaths.from_env(tmp_path)
    paths.templates_knowledge_dir.mkdir(parents=True, exist_ok=True)
    tpl_dir = Path(__file__).resolve().parents[2] / "core" / "templates" / "knowledge"
    for tpl in tpl_dir.glob("*.md"):
        (paths.templates_knowledge_dir / tpl.name).write_text(
            tpl.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    return paths


def _build_blank_pdf_bytes() -> bytes:
    """Build a minimal 1-page blank PDF simulating a scanned image PDF without text."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_atomic_json_repository_and_state_store(tmp_path: Path) -> None:
    """Verify atomic .lock + os.replace persistence and StateStore processed item tracking."""
    repo_file = tmp_path / "state" / "bookmarks.json"
    repo = JsonStateRepository(repo_file, GoogleSyncBookmarks)
    assert not repo.exists()
    assert repo.load().gmail_history_id is None

    updated = repo.update(
        lambda b: b.model_copy(update={"gmail_history_id": "9001", "drive_page_token": "tok_1"})
    )
    assert repo.exists()
    assert updated.gmail_history_id == "9001"
    assert (tmp_path / "state" / "bookmarks.json.lock").exists()

    log_file = tmp_path / "state" / "events.jsonl"
    event_log = JsonlEventLog(log_file, ProcessedItemEvent)
    assert event_log.read_all() == []

    paths = _make_paths(tmp_path)
    store = StateStore(paths)
    assert not store.has_processed_item("gmail", "msg_1")
    store.record_processed_item("gmail", "msg_1", "raw-payload", "SILENT_NO_ACTION")
    assert store.has_processed_item("gmail", "msg_1")


def test_knowledge_service_progressive_tiers_and_backup(tmp_path: Path) -> None:
    """Verify Tier-1 frontmatter scan, Tier-2 <=150L body load, and Drive .md backup."""
    paths = _make_paths(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    backups: list[tuple[str, str]] = []

    def record_backup(name: str, raw: str) -> str:
        """Record backed-up Markdown content."""
        backups.append((name, raw))
        return f"fid_{name}"

    svc = KnowledgeService(
        paths=paths,
        settings=settings,
        state_store=store,
        backup_fn=record_backup,
    )
    bootstrapped = svc.bootstrap_templates_if_empty()
    assert len(bootstrapped) >= 2
    assert all("assistante_maternelle" not in name for name in bootstrapped)
    assert len(svc.scan_frontmatters()) == len(bootstrapped)

    new_fm = FrontmatterSchema(
        id="patrimoine_immobilier",
        title="Patrimoine Immobilier et Baux",
        domain="immobilier",
        summary="Synthèse des baux signés avec échéances et préavis.",
        keywords=["immobilier", "bail", "préavis"],
        drive_source_ids=["drive_pdf_01"],
    )
    saved = svc.upsert_document("02_patrimoine_immobilier.md", new_fm, "# Bail\n- 3 mois\n")
    assert saved.frontmatter.drive_source_ids == ["drive_pdf_01"]
    assert len(backups) == 1
    assert len(store.knowledge_audit.read_all()) == 1

    ranked = svc.select_relevant_frontmatters("bail immobilier echeances")
    assert ranked[0][1].id == "patrimoine_immobilier"

    # Accent-insensitive French matching via fold_accents()
    filing_hits = svc.select_relevant_frontmatters("filing rules")
    assert filing_hits and filing_hits[0][1].id == "drive_taxonomy_and_filing_rules"
    synth_hits = svc.select_relevant_frontmatters("synthese echeances")
    assert synth_hits and synth_hits[0][1].id == "patrimoine_immobilier"
    preavis_hits = svc.select_relevant_frontmatters("quel est le preavis")
    assert preavis_hits and preavis_hits[0][1].id == "patrimoine_immobilier"

    doc = svc.load_document("patrimoine_immobilier")
    assert "3 mois" in doc.body

    with pytest.raises(ValueError, match="150"):
        svc.upsert_document(
            "02_patrimoine_immobilier.md",
            new_fm,
            "\n".join(f"L{i}" for i in range(155)),
        )
    with pytest.raises(ValueError, match="Invalid"):
        svc.upsert_document("../escape.md", new_fm, "# Bad\n")
    with pytest.raises(FileNotFoundError):
        svc.load_document("non_existent_doc")


def test_drive_init_and_delta_sync(tmp_path: Path) -> None:
    """Verify First-Run init-drive exhaustive tree scan, taxonomy plan, and changes().list."""
    paths = _make_paths(tmp_path)
    store = StateStore(paths)
    drive_client = MagicMock()
    files_data: list[dict[str, Any]] = [
        {
            "id": "fld_1",
            "name": "01_Administratif",
            "mimeType": "application/vnd.google-apps.folder",
            "parents": ["root"],
            "modifiedTime": "2026-01-01T10:00:00Z",
        },
        {
            "id": "doc_1",
            "name": "Contrat_Assurance_Habitation",
            "mimeType": "application/vnd.google-apps.document",
            "parents": ["fld_1"],
            "modifiedTime": "2026-02-01T10:00:00Z",
        },
    ]
    drive_client.files.return_value.list.return_value.execute.return_value = {"files": files_data}
    drive_client.changes.return_value.getStartPageToken.return_value.execute.return_value = {
        "startPageToken": "tok_100"
    }
    drive_client.changes.return_value.list.return_value.execute.return_value = {
        "changes": [
            {
                "fileId": "txt_2",
                "removed": False,
                "file": {
                    "id": "txt_2",
                    "name": "Note.txt",
                    "mimeType": "text/plain",
                    "parents": ["fld_1"],
                    "modifiedTime": "2026-03-01T00:00:00Z",
                },
            }
        ],
        "newStartPageToken": "tok_101",
    }

    svc = DriveService(paths, AlfredSettings.load(paths), store, drive_client=drive_client)
    index, plan_md = svc.init_drive()
    assert index.folder_count == 1
    assert index.file_count == 1
    assert index.entries["doc_1"].path == "/01_Administratif/Contrat_Assurance_Habitation"
    assert "00_drive_taxonomy_and_filing_rules.md" in plan_md
    assert len(svc.search_index("Contrat")) == 1

    synced = svc.sync_changes()
    assert "txt_2" in synced.entries
    assert synced.start_page_token == "tok_101"

    know = KnowledgeService(paths, AlfredSettings.load(paths), store)
    tax_doc = svc.execute_init_drive_taxonomy(know)
    assert tax_doc.file_name == "00_drive_taxonomy_and_filing_rules.md"
    assert "01_Administratif" in tax_doc.body


def test_drive_native_zero_ocr_readers_cache_and_backup(tmp_path: Path) -> None:
    """Verify Docs export, Sheets values.get, pypdf/Vision fallback, cache, and .md backup."""
    paths = _make_paths(tmp_path)
    store = StateStore(paths)
    drive_client = MagicMock()
    sheets_client = MagicMock()

    meta_by_id = {
        "doc_1": {
            "id": "doc_1",
            "name": "Doc",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "t1",
        },
        "sh_1": {
            "id": "sh_1",
            "name": "Sheet",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "modifiedTime": "t1",
        },
        "scan_1": {
            "id": "scan_1",
            "name": "Scan.pdf",
            "mimeType": "application/pdf",
            "modifiedTime": "t1",
        },
    }
    drive_client.files.return_value.get.side_effect = (
        lambda fileId, **_kw: MagicMock(execute=lambda: meta_by_id[fileId])  # noqa: N803
    )
    drive_client.files.return_value.export.return_value.execute.return_value = b"# Markdown Doc"
    drive_client.files.return_value.get_media.return_value.execute.return_value = (
        _build_blank_pdf_bytes()
    )
    vals_get = sheets_client.spreadsheets.return_value.values.return_value.get
    vals_get.return_value.execute.return_value = {"values": [["ColA", "ColB"], ["1", "2"]]}

    svc = DriveService(
        paths,
        AlfredSettings.load(paths),
        store,
        drive_client=drive_client,
        sheets_client=sheets_client,
        vision_ocr_fn=lambda _b, fid: f"VISION_{fid}",
    )
    assert svc.read_file_text("doc_1") == "# Markdown Doc"
    assert svc.read_file_text("doc_1") == "# Markdown Doc"
    assert drive_client.files.return_value.export.call_count == 1
    assert "ColA\tColB" in svc.read_file_text("sh_1")
    assert svc.read_file_text("scan_1") == "VISION_scan_1"


def test_drive_backup_markdown_to_referentiel(tmp_path: Path) -> None:
    """Verify unidirectional .md backup creates and updates files in _ALFRED_REFERENTIEL_."""
    paths = _make_paths(tmp_path)
    store = StateStore(paths)
    drive_client = MagicMock()
    svc = DriveService(paths, AlfredSettings.load(paths), store, drive_client=drive_client)

    drive_client.files.return_value.list.side_effect = [
        MagicMock(execute=lambda: {"files": []}),
        MagicMock(execute=lambda: {"files": []}),
        MagicMock(execute=lambda: {"files": [{"id": "md_1", "name": "ref.md"}]}),
    ]
    drive_client.files.return_value.create.side_effect = [
        MagicMock(execute=lambda: {"id": "fld_ref"}),
        MagicMock(execute=lambda: {"id": "md_1"}),
    ]
    assert svc.backup_markdown_file("ref.md", "---\nid: a\n---\n") == "md_1"
    assert svc.backup_markdown_file("ref.md", "---\nid: a\n---\n") == "md_1"


def test_drive_sheet_csv_export_multitab_and_slides(tmp_path: Path) -> None:
    """Verify Sheets CSV fallback when sheets_client is None, multi-tab Sheets, and Slides."""
    paths = _make_paths(tmp_path)
    store = StateStore(paths)
    drive_client = MagicMock()
    meta_map = {
        "sh_csv": {
            "id": "sh_csv",
            "name": "Budget",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "modifiedTime": "t1",
        },
        "sh_multi": {
            "id": "sh_multi",
            "name": "Multi",
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "modifiedTime": "t2",
        },
        "sl_1": {
            "id": "sl_1",
            "name": "Deck",
            "mimeType": "application/vnd.google-apps.presentation",
            "modifiedTime": "t3",
        },
    }
    drive_client.files.return_value.get.side_effect = (
        lambda fileId, **_kw: MagicMock(execute=lambda: meta_map[fileId])  # noqa: N803
    )
    drive_client.files.return_value.export.return_value.execute.return_value = b"A,B\n1,2"

    no_sheets_svc = DriveService(
        paths, AlfredSettings.load(paths), store, drive_client=drive_client, sheets_client=None
    )
    assert no_sheets_svc.read_file_text("sh_csv") == "A,B\n1,2"
    assert not drive_client.files.return_value.get_media.called
    assert no_sheets_svc.read_file_text("sl_1") == "A,B\n1,2"

    sheets_client = MagicMock()
    sheets_client.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "Jan"}}, {"properties": {"title": "Feb"}}]
    }
    vals_api = sheets_client.spreadsheets.return_value.values.return_value
    vals_api.get.return_value.execute.return_value = {"values": [["K", "V"]]}
    multi_svc = DriveService(
        paths,
        AlfredSettings.load(paths),
        store,
        drive_client=drive_client,
        sheets_client=sheets_client,
    )
    multi_txt = multi_svc.read_file_text("sh_multi")
    assert "[Jan]" in multi_txt and "[Feb]" in multi_txt


def test_google_oauth_refresh_and_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify OAuth2 token expiry auto-refresh persistence and InstalledAppFlow bootstrap."""
    paths = _make_paths(tmp_path)
    settings = AlfredSettings.load(paths)
    oauth = GoogleOAuthService(paths, settings)

    with pytest.raises(RuntimeError, match="missing"):
        oauth.load_credentials()
    with pytest.raises(FileNotFoundError):
        oauth.bootstrap_interactive_token(open_browser=False)

    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    token_file = paths.secrets_dir / "token.json"
    token_file.write_text('{"token": "old"}', encoding="utf-8")

    fake_creds = MagicMock()
    fake_creds.valid = False
    fake_creds.expired = True
    fake_creds.refresh_token = "ref_tok"
    fake_creds.to_json.return_value = '{"token": "refreshed"}'

    import google.oauth2.credentials as gcreds

    monkeypatch.setattr(
        gcreds.Credentials,
        "from_authorized_user_file",
        classmethod(lambda _cls, *_a, **_kw: fake_creds),
    )
    loaded = oauth.load_credentials()
    assert loaded is fake_creds
    fake_creds.refresh.assert_called_once()
    assert "refreshed" in token_file.read_text(encoding="utf-8")

    fake_creds.refresh_token = ""
    with pytest.raises(RuntimeError, match="invalid/expired"):
        oauth.load_credentials()

    secrets_file = paths.secrets_dir / "client_secrets.json"
    secrets_file.write_text('{"installed": {}}', encoding="utf-8")
    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fake_creds
    import google_auth_oauthlib.flow as gflow

    monkeypatch.setattr(
        gflow.InstalledAppFlow,
        "from_client_secrets_file",
        classmethod(lambda _cls, *_a, **_kw: fake_flow),
    )
    saved_tok = oauth.bootstrap_interactive_token(port=0, open_browser=False)
    assert saved_tok == token_file
