"""Google Drive v3 connector, First-Run exhaustive scaffolder (init-drive), and 0-OCR reader."""

from __future__ import annotations

import io
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
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
        self, paths: AlfredPaths, settings: AlfredSettings, state_store: StateStore,
        drive_client: Any | None = None, sheets_client: Any | None = None,
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

    def _acquire_sync_lock(self) -> AbstractContextManager[None]:
        """Acquire reentrant Drive sync lock when supported by the StateStore."""
        if hasattr(self._state_store, "acquire_drive_sync_lock"):
            return self._state_store.acquire_drive_sync_lock()
        return nullcontext()

    def init_drive(
        self, *, preserve_watcher_token: bool = False
    ) -> tuple[DriveMetadataIndex, str]:
        """Execute First-Run exhaustive scan of Google Drive and generate taxonomy Gate plan.

        Args:
            preserve_watcher_token: When True, keep an existing drive_watcher_page_token intact.

        Returns:
            Tuple of (persisted DriveMetadataIndex, implementation_plan.md Markdown string).
        """
        with self._acquire_sync_lock():
            client = self._require_drive_client()
            raw_items = self._fetch_all_drive_items(client)
            entries = self._build_indexed_entries(raw_items)
            token_resp = client.changes().getStartPageToken().execute()
            start_token = str(token_resp.get("startPageToken") or "")
            index = self._build_index_snapshot(entries, start_token)
            self._state_store.drive_index.save(index)
            bm_upd: dict[str, Any] = {
                "drive_page_token": start_token, "drive_last_synced_at": index.last_synced_at,
            }
            self._state_store.bookmarks.update(lambda b: b.model_copy(update=(
                bm_upd if preserve_watcher_token and b.drive_watcher_page_token else {
                    **bm_upd,
                    "drive_watcher_page_token": start_token,
                    "drive_watcher_last_polled_at": index.last_synced_at,
                }
            )))
            return index, self._render_init_drive_plan(index)

    def build_family_office_discovery_map(self) -> dict[str, Any]:
        """Cluster the indexed Google Drive tree by top-level domain for First-Run onboarding.

        Returns:
            Structured dictionary grouping subfolders and key documents per top-level folder.
        """
        index = (
            self._state_store.drive_index.load()
            if self._state_store.drive_index.exists() else self.init_drive()[0]
        )
        buckets: dict[str, dict[str, Any]] = {}
        for entry in index.entries.values():
            parts = [seg for seg in entry.path.strip("/").split("/") if seg]
            root_name = parts[0] if len(parts) > 1 or entry.is_folder else "(Root)"
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
            "folder_count": index.folder_count, "file_count": index.file_count,
            "last_synced_at": index.last_synced_at,
            "domains": [
                {
                    "folder": name, "subfolders": sorted(d["subfolders"])[:20],
                    "file_count": d["file_count"], "key_documents": d["key_documents"],
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
            if self._state_store.drive_index.exists() else self.init_drive()[0]
        )
        fm = FrontmatterSchema(
            id="drive_taxonomy_and_filing_rules", title="Google Drive Taxonomy & Filing Rules",
            domain="drive_governance",
            summary=f"Drive tree ({index.folder_count} folders, {index.file_count} files).",
            keywords=["drive", "taxonomy", "filing", "rules", "folders"], drive_source_ids=[],
        )
        folders = "\n".join(f"- `{n}/`" for n in index.top_level_folders[:30]) or "- `(Root)`"
        file_lines = [f"- `{e.path}`" for e in index.entries.values() if not e.is_folder]
        samples = "\n".join(file_lines)[:900] or "- `(No files)`"
        ref_dir = self._settings.referentiel_drive_folder_name
        body = (
            f"# Google Drive Taxonomy & Filing Rules\n\n"
            f"## 1. First-Run Indexing Metrics\n"
            f"- **Indexed folders**: {index.folder_count}\n"
            f"- **Indexed files**: {index.file_count}\n"
            f"- **Last synced**: `{index.last_synced_at}`\n\n"
            f"## 2. Detected Root Folders\n{folders}\n\n"
            f"## 3. Sample Indexed Tree\n{samples}\n\n"
            f"## 4. Filing Rules\n"
            f"- Original supporting documents stay in their thematic Google Drive folders.\n"
            f"- Reference `.md` files are backed up to `{ref_dir}`.\n"
        )
        return knowledge_service.upsert_document("00_drive_taxonomy_and_filing_rules.md", fm, body)

    def sync_changes(self) -> DriveMetadataIndex:
        """Apply incremental 0-token delta updates via drive.changes().list.

        Returns:
            Updated DriveMetadataIndex persisted to drive_metadata_index.json.
        """
        with self._acquire_sync_lock():
            client = self._require_drive_client()
            index = self._state_store.drive_index.load()
            page_token = index.start_page_token or (
                self._state_store.bookmarks.load().drive_page_token or ""
            )
            if not page_token:
                return self.init_drive(preserve_watcher_token=True)[0]
            changes, new_start_token = self._fetch_changes_pages(client, page_token)

            def _apply(cur: DriveMetadataIndex) -> DriveMetadataIndex:
                """Merge Drive deltas onto latest drive_index entries under lock."""
                entries = self._merge_delta_entries(cur.entries, changes)
                return self._build_index_snapshot(entries, new_start_token)

            updated = self._state_store.drive_index.update(_apply)
            self._state_store.bookmarks.update(
                lambda b: b.model_copy(update={
                    "drive_page_token": new_start_token,
                    "drive_last_synced_at": updated.last_synced_at,
                })
            )
            return updated

    def _merge_delta_entries(
        self, prev_entries: dict[str, DriveEntry], changes: list[dict[str, Any]]
    ) -> dict[str, DriveEntry]:
        """Apply incremental Drive changes onto prev_entries, dropping external orphans."""
        entries = dict(prev_entries)
        batch_owned = {
            str(ch.get("fileId", ""))
            for ch in changes
            if str(ch.get("fileId", ""))
            and not ch.get("removed")
            and not (ch.get("file") or {}).get("trashed")
            and bool((ch.get("file") or {}).get("ownedByMe", True))
        }
        known_ids = set(entries.keys()) | batch_owned
        for change in changes:
            fid = str(change.get("fileId", ""))
            file_obj = change.get("file") or {}
            if change.get("removed") or file_obj.get("trashed"):
                entries.pop(fid, None)
            elif fid and file_obj:
                owned = bool(file_obj.get("ownedByMe", True))
                parents = [str(p) for p in file_obj.get("parents", []) if p]
                if owned or any(p in known_ids for p in parents):
                    entries[fid] = self._to_drive_entry(file_obj)
        self._populate_paths(entries)
        return entries

    def _fetch_changes_pages(
        self, client: Any, start_token: str
    ) -> tuple[list[dict[str, Any]], str]:
        """Collect paginated Drive changes before mutating local index or cursor."""
        fields = (
            "nextPageToken,newStartPageToken,"
            "changes(fileId,removed,file(id,name,mimeType,parents,modifiedTime,ownedByMe,trashed))"
        )
        collected: list[dict[str, Any]] = []
        next_token = new_start_token = start_token
        while next_token:
            resp = client.changes().list(pageToken=next_token, fields=fields).execute()
            collected.extend(resp.get("changes", []))
            new_start_token = str(resp.get("newStartPageToken") or new_start_token)
            next_token = str(resp.get("nextPageToken") or "")
        return collected, new_start_token

    def _build_index_snapshot(
        self, entries: dict[str, DriveEntry], token: str
    ) -> DriveMetadataIndex:
        """Build a DriveMetadataIndex snapshot with computed counts and top-level folders."""
        folders = [e for e in entries.values() if e.is_folder]
        files = [e for e in entries.values() if not e.is_folder]
        top_folders = sorted({
            e.path.strip("/").split("/")[0] for e in folders
            if e.path and len(e.path.strip("/").split("/")) == 1
        })
        return DriveMetadataIndex(
            start_page_token=token, last_synced_at=utc_now_iso(),
            folder_count=len(folders), file_count=len(files),
            top_level_folders=top_folders, entries=entries,
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
            (e for e in index.entries.values()
             if needle in fold_accents(e.name) or needle in fold_accents(e.path)),
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
            file_name: Reference .md file name (e.g. '02_assets_and_key_contracts.md').
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
        fields = "nextPageToken,files(id,name,mimeType,parents,modifiedTime,trashed)"
        while True:
            kw: dict[str, Any] = {"pageSize": 1000, "q": "trashed = false", "fields": fields}
            if page_token:
                kw["pageToken"] = page_token
            resp = client.files().list(**kw).execute()
            items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return items

    def _to_drive_entry(self, raw: dict[str, Any]) -> DriveEntry:
        """Convert raw Google Drive API metadata dict into a DriveEntry."""
        mime = str(raw.get("mimeType", ""))
        return DriveEntry(
            file_id=str(raw["id"]), name=str(raw.get("name", "")), mime_type=mime,
            parents=[str(p) for p in raw.get("parents", [])],
            modified_time=str(raw.get("modifiedTime", "")),
            is_folder=(mime == FOLDER_MIME), trashed=bool(raw.get("trashed", False)),
        )

    def _build_indexed_entries(self, raw_items: list[dict[str, Any]]) -> dict[str, DriveEntry]:
        """Build full path-resolved DriveEntry map from raw items."""
        entries = {
            str(it["id"]): self._to_drive_entry(it) for it in raw_items if not it.get("trashed")
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
            s["properties"]["title"] for s in sheets
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
        if found:
            folder_id = str(found[0]["id"])
        else:
            cr = client.files().create(body={"name": fname, "mimeType": FOLDER_MIME}, fields="id")
            folder_id = str(cr.execute()["id"])
        self._state_store.bookmarks.update(
            lambda b: b.model_copy(update={"referentiel_folder_id": folder_id})
        )
        return folder_id

    def _render_init_drive_plan(self, index: DriveMetadataIndex) -> str:
        """Render the First-Run Gate implementation_plan.md proposing 00_drive_taxonomy."""
        folder_bullets = "\n".join(f"- `{n}/`" for n in index.top_level_folders) or "- `(Root)`"
        sample_files = [e.path for e in list(index.entries.values()) if not e.is_folder][:10]
        file_bullets = "\n".join(f"- `{path}`" for path in sample_files) or "- `(No files)`"
        return (
            "# Google Drive First-Run Initialization Plan (`init-drive`)\n\n"
            "## 1. Exhaustive Scan Summary\n"
            f"- **Analyzed folders**: {index.folder_count}\n"
            f"- **Indexed files**: {index.file_count}\n"
            f"- **Delta token (`startPageToken`)**: `{index.start_page_token}`\n\n"
            "## 2. Detected Root Folders\n"
            f"{folder_bullets}\n\n"
            "## 3. Sample Indexed Tree\n"
            f"{file_bullets}\n\n"
            "## 4. Proposal for `00_drive_taxonomy_and_filing_rules.md`\n"
            "After validation (`Proceed` -> `alfred init-drive --execute`), Alfred will save "
            "this taxonomy in `00_drive_taxonomy_and_filing_rules.md` "
            f"and back it up to `{self._settings.referentiel_drive_folder_name}`.\n"
        )
