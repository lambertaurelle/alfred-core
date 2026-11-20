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
        self._persist_index_and_bookmark(index, start_token)
        return index, self._render_init_drive_plan(index)

    def build_family_office_discovery_map(self) -> dict[str, Any]:
        """Cluster the indexed Google Drive tree by top-level domain for First-Run onboarding.

        Returns:
            Structured dictionary grouping subfolders and key documents per top-level folder.
        """
        index = (
            self._state_store.drive_index.load()
            if self._state_store.drive_index.exists()
            else self.init_drive()[0]
        )
        buckets: dict[str, dict[str, Any]] = {}
        for entry in index.entries.values():
            parts = [seg for seg in entry.path.strip("/").split("/") if seg]
            root_name = parts[0] if len(parts) > 1 or entry.is_folder else "(Racine)"
            if root_name == self._settings.referentiel_drive_folder_name:
                continue
            b = buckets.setdefault(
                root_name, {"subfolders": set(), "file_count": 0, "key_documents": []}
            )
            if entry.is_folder and len(parts) >= 2:
                b["subfolders"].add(parts[1])
            elif not entry.is_folder:
                b["file_count"] += 1
                if len(b["key_documents"]) < 12:
                    b["key_documents"].append(
                        {"file_id": entry.file_id, "name": entry.name, "path": entry.path}
                    )
        return {
            "folder_count": index.folder_count,
            "file_count": index.file_count,
            "last_synced_at": index.last_synced_at,
            "domains": [
                {
                    "folder": name,
                    "subfolders": sorted(d["subfolders"])[:20],
                    "file_count": d["file_count"],
                    "key_documents": d["key_documents"],
                }
                for name, d in sorted(buckets.items())
            ],
        }

    def execute_init_drive_taxonomy(self, knowledge_service: KnowledgeService) -> KnowledgeDocument:
        """Persist Stage-3 00_drive_taxonomy_and_filing_rules.md from scanned Drive index.

        Args:
            knowledge_service: 3-tier YAML knowledge service handling local save & Drive backup.
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
                f"Arborescence Drive ({index.folder_count} dossiers, {index.file_count} fichiers)."
            ),
            keywords=["drive", "taxonomie", "rangement", "arborescence", "classement"],
            drive_source_ids=[],
        )
        folders = "\n".join(f"- `{n}/`" for n in index.top_level_folders[:30]) or "- `(Racine)`"
        file_lines = [f"- `{e.path}`" for e in index.entries.values() if not e.is_folder]
        samples = "\n".join(file_lines)[:900] or "- `(Aucun fichier)`"
        ref_dir = self._settings.referentiel_drive_folder_name
        body = (
            f"# Cartographie Google Drive & Règles de Rangement\n\n"
            f"## 1. Métriques d'Indexation First-Run\n"
            f"- **Dossiers indexés** : {index.folder_count}\n"
            f"- **Fichiers indexés** : {index.file_count}\n"
            f"- **Dernière synchronisation** : `{index.last_synced_at}`\n\n"
            f"## 2. Dossiers Racines Détectés\n{folders}\n\n"
            f"## 3. Échantillon d'Arborescence\n{samples}\n\n"
            f"## 4. Règles de Rangement\n"
            f"- Les pièces originales restent dans leurs dossiers thématiques Google Drive.\n"
            f"- Les fiches `.md` sont sauvegardées dans `{ref_dir}`.\n"
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
        self._persist_index_and_bookmark(updated, new_start_token)
        return updated

    def _persist_index_and_bookmark(self, index: DriveMetadataIndex, token: str) -> None:
        """Persist DriveMetadataIndex and update bookmarks atomically."""
        self._state_store.drive_index.save(index)
        self._state_store.bookmarks.update(
            lambda b: b.model_copy(
                update={"drive_page_token": token, "drive_last_synced_at": index.last_synced_at}
            )
        )

    def search_index(self, query: str) -> list[DriveEntry]:
        """Search the local Drive metadata index at 0 tokens by file name or folder path.

        Args:
            query: Substring query matched accent- and case-insensitively against name and path.

        Returns:
            Matching DriveEntry items sorted by path.
        """
        needle = fold_accents(query).strip()
        index = self._state_store.drive_index.load()
        return sorted(
            (
                e
                for e in index.entries.values()
                if needle in fold_accents(e.name) or needle in fold_accents(e.path)
            ),
            key=lambda item: item.path or item.name,
        )

    def read_file_text(self, file_id: str) -> str:
        """Read a Google Drive file natively (0-OCR first) with (file_id, modifiedTime) cache.

        Args:
            file_id: Google Drive file identifier.

        Returns:
            Extracted UTF-8 text content.

        Raises:
            PermissionError: If file_id belongs to protected_signature_drive_ids.
        """
        if file_id in self._settings.security.protected_signature_drive_ids:
            raise PermissionError(
                f"Access denied: Drive file '{file_id}' is protected by signature_vault policy."
            )
        client = self._require_drive_client()
        meta = client.files().get(fileId=file_id, fields="id,name,mimeType,modifiedTime").execute()
        modified_time, mime_type = str(meta.get("modifiedTime", "")), str(meta.get("mimeType", ""))
        cached = self._state_store.drive_read_cache.load().items.get(file_id)
        if cached is not None and cached.modified_time == modified_time:
            return cached.text
        method, text = self._extract_by_mime(client, file_id, mime_type)
        entry = DriveCachedContent(
            file_id=file_id, modified_time=modified_time, extraction_method=method, text=text
        )
        self._state_store.drive_read_cache.update(
            lambda cur: cur.model_copy(update={"items": {**cur.items, file_id: entry}})
        )
        return text

    def backup_markdown_file(self, file_name: str, content: str) -> str:
        """Back up an up-to-date .md reference file intact to _ALFRED_REFERENTIEL_ on Drive.

        Args:
            file_name: Reference .md file name (e.g. '02_patrimoine_et_contrats_cles.md').
            content: Full Markdown string including YAML frontmatter.

        Returns:
            Google Drive file_id of the created or updated backup file.
        """
        from googleapiclient.http import MediaIoBaseUpload

        client = self._require_drive_client()
        folder_id = self._ensure_referentiel_folder(client)
        safe_f = _escape_drive_query_literal(folder_id)
        safe_n = _escape_drive_query_literal(file_name)
        query = f"'{safe_f}' in parents and name = '{safe_n}' and trashed = false"
        existing = client.files().list(q=query, fields="files(id,name)").execute().get("files", [])
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")), mimetype="text/markdown", resumable=False
        )
        if existing:
            target_id = str(existing[0]["id"])
            client.files().update(fileId=target_id, media_body=media).execute()
            return target_id
        body = {"name": file_name, "parents": [folder_id], "mimeType": "text/markdown"}
        created = client.files().create(body=body, media_body=media, fields="id").execute()
        return str(created["id"])

    def get_raw_client(self) -> Any:
        """Return the underlying Google Drive v3 client, initializing it lazily when needed."""
        return self._require_drive_client()

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
            txt = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            return ("gsheet_export_csv", txt)
        api = self._sheets_client.spreadsheets()
        meta = api.get(spreadsheetId=file_id, fields="sheets.properties.title").execute()
        sheets = meta.get("sheets", []) if isinstance(meta, dict) else []
        titles = [
            s["properties"]["title"]
            for s in sheets
            if isinstance(s, dict) and isinstance(s.get("properties", {}).get("title"), str)
        ]
        if len(titles) > 1:
            sections = []
            for title in titles:
                rng = f"'{title.replace(chr(39), chr(39) * 2)}'!A1:Z500"
                resp = api.values().get(spreadsheetId=file_id, range=rng).execute()
                vals = resp.get("values", [])
                sections.append(f"[{title}]\n" + "\n".join("\t".join(map(str, r)) for r in vals))
            return ("gsheet_values_get", "\n\n".join(sections))
        rng = f"'{titles[0].replace(chr(39), chr(39) * 2)}'!A1:Z500" if titles else "A1:Z500"
        vals = api.values().get(spreadsheetId=file_id, range=rng).execute().get("values", [])
        return ("gsheet_values_get", "\n".join("\t".join(map(str, r)) for r in vals))

    def _extract_by_mime(self, client: Any, file_id: str, mime_type: str) -> tuple[str, str]:
        """Extract UTF-8 text using 0-OCR native APIs first and Vision hook only on scans."""
        if mime_type == GDOC_MIME:
            raw = client.files().export(fileId=file_id, mimeType="text/markdown").execute()
            txt = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            return ("gdoc_export_markdown", txt)
        if mime_type == GSHEET_MIME:
            return self._extract_spreadsheet_text(client, file_id)
        if mime_type == GSLIDE_MIME:
            raw = client.files().export(fileId=file_id, mimeType="text/plain").execute()
            txt = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            return ("gslide_export_text", txt)
        raw_val = client.files().get_media(fileId=file_id).execute()
        raw_bytes = raw_val if isinstance(raw_val, bytes) else str(raw_val).encode("utf-8")
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
        fname = self._settings.referentiel_drive_folder_name
        esc_name = _escape_drive_query_literal(fname)
        query = f"mimeType = '{FOLDER_MIME}' and name = '{esc_name}' and trashed = false"
        found = client.files().list(q=query, fields="files(id,name)").execute().get("files", [])
        folder_id = (
            str(found[0]["id"])
            if found
            else str(
                client.files()
                .create(body={"name": fname, "mimeType": FOLDER_MIME}, fields="id")
                .execute()["id"]
            )
        )
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
