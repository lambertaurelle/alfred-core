"""Pure domain models for Gmail MIME attachments and Gmail-to-Drive transfers."""

from __future__ import annotations

from pydantic import BaseModel


class GmailAttachmentMeta(BaseModel):
    """Metadata describing a single MIME attachment part inside a Gmail message."""

    index: int
    message_id: str
    attachment_id: str = ""
    filename: str
    mime_type: str
    size_bytes: int = 0
    is_inline: bool = False


class GmailAttachmentContent(BaseModel):
    """Extracted in-memory textual representation of a Gmail attachment."""

    index: int
    message_id: str
    attachment_id: str = ""
    filename: str
    mime_type: str
    size_bytes: int = 0
    extraction_method: str
    text: str = ""
    truncated: bool = False


class DriveAttachmentTransferResult(BaseModel):
    """Outcome record of transferring a Gmail attachment into a Google Drive folder."""

    message_id: str
    source_filename: str
    drive_file_id: str
    drive_name: str
    parent_id: str
    mime_type: str
    size_bytes: int
    web_view_link: str = ""
    path: str = ""
    deduplicated: bool = False
    updated: bool = False
