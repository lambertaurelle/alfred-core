"""Google Drive v3 connector, First-Run exhaustive scaffolder (init-drive), and 0-OCR reader."""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

from pypdf import PdfReader

from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    DriveCachedContent,
    DriveEntry,
    DriveMetadataIndex,
    FrontmatterSchema,
    KnowledgeDocument,
    fold_accents,
    utc_now_iso,
)
from alfred.repositories.state_store import StateStore
from alfred.services.google_auth import GoogleOAuthService
from alfred.services.knowledge_service import KnowledgeService

FOLDER_MIME = "application/vnd.google-apps.folder"
GDOC_MIME = "application/vnd.google-apps.document"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
GSLIDE_MIME = "application/vnd.google-apps.presentation"
PDF_MIME = "application/pdf"


def _escape_drive_query_literal(value: str) -> str:
    """Escape backslashes and single quotes for Google Drive v3 query expressions."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class DriveService:
    """Google Drive v3 service handling init-drive, delta changes, 0-OCR read, and .md backup."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
        drive_client: Any | None = None,
        sheets_client: Any | None = None,
        vision_ocr_fn: Callable[[bytes, str], str] | None = None,
        oauth_service: GoogleOAuthService | None = None,
    ) -> None:
        """Initialize the Google Drive service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Structural runtime settings.
            state_store: Local atomic state and cache store.
            drive_client: Optional injected Google Drive v3 client.
            sheets_client: Optional injected Google Sheets v4 client.
            vision_ocr_fn: Optional Vision OCR hook for scanned image PDFs.
            oauth_service: Optional shared GoogleOAuthService instance.
        """
        self._paths = paths
        self._settings = settings
        self._state_store = state_store
        self._drive_client = drive_client
        self._sheets_client = sheets_client
        self._vision_ocr_fn = vision_ocr_fn
        self._oauth = oauth_service or GoogleOAuthService(paths, settings)

    def init_drive(self) -> tuple[DriveMetadataIndex, str]:
        """Execute First-Run exhaustive scan of Google Drive and generate taxonomy Gate plan.

        Returns:
            Tuple of (persisted DriveMetadataIndex, implementation_plan.md Markdown string).
        """
        client = self._require_drive_client()
        raw_items = self._fetch_all_drive_items(client)
        entries = self._build_indexed_entries(raw_items)
        token_resp = client.changes().getStartPageToken().execute()
        start_token = str(token_resp.get("startPageToken", ""))

        folders = [e for e in entries.values() if e.is_folder]
        files = [e for e in entries.values() if not e.is_folder]
        top_folders = sorted(
            {
                e.path.strip("/").split("/")[0]
                for e in folders
                if e.path and len(e.path.strip("/").split("/")) == 1
            }
        )
        index = DriveMetadataIndex(
            start_page_token=start_token,
            last_synced_at=utc_now_iso(),
            folder_count=len(folders),
            file_count=len(files),
            top_level_folders=top_folders,
            entries=entries,
        )
        self._state_store.drive_index.save(index)
        self._state_store.bookmarks.update(
            lambda b: b.model_copy(
                update={
                    "drive_page_token": start_token,
                    "drive_last_synced_at": index.last_synced_at,
                }
            )
        )
        plan_md = self._render_init_drive_plan(index)
        return index, plan_md

    def execute_init_drive_taxonomy(
        self,
        knowledge_service: KnowledgeService,
    ) -> KnowledgeDocument:
        """Persist Stage-3 00_drive_taxonomy_and_filing_rules.md from scanned Drive index.

        Args:
            knowledge_service: 3-tier YAML knowledge service handling local save & Drive backup.

        Returns:
            Persisted KnowledgeDocument for 00_drive_taxonomy_and_filing_rules.md.
        """
        index = (
            self._state_store.drive_index.load()
            if self._state_store.drive_index.exists()
            else self.init_drive()[0]
        )
        fm = FrontmatterSchema(
            id="drive_taxonomy_and_filing_rules",
            title="Cartographie Google Drive & Règles de Rangement",
            domain="drive_governance",
            summary=(
                f"Arborescence Google Drive indexée ({index.folder_count} dossiers, "
                f"{index.file_count} fichiers) et conventions de classement."
            ),
            keywords=["drive", "taxonomie", "rangement", "arborescence", "classement"],
            drive_source_ids=[],
        )
        folder_lines = (
            "\n".join(f"- `{name}/`" for name in index.top_level_folders[:30])
            or "- `(Racine uniquement)`"
        )
        sample_files = [e.path for e in list(index.entries.values()) if not e.is_folder][:15]
        sample_lines = "\n".join(f"- `{p}`" for p in sample_files) or "- `(Aucun fichier)`"
        body = (
            "# Cartographie Google Drive & Règles de Rangement\n\n"
            "## 1. Métriques d'Indexation First-Run\n"
            f"- **Dossiers indexés** : {index.folder_count}\n"
            f"- **Fichiers indexés** : {index.file_count}\n"
            f"- **Dernière synchronisation** : `{index.last_synced_at}`\n\n"
            "## 2. Dossiers Racines Détectés\n"
            f"{folder_lines}\n\n"
            "## 3. Échantillon d'Arborescence\n"
            f"{sample_lines}\n\n"
            "## 4. Règles de Rangement\n"
            "- Les pièces originales restent dans leurs dossiers thématiques Google Drive.\n"
            "- Les fiches `.md` de synthèse sont sauvegardées dans "
            f"`{self._settings.referentiel_drive_folder_name}`.\n"
        )
        return knowledge_service.upsert_document("00_drive_taxonomy_and_filing_rules.md", fm, body)

    def sync_changes(self) -> DriveMetadataIndex:
        """Apply incremental 0-token delta updates via drive.changes().list.

        Returns:
            Updated DriveMetadataIndex persisted to drive_metadata_index.json.
        """
        client = self._require_drive_client()
        index = self._state_store.drive_index.load()
        page_token = index.start_page_token
        if not page_token:
            synced_index, _ = self.init_drive()
            return synced_index

        entries = dict(index.entries)
        next_token = page_token
        new_start_token = page_token
        while next_token:
            resp = (
                client.changes()
                .list(
                    pageToken=next_token,
                    fields=(
                        "nextPageToken,newStartPageToken,"
                        "changes(fileId,removed,file(id,name,mimeType,parents,modifiedTime,trashed))"
                    ),
                )
                .execute()
            )
            for change in resp.get("changes", []):
                fid = str(change.get("fileId", ""))
                file_obj = change.get("file") or {}
                if change.get("removed") or file_obj.get("trashed"):
                    entries.pop(fid, None)
                elif fid and file_obj:
                    entries[fid] = self._to_drive_entry(file_obj)
            new_start_token = str(resp.get("newStartPageToken", new_start_token))
            next_token = str(resp.get("nextPageToken", ""))

        self._populate_paths(entries)
        folders = [e for e in entries.values() if e.is_folder]
        files = [e for e in entries.values() if not e.is_folder]
        updated = DriveMetadataIndex(
            start_page_token=new_start_token,
            last_synced_at=utc_now_iso(),
            folder_count=len(folders),
            file_count=len(files),
            top_level_folders=sorted(
                {
                    e.path.strip("/").split("/")[0]
                    for e in folders
                    if e.path and len(e.path.strip("/").split("/")) == 1
                }
            ),
            entries=entries,
        )
        self._state_store.drive_index.save(updated)
        self._state_store.bookmarks.update(
            lambda b: b.model_copy(
                update={
                    "drive_page_token": new_start_token,
                    "drive_last_synced_at": updated.last_synced_at,
                }
            )
        )
        return updated

    def search_index(self, query: str) -> list[DriveEntry]:
        """Search the local Drive metadata index at 0 tokens by file name or folder path.

        Args:
            query: Substring query matched accent- and case-insensitively against name and path.

        Returns:
            Matching DriveEntry items sorted by path.
        """
        needle = fold_accents(query).strip()
        index = self._state_store.drive_index.load()
        matches = [
            entry
            for entry in index.entries.values()
            if needle in fold_accents(entry.name) or needle in fold_accents(entry.path)
        ]
        matches.sort(key=lambda item: item.path or item.name)
        return matches

    def read_file_text(self, file_id: str) -> str:
        """Read a Google Drive file natively (0-OCR first) with (file_id, modifiedTime) cache.

        Args:
            file_id: Google Drive file identifier.

        Returns:
            Extracted UTF-8 text content.
        """
        client = self._require_drive_client()
        meta = client.files().get(fileId=file_id, fields="id,name,mimeType,modifiedTime").execute()
        modified_time = str(meta.get("modifiedTime", ""))
        mime_type = str(meta.get("mimeType", ""))

        cache = self._state_store.drive_read_cache.load()
        cached = cache.items.get(file_id)
        if cached is not None and cached.modified_time == modified_time:
            return cached.text

        method, text = self._extract_by_mime(client, file_id, mime_type)
        self._state_store.drive_read_cache.update(
            lambda cur: cur.model_copy(
                update={
                    "items": {
                        **cur.items,
                        file_id: DriveCachedContent(
                            file_id=file_id,
                            modified_time=modified_time,
                            extraction_method=method,
                            text=text,
                        ),
                    }
                }
            )
        )
        return text

    def backup_markdown_file(self, file_name: str, content: str) -> str:
        """Back up an up-to-date .md reference file intact to _ALFRED_REFERENTIEL_ on Drive.

        Args:
            file_name: Reference .md file name (e.g. '02_assistante_maternelle.md').
            content: Full Markdown string including YAML frontmatter.

        Returns:
            Google Drive file_id of the created or updated backup file.
        """
        from googleapiclient.http import MediaIoBaseUpload

        client = self._require_drive_client()
        folder_id = self._ensure_referentiel_folder(client)
        safe_folder = _escape_drive_query_literal(folder_id)
        safe_name = _escape_drive_query_literal(file_name)
        query = f"'{safe_folder}' in parents and name = '{safe_name}' and trashed = false"
        existing = client.files().list(q=query, fields="files(id,name)").execute().get("files", [])
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")),
            mimetype="text/markdown",
            resumable=False,
        )
        if existing:
            target_id = str(existing[0]["id"])
            client.files().update(fileId=target_id, media_body=media).execute()
            return target_id
        created = (
            client.files()
            .create(
                body={"name": file_name, "parents": [folder_id], "mimeType": "text/markdown"},
                media_body=media,
                fields="id",
            )
            .execute()
        )
        return str(created["id"])

    def _require_drive_client(self) -> Any:
        """Return injected Drive client or build one lazily via GoogleOAuthService."""
        if self._drive_client is not None:
            return self._drive_client
        from googleapiclient.discovery import build

        creds = self._oauth.load_credentials()
        self._drive_client = build("drive", "v3", credentials=creds)
        if self._sheets_client is None:
            self._sheets_client = build("sheets", "v4", credentials=creds)
        return self._drive_client

    def _fetch_all_drive_items(self, client: Any) -> list[dict[str, Any]]:
        """Paginate through the entire non-trashed Google Drive file list."""
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "pageSize": 1000,
                "q": "trashed = false",
                "fields": "nextPageToken,files(id,name,mimeType,parents,modifiedTime,trashed)",
            }
            if page_token:
                kwargs["pageToken"] = page_token
            resp = client.files().list(**kwargs).execute()
            items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items

    def _to_drive_entry(self, raw: dict[str, Any]) -> DriveEntry:
        """Convert raw Google Drive API metadata dict into a DriveEntry."""
        mime = str(raw.get("mimeType", ""))
        return DriveEntry(
            file_id=str(raw["id"]),
            name=str(raw.get("name", "")),
            mime_type=mime,
            parents=[str(p) for p in raw.get("parents", [])],
            modified_time=str(raw.get("modifiedTime", "")),
            is_folder=(mime == FOLDER_MIME),
            trashed=bool(raw.get("trashed", False)),
        )

    def _build_indexed_entries(self, raw_items: list[dict[str, Any]]) -> dict[str, DriveEntry]:
        """Build full path-resolved DriveEntry map from raw items."""
        entries = {
            str(item["id"]): self._to_drive_entry(item)
            for item in raw_items
            if not item.get("trashed", False)
        }
        self._populate_paths(entries)
        return entries

    def _populate_paths(self, entries: dict[str, DriveEntry]) -> None:
        """Resolve hierarchical '/Folder/Subfolder/File' paths in-place."""
        for fid, entry in list(entries.items()):
            segments = [entry.name]
            seen = {fid}
            parent_id = entry.parents[0] if entry.parents else ""
            while parent_id and parent_id in entries and parent_id not in seen:
                seen.add(parent_id)
                parent = entries[parent_id]
                segments.append(parent.name)
                parent_id = parent.parents[0] if parent.parents else ""
            segments.reverse()
            entries[fid] = entry.model_copy(update={"path": "/" + "/".join(segments)})

    def _extract_spreadsheet_text(self, client: Any, file_id: str) -> tuple[str, str]:
        """Extract Google Sheets text via Sheets v4 (all tabs) or Drive CSV export fallback."""
        if self._sheets_client is None:
            raw = client.files().export(fileId=file_id, mimeType="text/csv").execute()
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            return ("gsheet_export_csv", text)
        spreadsheets_api = self._sheets_client.spreadsheets()
        meta = spreadsheets_api.get(
            spreadsheetId=file_id,
            fields="sheets.properties.title",
        ).execute()
        titles = (
            [
                s["properties"]["title"]
                for s in meta.get("sheets", [])
                if isinstance(s, dict)
                and isinstance(s.get("properties", {}).get("title"), str)
            ]
            if isinstance(meta, dict)
            else []
        )
        if len(titles) > 1:
            sections: list[str] = []
            for title in titles:
                escaped = title.replace("'", "''")
                resp = (
                    spreadsheets_api.values()
                    .get(spreadsheetId=file_id, range=f"'{escaped}'!A1:Z500")
                    .execute()
                )
                rows = ["\t".join(str(c) for c in row) for row in resp.get("values", [])]
                sections.append(f"[{title}]\n" + "\n".join(rows))
            return ("gsheet_values_get", "\n\n".join(sections))
        rng = f"'{titles[0].replace(chr(39), chr(39) * 2)}'!A1:Z500" if titles else "A1:Z500"
        resp = spreadsheets_api.values().get(spreadsheetId=file_id, range=rng).execute()
        rows = ["\t".join(str(cell) for cell in row) for row in resp.get("values", [])]
        return ("gsheet_values_get", "\n".join(rows))

    def _extract_by_mime(self, client: Any, file_id: str, mime_type: str) -> tuple[str, str]:
        """Extract UTF-8 text using 0-OCR native APIs first and Vision hook only on scans."""
        if mime_type == GDOC_MIME:
            raw = client.files().export(fileId=file_id, mimeType="text/markdown").execute()
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            return ("gdoc_export_markdown", text)
        if mime_type == GSHEET_MIME:
            return self._extract_spreadsheet_text(client, file_id)
        if mime_type == GSLIDE_MIME:
            raw = client.files().export(fileId=file_id, mimeType="text/plain").execute()
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            return ("gslide_export_text", text)
        raw_bytes_or_str = client.files().get_media(fileId=file_id).execute()
        raw_bytes = (
            raw_bytes_or_str
            if isinstance(raw_bytes_or_str, bytes)
            else str(raw_bytes_or_str).encode("utf-8")
        )
        if mime_type == PDF_MIME:
            reader = PdfReader(io.BytesIO(raw_bytes))
            extracted = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
            if extracted:
                return ("pypdf_native_text", extracted)
            if self._vision_ocr_fn is not None:
                return ("vision_ocr_fallback", self._vision_ocr_fn(raw_bytes, file_id))
            return ("vision_ocr_pending", f"[VISION_OCR_REQUIRED:{file_id}]")
        return ("raw_utf8", raw_bytes.decode("utf-8", errors="replace"))

    def _ensure_referentiel_folder(self, client: Any) -> str:
        """Locate or create the _ALFRED_REFERENTIEL_ backup folder on Google Drive."""
        bookmarks = self._state_store.bookmarks.load()
        if bookmarks.referentiel_folder_id:
            return bookmarks.referentiel_folder_id
        folder_name = self._settings.referentiel_drive_folder_name
        safe_folder = _escape_drive_query_literal(folder_name)
        query = f"mimeType = '{FOLDER_MIME}' and name = '{safe_folder}' and trashed = false"
        found = client.files().list(q=query, fields="files(id,name)").execute().get("files", [])
        if found:
            folder_id = str(found[0]["id"])
        else:
            created = (
                client.files()
                .create(body={"name": folder_name, "mimeType": FOLDER_MIME}, fields="id")
                .execute()
            )
            folder_id = str(created["id"])
        self._state_store.bookmarks.update(
            lambda b: b.model_copy(update={"referentiel_folder_id": folder_id})
        )
        return folder_id

    def _render_init_drive_plan(self, index: DriveMetadataIndex) -> str:
        """Render the First-Run Gate implementation_plan.md proposing 00_drive_taxonomy."""
        folder_bullets = (
            "\n".join(f"- `{name}/`" for name in index.top_level_folders) or "- `(Racine)`"
        )
        sample_files = [e.path for e in list(index.entries.values()) if not e.is_folder][:10]
        file_bullets = "\n".join(f"- `{path}`" for path in sample_files) or "- `(Aucun fichier)`"
        return (
            "# Plan d'Initialisation First-Run Google Drive (`init-drive`)\n\n"
            "## 1. Synthèse de l'Analyse Exhaustive\n"
            f"- **Dossiers analysés** : {index.folder_count}\n"
            f"- **Fichiers indexés** : {index.file_count}\n"
            f"- **Jeton Delta (`startPageToken`)** : `{index.start_page_token}`\n\n"
            "## 2. Dossiers Racines Détectés\n"
            f"{folder_bullets}\n\n"
            "## 3. Échantillon d'Arborescence Indexée\n"
            f"{file_bullets}\n\n"
            "## 4. Proposition pour `00_drive_taxonomy_and_filing_rules.md`\n"
            "Après validation (`Proceed` -> `alfred init-drive --execute`), Alfred enregistrera "
            "cette cartographie dans `00_drive_taxonomy_and_filing_rules.md` "
            f"et la sauvegardera dans `{self._settings.referentiel_drive_folder_name}`.\n"
        )
