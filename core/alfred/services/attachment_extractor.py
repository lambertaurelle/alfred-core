"""In-memory Gmail MIME traversal, filename sanitization, and secure attachment text extraction."""

from __future__ import annotations

import base64
import binascii
import io
import mimetypes
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from alfred.domain.attachment_models import GmailAttachmentMeta
from alfred.services.email_security_service import INVISIBLE_UNICODE_RE, EmailSanitizer

MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_ZIP_MEMBERS = 250
MAX_SINGLE_XML_BYTES = 10 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 25 * 1024 * 1024
ATTACHMENT_BODY_MARKERS: tuple[str, ...] = ("[Attachment:", "[Pièce jointe:")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_MIME_EXTENSION_MAP: dict[str, str] = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "text/markdown": ".md",
    "text/calendar": ".ics",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    DOCX_MIME: ".docx",
    XLSX_MIME: ".xlsx",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
}
_TEXT_EXTENSIONS = frozenset(
    {".txt", ".md", ".csv", ".tsv", ".json", ".ics", ".xml", ".yaml", ".yml"}
)
_TEXT_MIMES = frozenset(
    {"application/json", "application/xml", "application/csv", "application/javascript"}
)
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]*={0,2}$")
_UNICODE_SLASH_RE = re.compile(r"[\uff0f\u2215\\]")


def split_attachment_body(body_text: str) -> tuple[str, str, str]:
    """Split email body from appended attachment section on EN or legacy FR markers.

    Args:
        body_text: Combined email body and optional attachment text string.

    Returns:
        Tuple of (email_body, matched_marker, attachment_remainder).
    """
    if not body_text:
        return ("", "", "")
    earliest: tuple[int, str, int] | None = None
    for marker in ATTACHMENT_BODY_MARKERS:
        match = re.search(rf"(?:^|\n+){re.escape(marker)}", body_text)
        if match is not None and (earliest is None or match.start() < earliest[0]):
            earliest = (match.start(), marker, match.end())
    if earliest is None:
        return (body_text, "", "")
    split_idx, found_marker, att_start = earliest
    return (body_text[:split_idx], found_marker, body_text[att_start:])


def _safe_int(value: object) -> int:
    """Safely coerce a Gmail body size field to non-negative int, returning 0 if invalid."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isascii() and stripped.isdigit():
            return int(stripped)
    return 0


def _infer_extension(mime_type: str) -> str:
    """Resolve a deterministic file extension from a MIME type string."""
    norm = mime_type.strip().lower().split(";")[0].strip()
    if not norm:
        return ""
    if norm in _MIME_EXTENSION_MAP:
        return _MIME_EXTENSION_MAP[norm]
    return mimetypes.guess_extension(norm) or ""


def sanitize_attachment_filename(raw_name: str, index: int = 0, mime_type: str = "") -> str:
    """Sanitize a MIME filename against traversal, C0/C1/BiDi controls, and backticks.

    Args:
        raw_name: Raw untrusted filename string from MIME metadata or user input.
        index: Zero-based attachment index used for fallback naming.
        mime_type: Optional MIME type used to infer a fallback extension.

    Returns:
        Safe basename string suitable for display and Drive upload.
    """
    stripped_ctrl = "".join(
        ch
        for ch in raw_name
        if ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F) and ch not in {"`", "\u2028", "\u2029"}
    )
    no_invisible = INVISIBLE_UNICODE_RE.sub("", stripped_ctrl)
    slashed = _UNICODE_SLASH_RE.sub("/", no_invisible).strip()
    segments = [seg.strip() for seg in slashed.split("/") if seg.strip()]
    candidate = segments[-1] if segments else ""
    if candidate in {"", ".", ".."}:
        return f"attachment_{index}{_infer_extension(mime_type)}"
    return candidate


def decode_base64url_bytes(data: str) -> bytes:
    """Decode a strict ASCII URL-safe base64 string into raw bytes, returning b'' if invalid.

    Args:
        data: Base64url-encoded ASCII string.

    Returns:
        Decoded binary bytes (b"" when input is empty or invalid).
    """
    cleaned = "".join(data.split())
    if not cleaned or not cleaned.isascii() or not _B64URL_RE.fullmatch(cleaned):
        return b""
    padded = cleaned + "=" * (-len(cleaned) % 4)
    try:
        return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return b""


def _extract_part_headers(part: Mapping[str, Any]) -> dict[str, str]:
    """Build lowercase header dictionary from a Gmail MIME part headers list."""
    raw_headers = part.get("headers", [])
    if not isinstance(raw_headers, list):
        return {}
    return {
        str(item.get("name", "")).strip().lower(): str(item.get("value", "")).strip()
        for item in raw_headers
        if isinstance(item, Mapping)
    }


def _is_inline_mime_part(part: Mapping[str, Any], mime_type: str) -> bool:
    """Return True when a MIME part is marked inline or is an inline CID image."""
    headers = _extract_part_headers(part)
    disp = headers.get("content-disposition", "").strip().lower()
    has_cid = bool(headers.get("content-id", "").strip())
    return disp.startswith("inline") or (
        mime_type.startswith("image/") and has_cid and not disp.startswith("attachment")
    )


def collect_message_mime_summary(payload: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """Collect deduplicated lowercase MIME types and non-inline attachment filenames.

    Args:
        payload: Raw Gmail message payload dictionary.

    Returns:
        Tuple of (ordered_mime_types, ordered_non_inline_filenames).
    """
    mimes: list[str] = []
    filenames: list[str] = []
    stack: list[Mapping[str, Any]] = [payload]
    while stack:
        part = stack.pop()
        mt = str(part.get("mimeType", "") or "").strip().lower()
        if mt and mt not in mimes:
            mimes.append(mt)
        fname = str(part.get("filename", "") or "").strip()
        if fname and not _is_inline_mime_part(part, mt):
            safe_name = sanitize_attachment_filename(fname, index=len(filenames), mime_type=mt)
            if safe_name and safe_name not in filenames:
                filenames.append(safe_name)
        subparts = part.get("parts", [])
        if isinstance(subparts, list):
            stack.extend(reversed([c for c in subparts if isinstance(c, Mapping)]))
    return mimes, filenames


def collect_attachment_parts(
    message_id: str,
    payload: Mapping[str, Any],
) -> list[tuple[GmailAttachmentMeta, str]]:
    """Traverse a Gmail MIME tree recursively and collect indexed attachment metadata and data.

    Args:
        message_id: Parent Gmail message identifier.
        payload: Raw Gmail message payload dictionary.

    Returns:
        Ordered list of (GmailAttachmentMeta, inline_base64url_data) tuples.
    """
    collected: list[tuple[GmailAttachmentMeta, str]] = []

    def _walk(node: Mapping[str, Any]) -> None:
        """Walk MIME tree nodes depth-first in document order."""
        mime_type = str(node.get("mimeType", "")).strip().lower()
        if mime_type.startswith("multipart/"):
            subparts = node.get("parts", [])
            if isinstance(subparts, list):
                for child in subparts:
                    if isinstance(child, Mapping):
                        _walk(child)
            return

        body_raw = node.get("body", {})
        body: Mapping[str, Any] = body_raw if isinstance(body_raw, Mapping) else {}
        raw_filename = str(node.get("filename", "") or "").strip()
        attachment_id = str(body.get("attachmentId", "") or "").strip()
        if raw_filename or attachment_id:
            idx = len(collected)
            is_inline = _is_inline_mime_part(node, mime_type)
            meta = GmailAttachmentMeta(
                index=idx,
                message_id=message_id,
                attachment_id=attachment_id,
                filename=sanitize_attachment_filename(
                    raw_filename, index=idx, mime_type=mime_type
                ),
                mime_type=mime_type or "application/octet-stream",
                size_bytes=_safe_int(body.get("size")),
                is_inline=is_inline,
            )
            inline_data = "" if attachment_id else str(body.get("data", "") or "")
            collected.append((meta, inline_data))

        subparts = node.get("parts", [])
        if isinstance(subparts, list):
            for child in subparts:
                if isinstance(child, Mapping):
                    _walk(child)

    _walk(payload)
    return collected


def _apply_truncation(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate extracted text cleanly to max_chars, raising ValueError when negative."""
    if max_chars < 0:
        raise ValueError(f"max_chars must be >= 0, got {max_chars}.")
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _local_name(tag: str) -> str:
    """Return XML local tag name without namespace prefix."""
    return tag.rsplit("}", 1)[-1]


def _decode_xml_bytes(raw_xml: bytes) -> str:
    """Decode XML bytes handling UTF-8 and UTF-16 (with or without BOM) before XXE scan."""
    if raw_xml.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw_xml.decode("utf-16", errors="replace")
    sample = raw_xml[:64]
    if b"\x00" in sample:
        even_zeros = sum(1 for b in sample[0::2] if b == 0)
        odd_zeros = sum(1 for b in sample[1::2] if b == 0)
        encoding = "utf-16-be" if even_zeros > odd_zeros else "utf-16-le"
        return raw_xml.decode(encoding, errors="replace")
    return raw_xml.decode("utf-8", errors="replace")


def _read_zip_xml_safe(
    zf: zipfile.ZipFile,
    member_name: str,
    total_xml_bytes: int,
) -> tuple[ET.Element, int]:
    """Read and parse one ZIP XML member enforcing anti-zip-bomb and anti-XXE limits."""
    info = zf.getinfo(member_name)
    if info.file_size > MAX_SINGLE_XML_BYTES:
        raise ValueError(f"XML member '{member_name}' header size exceeds limit.")
    with zf.open(info, "r") as stream:
        raw_xml = stream.read(MAX_SINGLE_XML_BYTES + 1)
    if len(raw_xml) > MAX_SINGLE_XML_BYTES:
        raise ValueError(f"XML member '{member_name}' decompressed size exceeds limit.")
    new_total = total_xml_bytes + len(raw_xml)
    if new_total > MAX_TOTAL_XML_BYTES:
        raise ValueError("Total extracted XML size exceeds archive budget.")
    decoded = _decode_xml_bytes(raw_xml).lower()
    if "<!doctype" in decoded or "<!entity" in decoded:
        raise ValueError(f"Unsafe DOCTYPE/ENTITY declaration detected in '{member_name}'.")
    return ET.fromstring(raw_xml), new_total


def _extract_pdf_page_layout(page: Any) -> str:
    """Extract single-page PDF text in layout mode, falling back to plain extraction."""
    raw_page = ""
    try:
        raw_page = page.extract_text(extraction_mode="layout") or ""
    except Exception:
        raw_page = ""
    if not raw_page.strip():
        raw_page = page.extract_text() or ""
    return "\n".join(line.rstrip(" \t\r") for line in raw_page.splitlines())


def _extract_pdf_text(raw_bytes: bytes, max_chars: int) -> tuple[str, str, bool]:
    """Extract layout-preserving text from an in-memory PDF via pypdf."""
    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        joined = "\n".join(_extract_pdf_page_layout(page) for page in reader.pages)
    except Exception:
        return ("corrupted_pdf", "", False)
    normalized = re.sub(r"\n{3,}", "\n\n", joined).strip("\r\n")
    if not normalized.strip():
        return ("empty_or_scanned_pdf", "", False)
    out, trunc = _apply_truncation(normalized, max_chars)
    return ("pypdf_native_text", out, trunc)


def _extract_docx_text(raw_bytes: bytes, max_chars: int) -> tuple[str, str, bool]:
    """Extract text from a DOCX ZIP archive with anti-XXE and anti-zip-bomb guards."""
    if len(raw_bytes) > MAX_ARCHIVE_BYTES:
        return ("corrupted_or_unsafe_archive", "", False)
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
            if len(zf.infolist()) > MAX_ZIP_MEMBERS:
                return ("corrupted_or_unsafe_archive", "", False)
            root, _ = _read_zip_xml_safe(zf, "word/document.xml", 0)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError, ValueError):
        return ("corrupted_or_unsafe_archive", "", False)
    paragraphs = [
        "".join(el.itertext()).strip()
        for el in root.iter()
        if _local_name(el.tag) == "p"
    ]
    lines = [p for p in paragraphs if p]
    text = "\n".join(lines) if lines else "".join(root.itertext()).strip()
    out, trunc = _apply_truncation(text, max_chars)
    return ("docx_xml", out, trunc)


def _sheet_sort_key(name: str) -> tuple[int, str]:
    """Sort worksheet XML paths numerically when named sheet<N>.xml."""
    match = re.search(r"sheet(\d+)\.xml$", name)
    return (int(match.group(1)) if match else 999999, name)


def _extract_xlsx_cell_value(cell: ET.Element, shared: list[str]) -> str:
    """Resolve a single XLSX cell value supporting shared strings and inline strings."""
    cell_type = cell.attrib.get("t", "")
    v_text = ""
    is_elem: ET.Element | None = None
    for child in cell:
        lname = _local_name(child.tag)
        if lname == "v" and child.text is not None:
            v_text = child.text
        elif lname == "is":
            is_elem = child
    if cell_type == "s" and v_text.isascii() and v_text.isdigit():
        idx = int(v_text)
        return shared[idx] if 0 <= idx < len(shared) else ""
    if cell_type == "inlineStr" and is_elem is not None:
        return "".join(is_elem.itertext())
    return v_text or "".join(cell.itertext())


def _extract_xlsx_text(raw_bytes: bytes, max_chars: int) -> tuple[str, str, bool]:
    """Extract cell text from an XLSX ZIP archive with anti-XXE and anti-zip-bomb guards."""
    if len(raw_bytes) > MAX_ARCHIVE_BYTES:
        return ("corrupted_or_unsafe_archive", "", False)
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes), "r") as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ZIP_MEMBERS:
                return ("corrupted_or_unsafe_archive", "", False)
            names = {info.filename for info in infos}
            total_xml = 0
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                sst_root, total_xml = _read_zip_xml_safe(zf, "xl/sharedStrings.xml", total_xml)
                shared = [
                    "".join(el.itertext()) for el in sst_root if _local_name(el.tag) == "si"
                ]
            sheets = sorted(
                (n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")),
                key=_sheet_sort_key,
            )
            if not sheets:
                return ("corrupted_or_unsafe_archive", "", False)
            sheet_lines: list[str] = []
            for sheet_name in sheets:
                s_root, total_xml = _read_zip_xml_safe(zf, sheet_name, total_xml)
                sheet_lines.extend(_collect_sheet_rows(s_root, shared))
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError, ValueError):
        return ("corrupted_or_unsafe_archive", "", False)
    text = "\n".join(sheet_lines).strip()
    out, trunc = _apply_truncation(text, max_chars)
    return ("xlsx_xml", out, trunc)


def _collect_sheet_rows(sheet_root: ET.Element, shared: list[str]) -> list[str]:
    """Collect tab-delimited row lines from a parsed XLSX worksheet XML element."""
    rows: list[str] = []
    for el in sheet_root.iter():
        if _local_name(el.tag) != "row":
            continue
        vals = [
            _extract_xlsx_cell_value(cell, shared)
            for cell in el
            if _local_name(cell.tag) == "c"
        ]
        line = "\t".join(v for v in vals if v)
        if line:
            rows.append(line)
    return rows


def _extract_html_text(raw_bytes: bytes, max_chars: int) -> tuple[str, str, bool]:
    """Convert raw HTML/XHTML bytes to sanitized plain text stripping hidden CSS/Unicode."""
    raw_html = raw_bytes.decode("utf-8", errors="replace")
    text = EmailSanitizer().sanitize(
        raw_html, "", max_chars=max(max_chars + 1, 12000)
    ).clean_text
    out, trunc = _apply_truncation(text, max_chars)
    return ("html_plain_text", out, trunc)


def extract_attachment_text(
    raw_bytes: bytes,
    filename: str,
    mime_type: str,
    max_chars: int = 20000,
) -> tuple[str, str, bool]:
    """Extract UTF-8 text from an attachment byte buffer using format-specific safe readers.

    Args:
        raw_bytes: Raw binary attachment bytes in memory.
        filename: Sanitized attachment filename (used for extension detection).
        mime_type: Normalized MIME content type string.
        max_chars: Maximum returned character length before truncation.

    Returns:
        Tuple of (extraction_method, extracted_text, truncated_flag).
    """
    if max_chars < 0:
        raise ValueError(f"max_chars must be >= 0, got {max_chars}.")
    mime = mime_type.strip().lower().split(";")[0].strip()
    ext = Path(filename).suffix.lower()
    if mime == "application/pdf" or ext == ".pdf":
        return _extract_pdf_text(raw_bytes, max_chars)
    if mime == DOCX_MIME or ext == ".docx":
        return _extract_docx_text(raw_bytes, max_chars)
    if mime == XLSX_MIME or ext == ".xlsx":
        return _extract_xlsx_text(raw_bytes, max_chars)
    if mime in {"text/html", "application/xhtml+xml"} or ext in {".html", ".htm", ".xhtml"}:
        return _extract_html_text(raw_bytes, max_chars)
    if mime.startswith("text/") or mime in _TEXT_MIMES or ext in _TEXT_EXTENSIONS:
        text = raw_bytes.decode("utf-8", errors="replace").strip()
        out, trunc = _apply_truncation(text, max_chars)
        return ("utf8_text", out, trunc)
    return ("binary_not_extractable", "", False)
