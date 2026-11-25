"""Service bridge connecting Gmail MIME attachments, EmailSanitizer, and Google Drive."""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from alfred.domain.attachment_models import (
    DriveAttachmentTransferResult,
    GmailAttachmentContent,
    GmailAttachmentMeta,
)
from alfred.domain.models import AlfredSettings, DriveEntry, DriveMetadataIndex, EmailMessage
from alfred.repositories.state_store import StateStore
from alfred.services.attachment_extractor import (
    collect_attachment_parts,
    decode_base64url_bytes,
    extract_attachment_text,
    sanitize_attachment_filename,
)
from alfred.services.drive_service import FOLDER_MIME, DriveService, _escape_drive_query_literal
from alfred.services.email_security_service import EmailSanitizer
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_eligibility import DOCUMENT_ATTACHMENT_EXTENSIONS

MAX_ENRICHED_ATTACHMENTS = 5


class GmailDriveBridgeService:
    """List, read in-memory, sanitize, and transfer Gmail attachments into Google Drive."""

    def __init__(
        self,
        settings: AlfredSettings,
        state_store: StateStore,
        gmail_service: GmailService,
        drive_service: DriveService,
    ) -> None:
        """Initialize the Gmail-to-Drive attachment bridge service.

        Args:
            settings: Runtime structural settings including protected Drive vault IDs.
            state_store: Atomic state facade holding DriveMetadataIndex.
            gmail_service: Gmail v1 connector providing raw discovery client access.
            drive_service: Google Drive v3 connector providing raw discovery client access.
        """
        self._settings = settings
        self._state_store = state_store
        self._gmail = gmail_service
        self._drive = drive_service

    def list_message_attachments(self, message_id: str) -> list[GmailAttachmentMeta]:
        """List all indexed MIME attachment parts on a Gmail message without downloading blobs.

        Args:
            message_id: Target Gmail message identifier.

        Returns:
            Ordered list of GmailAttachmentMeta descriptors.
        """
        payload = self._fetch_message_payload(message_id)
        if payload is None:
            return []
        return [meta for meta, _ in collect_attachment_parts(message_id, payload)]

    def read_message_attachment(
        self,
        message_id: str,
        filename: str = "",
        index: int | None = None,
        attachment_id: str = "",
        max_chars: int = 20000,
    ) -> GmailAttachmentContent:
        """Download and extract text in-memory for a selected Gmail attachment.

        Args:
            message_id: Target Gmail message identifier.
            filename: Optional exact, case-insensitive, or unique substring filename selector.
            index: Optional zero-based attachment index selector.
            attachment_id: Optional Gmail attachmentId selector (highest priority).
            max_chars: Maximum extracted character count before truncation.

        Returns:
            GmailAttachmentContent containing extraction_method, text, and truncation status.
        """
        payload = self._fetch_message_payload(message_id)
        if payload is None:
            raise ValueError(f"Gmail message '{message_id}' payload is unavailable.")
        parts = collect_attachment_parts(message_id, payload)
        meta, inline_b64 = self._select_attachment_part(
            parts, filename=filename, index=index, attachment_id=attachment_id,
            allow_inline_fallback=False,
        )
        raw_bytes = self._download_part_bytes(message_id, meta, inline_b64)
        method, text, truncated = extract_attachment_text(
            raw_bytes, meta.filename, meta.mime_type, max_chars=max_chars
        )
        return GmailAttachmentContent(
            index=meta.index, message_id=message_id, attachment_id=meta.attachment_id,
            filename=meta.filename, mime_type=meta.mime_type,
            size_bytes=meta.size_bytes or len(raw_bytes),
            extraction_method=method, text=text, truncated=truncated,
        )

    def enrich_email_with_attachments(
        self,
        msg: EmailMessage,
        sanitizer: EmailSanitizer,
        max_chars_per_file: int = 3000,
    ) -> tuple[EmailMessage, list[str], bool]:
        """Enrich an EmailMessage body and brief bullets from up to 5 non-inline attachments.

        Args:
            msg: Sanitized incoming EmailMessage.
            sanitizer: EmailSanitizer used to scan extracted attachment text/HTML for injections.
            max_chars_per_file: Maximum extracted text characters retained per attachment.

        Returns:
            Tuple of (enriched_email, brief_bullets, any_suspicious_injection).
        """
        enriched, bullets, suspicious, _, _ = self.enrich_email_with_attachments_and_status(
            msg, sanitizer, max_chars_per_file=max_chars_per_file
        )
        return (enriched, bullets, suspicious)

    def enrich_email_with_attachments_and_status(
        self,
        msg: EmailMessage,
        sanitizer: EmailSanitizer,
        max_chars_per_file: int = 3000,
    ) -> tuple[EmailMessage, list[str], bool, bool | None, bool | None]:
        """Enrich EmailMessage from non-inline attachments and return doc/non-inline flags.

        Args:
            msg: Sanitized incoming EmailMessage.
            sanitizer: EmailSanitizer used to scan extracted attachment text/HTML for injections.
            max_chars_per_file: Maximum extracted text characters retained per attachment.

        Returns:
            Tuple of (enriched_email, brief_bullets, any_suspicious, has_doc, has_non_inline).
        """
        if not msg.attachment_filenames or not msg.message_id:
            return (msg, [], False, None, None)
        try:
            payload = self._fetch_message_payload(msg.message_id)
        except Exception:
            return (msg, [], False, None, None)
        if payload is None:
            return (msg, [], False, None, None)
        parts = collect_attachment_parts(msg.message_id, payload)
        all_non_inline = [(m, b64) for m, b64 in parts if not m.is_inline]
        if not all_non_inline:
            return (msg, [], False, False, False)
        has_doc = any(
            Path(m.filename).suffix.lower() in DOCUMENT_ATTACHMENT_EXTENSIONS
            for m, _ in all_non_inline
        )
        body_sections, brief_bullets, any_suspicious = self._extract_enriched_sections(
            msg.message_id, all_non_inline[:MAX_ENRICHED_ATTACHMENTS], sanitizer, max_chars_per_file
        )
        if not body_sections:
            return (msg, brief_bullets, any_suspicious, has_doc, True)
        base_body = msg.body_text.rstrip()
        combined = "\n\n".join(body_sections)
        new_body = f"{base_body}\n\n{combined}" if base_body else combined
        return (
            msg.model_copy(update={"body_text": new_body}), brief_bullets, any_suspicious,
            has_doc, True,
        )

    def _extract_enriched_sections(
        self,
        message_id: str,
        non_inline: list[tuple[GmailAttachmentMeta, str]],
        sanitizer: EmailSanitizer,
        max_chars_per_file: int,
    ) -> tuple[list[str], list[str], bool]:
        """Extract and sanitize text previews for selected non-inline attachment parts."""
        body_sections: list[str] = []
        brief_bullets: list[str] = []
        any_suspicious = False
        for meta, inline_b64 in non_inline:
            try:
                raw_bytes = self._download_part_bytes(message_id, meta, inline_b64)
            except Exception:
                continue
            method, extracted, _ = extract_attachment_text(
                raw_bytes, meta.filename, meta.mime_type, max_chars=max_chars_per_file
            )
            norm_mime = meta.mime_type.strip().lower().split(";")[0].strip()
            is_html = norm_mime in {"text/html", "application/xhtml+xml"} or (
                meta.filename.lower().endswith((".html", ".htm", ".xhtml"))
            )
            san = (
                sanitizer.sanitize(raw_bytes.decode("utf-8", errors="replace"), "")
                if is_html
                else sanitizer.sanitize("", extracted)
            )
            if san.suspicious_injection:
                any_suspicious = True
            if san.clean_text:
                body_sections.append(f"[Attachment: {meta.filename}]\n{san.clean_text}")
                preview = " ".join(san.clean_text.split())[:160]
                brief_bullets.append(
                    f"Attachment `{meta.filename}` ({meta.mime_type}, {method}): {preview}"
                )
            else:
                brief_bullets.append(f"Attachment `{meta.filename}` ({meta.mime_type}, {method})")
        return (body_sections, brief_bullets, any_suspicious)

    def transfer_attachment_to_drive(
        self,
        message_id: str,
        parent_id: str,
        filename: str = "",
        index: int | None = None,
        attachment_id: str = "",
        target_name: str = "",
        overwrite: bool = False,
    ) -> DriveAttachmentTransferResult:
        """Upload or deduplicate a Gmail attachment directly into an active Drive folder.

        Args:
            message_id: Source Gmail message identifier.
            parent_id: Destination Google Drive folder ID.
            filename: Optional attachment filename selector.
            index: Optional attachment index selector.
            attachment_id: Optional Gmail attachmentId selector.
            target_name: Optional sanitized target filename inside Google Drive.
            overwrite: When True, overwrite an existing file with the same name in parent_id.

        Returns:
            DriveAttachmentTransferResult describing the uploaded, updated, or deduplicated file.

        Raises:
            PermissionError: When parent_id or matched file is in protected_signature_drive_ids.
            ValueError: When parent_id is not an active folder or attachment selection fails.
        """
        if not message_id.strip() or not parent_id.strip():
            raise ValueError("Both message_id and parent_id must be non-empty.")
        protected = set(self._settings.security.protected_signature_drive_ids)
        if parent_id in protected:
            raise PermissionError(f"Access denied: Drive folder '{parent_id}' is protected.")
        drive_client = self._drive.get_raw_client()
        parent_name = self._validate_active_drive_folder(drive_client, parent_id, protected)

        payload = self._fetch_message_payload(message_id)
        if payload is None:
            raise ValueError(f"Gmail message '{message_id}' payload is unavailable.")
        parts = collect_attachment_parts(message_id, payload)
        meta, inline_b64 = self._select_attachment_part(
            parts, filename=filename, index=index, attachment_id=attachment_id,
            allow_inline_fallback=False,
        )
        raw_bytes = self._download_part_bytes(message_id, meta, inline_b64)
        final_name = self._resolve_target_name(target_name, meta)
        existing = self._find_existing_drive_file(drive_client, parent_id, final_name, protected)
        return self._execute_drive_transfer(
            drive_client, protected, existing, overwrite, message_id, meta,
            raw_bytes, parent_id, parent_name, final_name,
        )

    def _resolve_target_name(self, raw_target: str, meta: GmailAttachmentMeta) -> str:
        """Validate explicit target_name or fall back to sanitized attachment filename."""
        if not raw_target.strip():
            return meta.filename
        cleaned = "".join(
            ch for ch in raw_target if ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F)
        ).replace("\\", "/").replace("\uff0f", "/").replace("\u2215", "/").strip()
        segs = [s.strip() for s in cleaned.split("/") if s.strip()]
        if not segs or segs[-1] in {"", ".", ".."}:
            raise ValueError(f"Invalid target_name '{raw_target}'.")
        return sanitize_attachment_filename(raw_target, index=meta.index, mime_type=meta.mime_type)

    def _fetch_message_payload(self, message_id: str) -> dict[str, Any] | None:
        """Fetch raw Gmail message payload dict, returning None on invalid/mock responses."""
        if not message_id or not hasattr(self._gmail, "get_raw_client"):
            return None
        client = self._gmail.get_raw_client()
        raw = client.users().messages().get(userId="me", id=message_id, format="full").execute()
        if not isinstance(raw, Mapping):
            return None
        payload = raw.get("payload")
        return dict(payload) if isinstance(payload, Mapping) else None

    def _select_attachment_part(
        self,
        parts: list[tuple[GmailAttachmentMeta, str]],
        filename: str = "",
        index: int | None = None,
        attachment_id: str = "",
        allow_inline_fallback: bool = True,
    ) -> tuple[GmailAttachmentMeta, str]:
        """Select a single attachment part by attachment_id, index, filename, or uniqueness."""
        if not parts:
            raise ValueError("No attachments found on message.")
        if attachment_id.strip():
            needle = attachment_id.strip()
            matches = [p for p in parts if p[0].attachment_id == needle]
            if len(matches) == 1:
                return matches[0]
            raise ValueError(f"Attachment ID '{needle}' not found on message.")
        if index is not None:
            for part in parts:
                if part[0].index == index:
                    return part
            raise ValueError(f"Attachment index {index} out of range (0..{len(parts) - 1}).")
        if filename.strip():
            return self._select_by_filename(parts, filename.strip())
        non_inline = [p for p in parts if not p[0].is_inline]
        if len(non_inline) == 1:
            return non_inline[0]
        if allow_inline_fallback and not non_inline and len(parts) == 1:
            return parts[0]
        raise ValueError("Multiple or ambiguous attachments; specify filename, index, or id.")

    def _select_by_filename(
        self,
        parts: list[tuple[GmailAttachmentMeta, str]],
        needle: str,
    ) -> tuple[GmailAttachmentMeta, str]:
        """Match attachment part by exact name, case-insensitive name, or unique substring."""
        exact = [p for p in parts if p[0].filename == needle]
        if len(exact) == 1:
            return exact[0]
        low_needle = needle.lower()
        ci_exact = [p for p in parts if p[0].filename.lower() == low_needle]
        if len(ci_exact) == 1:
            return ci_exact[0]
        substr = [p for p in parts if low_needle in p[0].filename.lower()]
        if len(substr) == 1:
            return substr[0]
        raise ValueError(f"Attachment filename '{needle}' did not uniquely match any part.")

    def _download_part_bytes(
        self, message_id: str, meta: GmailAttachmentMeta, inline_b64: str
    ) -> bytes:
        """Retrieve raw attachment bytes via Gmail attachments().get() or inline body data."""
        if meta.attachment_id:
            client = self._gmail.get_raw_client()
            atts = client.users().messages().attachments()
            resp = atts.get(userId="me", messageId=message_id, id=meta.attachment_id).execute()
            b64_data = str(resp.get("data", "") or "") if isinstance(resp, Mapping) else ""
            return decode_base64url_bytes(b64_data)
        return decode_base64url_bytes(inline_b64)

    def _validate_active_drive_folder(
        self, drive_client: Any, parent_id: str, protected: set[str]
    ) -> str:
        """Verify parent_id is an active Drive folder outside protected signature ancestry."""
        meta = drive_client.files().get(
            fileId=parent_id, fields="id,name,mimeType,parents,trashed"
        ).execute()
        if (
            not isinstance(meta, Mapping)
            or meta.get("mimeType") != FOLDER_MIME
            or bool(meta.get("trashed", False))
        ):
            raise ValueError(f"Drive parent_id '{parent_id}' is not an active Drive folder.")
        parents = meta.get("parents")
        if isinstance(parents, list) and any(str(p) in protected for p in parents):
            raise PermissionError(
                f"Access denied: Drive folder '{parent_id}' is inside a protected parent."
            )
        return str(meta.get("name", "") or "")

    def _find_existing_drive_file(
        self, drive_client: Any, parent_id: str, final_name: str, protected: set[str]
    ) -> Mapping[str, Any] | None:
        """Look up a non-trashed non-folder file named final_name inside parent_id."""
        safe_parent = _escape_drive_query_literal(parent_id)
        safe_name = _escape_drive_query_literal(final_name)
        query = (
            f"'{safe_parent}' in parents and name = '{safe_name}' "
            f"and mimeType != '{FOLDER_MIME}' and trashed = false"
        )
        resp = drive_client.files().list(
            q=query, fields="files(id,name,mimeType,size,webViewLink,modifiedTime)"
        ).execute()
        files = resp.get("files", []) if isinstance(resp, Mapping) else []
        non_folders: list[Mapping[str, Any]] = []
        for item in files:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("id", "")) in protected:
                raise PermissionError("Access denied: matched Drive item is protected.")
            if item.get("mimeType") != FOLDER_MIME:
                non_folders.append(item)
        return non_folders[0] if non_folders else None

    def _upload_or_dedup_drive(
        self, drive_client: Any, protected: set[str], existing: Mapping[str, Any] | None,
        overwrite: bool, raw_bytes: bytes, upload_mime: str, parent_id: str, final_name: str,
    ) -> tuple[Mapping[str, Any], bool, bool]:
        """Deduplicate, overwrite, or create a file on Google Drive."""
        from googleapiclient.http import MediaIoBaseUpload

        fields = "id,name,mimeType,webViewLink,size,modifiedTime"
        if existing is not None:
            existing_id = str(existing.get("id", ""))
            if existing_id in protected:
                raise PermissionError(f"Access denied: Drive file '{existing_id}' is protected.")
            if not overwrite:
                return existing, True, False
            media = MediaIoBaseUpload(io.BytesIO(raw_bytes), mimetype=upload_mime, resumable=False)
            upd = drive_client.files().update(
                fileId=existing_id, body={"name": final_name}, media_body=media, fields=fields,
            )
            return upd.execute(), False, True
        media = MediaIoBaseUpload(io.BytesIO(raw_bytes), mimetype=upload_mime, resumable=False)
        body = {"name": final_name, "parents": [parent_id], "mimeType": upload_mime}
        created = drive_client.files().create(body=body, media_body=media, fields=fields).execute()
        return created, False, False

    def _execute_drive_transfer(
        self, drive_client: Any, protected: set[str], existing: Mapping[str, Any] | None,
        overwrite: bool, message_id: str, meta: GmailAttachmentMeta, raw_bytes: bytes,
        parent_id: str, parent_name: str, final_name: str,
    ) -> DriveAttachmentTransferResult:
        """Perform deduplication, overwrite update, or creation and sync DriveMetadataIndex."""
        upload_mime = meta.mime_type or "application/octet-stream"
        res_meta, dedup, updated = self._upload_or_dedup_drive(
            drive_client, protected, existing, overwrite,
            raw_bytes, upload_mime, parent_id, final_name,
        )
        drive_id = str(res_meta.get("id") or "").strip()
        if not drive_id:
            raise ValueError("Google Drive API response missing required 'id'.")
        drive_name = str(res_meta.get("name") or final_name)
        drive_mime = str(res_meta.get("mimeType") or upload_mime)
        raw_sz = res_meta.get("size")
        sz_str = str(raw_sz).strip() if raw_sz is not None else ""
        if isinstance(raw_sz, int) and not isinstance(raw_sz, bool):
            size_val = max(0, raw_sz)
        elif sz_str.isascii() and sz_str.isdigit():
            size_val = int(sz_str)
        else:
            size_val = len(raw_bytes) or meta.size_bytes
        path = self._upsert_drive_index_entry(
            drive_id, drive_name, parent_id, parent_name, drive_mime,
            modified_time=str(res_meta.get("modifiedTime") or ""),
        )
        return DriveAttachmentTransferResult(
            message_id=message_id, source_filename=meta.filename,
            drive_file_id=drive_id, drive_name=drive_name, parent_id=parent_id,
            mime_type=drive_mime, size_bytes=size_val,
            web_view_link=str(res_meta.get("webViewLink") or ""),
            path=path, deduplicated=dedup, updated=updated,
        )

    def _upsert_drive_index_entry(
        self, drive_file_id: str, drive_name: str, parent_id: str,
        parent_name: str, drive_mime: str, modified_time: str = "",
    ) -> str:
        """Insert or update a transferred file in StateStore.drive_index and return its path."""
        resolved_path = ""

        def _apply(cur: DriveMetadataIndex) -> DriveMetadataIndex:
            """Apply atomic DriveEntry update and recompute folder/file counts."""
            nonlocal resolved_path
            parent_entry = cur.entries.get(parent_id)
            if parent_entry is not None and parent_entry.path:
                resolved_path = f"{parent_entry.path.rstrip('/')}/{drive_name}"
            elif parent_name:
                resolved_path = f"/{parent_name.strip('/')}/{drive_name}"
            else:
                resolved_path = f"/{drive_name}"
            entry = DriveEntry(
                file_id=drive_file_id, name=drive_name, mime_type=drive_mime,
                parents=[parent_id], modified_time=modified_time, path=resolved_path,
                is_folder=False, trashed=False,
            )
            entries = {**cur.entries, drive_file_id: entry}
            folders = sum(1 for e in entries.values() if e.is_folder)
            files = sum(1 for e in entries.values() if not e.is_folder)
            return cur.model_copy(
                update={"entries": entries, "folder_count": folders, "file_count": files}
            )

        self._state_store.drive_index.update(_apply)
        return resolved_path
