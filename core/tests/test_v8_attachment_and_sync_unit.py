"""Unit tests for Gmail/Drive attachments, delegation timeouts, and stale sync detection."""

from __future__ import annotations

import base64
import io
import os
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pydantic
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from alfred.domain.feature_models import SecurityConfig
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.repositories.state_store import StateStore
from alfred.services.agy_daemon_sync import AgyDaemonSyncService
from alfred.services.delegation_service import DelegationService
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService

REPO_ROOT = Path(__file__).resolve().parents[2]


def _b64u(raw: bytes) -> str:
    """Encode raw bytes into an unpadded URL-safe base64 string.

    Args:
        raw: Raw binary payload to encode.

    Returns:
        URL-safe base64 string without trailing '=' padding.
    """
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _build_sample_pdf(text: str = "") -> bytes:
    """Build an in-memory 1-page PDF with optional Helvetica text stream.

    Args:
        text: Optional ASCII text embedded into the PDF content stream.

    Returns:
        Raw serialized PDF bytes.
    """
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    if text:
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
        font_dict = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        fonts = DictionaryObject({NameObject("/F1"): writer._add_object(font_dict)})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _build_zip_bytes(entries: Mapping[str, bytes]) -> bytes:
    """Build an in-memory deflate-compressed ZIP archive from path-to-bytes entries.

    Args:
        entries: Mapping of archive member names to raw member bytes.

    Returns:
        Serialized ZIP archive bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _build_docx_bytes(paragraphs: list[str], extra_xml: str = "") -> bytes:
    """Build a minimal valid in-memory DOCX archive containing word/document.xml.

    Args:
        paragraphs: List of plain-text paragraph strings.
        extra_xml: Optional XML preamble inserted before the root element.

    Returns:
        Serialized DOCX ZIP archive bytes.
    """
    para_xml = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    doc_xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>{extra_xml}'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{para_xml}</w:body></w:document>"
    )
    return _build_zip_bytes({"word/document.xml": doc_xml.encode("utf-8")})


def _build_xlsx_bytes(shared: list[str], rows: list[list[tuple[str, str]]]) -> bytes:
    """Build a minimal valid in-memory XLSX archive with sharedStrings and sheet1.

    Args:
        shared: Shared string table entries referenced by 's' cells.
        rows: List of spreadsheet rows, each containing (cell_type, value) pairs.

    Returns:
        Serialized XLSX ZIP archive bytes.
    """
    si_xml = "".join(f"<si><t>{txt}</t></si>" for txt in shared)
    sst_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"{si_xml}</sst>"
    )
    row_chunks: list[str] = []
    for r_idx, row in enumerate(rows, start=1):
        cells: list[str] = []
        for c_idx, (c_type, c_val) in enumerate(row):
            t_attr = ' t="' + c_type + '"' if c_type else ""
            col = chr(ord("A") + c_idx)
            cells.append(f'<c r="{col}{r_idx}"{t_attr}><v>{c_val}</v></c>')
        row_chunks.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_chunks)}</sheetData></worksheet>'
    )
    return _build_zip_bytes(
        {"xl/sharedStrings.xml": sst_xml.encode(), "xl/worksheets/sheet1.xml": sheet_xml.encode()}
    )


def _make_bridge(
    tmp_path: Path,
    payload: Mapping[str, Any],
    att_blobs: Mapping[str, bytes] | None = None,
    protected_ids: list[str] | None = None,
) -> tuple[Any, GmailService, DriveService, MagicMock, MagicMock]:
    """Wire GmailDriveBridgeService with mocked Gmail and Drive API clients.

    Args:
        tmp_path: Pytest temporary directory for isolated AlfredPaths.
        payload: Raw Gmail message payload dictionary returned by messages().get().
        att_blobs: Optional mapping of attachment_id -> raw bytes for attachments().get().
        protected_ids: Optional protected Drive IDs configured in SecurityConfig.

    Returns:
        Tuple of (GmailDriveBridgeService, GmailService, DriveService, gmail_mock, drive_mock).
    """
    from alfred.services.gmail_drive_bridge_service import GmailDriveBridgeService

    paths = AlfredPaths.from_env(tmp_path)
    cfg = AlfredSettings(security=SecurityConfig(protected_signature_drive_ids=protected_ids or []))
    store = StateStore(paths)
    gmail_client, drive_client = MagicMock(), MagicMock()
    users = gmail_client.users.return_value
    users.messages.return_value.get.return_value.execute.return_value = {
        "id": "m1",
        "payload": dict(payload),
    }
    blobs = dict(att_blobs or {})
    users.messages.return_value.attachments.return_value.get.side_effect = (
        lambda **kw: MagicMock(
            execute=lambda: {
                "data": _b64u(blobs.get(str(kw.get("id", "")), b"")),
                "size": len(blobs.get(str(kw.get("id", "")), b"")),
            }
        )
    )
    gmail = GmailService(paths, cfg, gmail_client=gmail_client)
    drive = DriveService(paths, cfg, store, drive_client=drive_client)
    bridge = GmailDriveBridgeService(cfg, store, gmail, drive)
    return bridge, gmail, drive, gmail_client, drive_client


def test_attachment_domain_models_defaults_and_validation() -> None:
    """Verify Pydantic schemas, defaults, and required-field checks for attachment models."""
    from alfred.domain.attachment_models import (
        DriveAttachmentTransferResult,
        GmailAttachmentContent,
        GmailAttachmentMeta,
    )

    meta = GmailAttachmentMeta(
        index=0, message_id="m1", filename="c.pdf", mime_type="application/pdf"
    )
    assert meta.attachment_id == "" and meta.size_bytes == 0 and meta.is_inline is False
    content = GmailAttachmentContent(
        index=1, message_id="m1", attachment_id="att-1", filename="note.txt",
        mime_type="text/plain", size_bytes=12, extraction_method="utf8_text", text="hello",
    )
    assert content.extraction_method == "utf8_text" and content.truncated is False
    transfer = DriveAttachmentTransferResult(
        message_id="m1", source_filename="c.pdf", drive_file_id="drv-1",
        drive_name="c.pdf", parent_id="dir-1", mime_type="application/pdf", size_bytes=512,
    )
    assert transfer.web_view_link == "" and transfer.path == ""
    assert transfer.deduplicated is False and transfer.updated is False
    with pytest.raises(pydantic.ValidationError):
        GmailAttachmentMeta(index=0, message_id="m1", filename="x.pdf")  # type: ignore[call-arg]


def test_attachment_extractor_constants_and_base64url() -> None:
    """Verify security archive limits and unpadded base64url decoding."""
    from alfred.services.attachment_extractor import (
        MAX_ARCHIVE_BYTES,
        MAX_SINGLE_XML_BYTES,
        MAX_TOTAL_XML_BYTES,
        MAX_ZIP_MEMBERS,
        decode_base64url_bytes,
    )

    assert MAX_ARCHIVE_BYTES == 25 * 1024 * 1024
    assert MAX_ZIP_MEMBERS == 250
    assert MAX_SINGLE_XML_BYTES == 10 * 1024 * 1024
    assert MAX_TOTAL_XML_BYTES == 25 * 1024 * 1024
    assert decode_base64url_bytes("") == b""
    assert decode_base64url_bytes(_b64u(b"Hello Alfred!")) == b"Hello Alfred!"
    assert decode_base64url_bytes("-_-_") == base64.urlsafe_b64decode("-_-_")


def test_sanitize_attachment_filename_security_and_fallbacks() -> None:
    """Verify path traversal, null-byte/control stripping, dot rejection, and fallback names."""
    from alfred.services.attachment_extractor import sanitize_attachment_filename

    assert sanitize_attachment_filename("../../token.json", index=1) == "token.json"
    assert sanitize_attachment_filename("sub/dir/devis.pdf", index=0) == "devis.pdf"
    cleaned = sanitize_attachment_filename("a\x00b\r\nc\x1f.pdf", index=0)
    assert "\x00" not in cleaned and not any(ord(ch) < 32 for ch in cleaned)
    assert cleaned.endswith(".pdf")
    for bad in ("", ".", "..", "   ", "/../../"):
        assert sanitize_attachment_filename(bad, index=2, mime_type="application/pdf") == (
            "attachment_2.pdf"
        )
    assert sanitize_attachment_filename("..", index=0, mime_type="text/plain") == "attachment_0.txt"


def test_collect_attachment_parts_nested_mime_and_inline_rules() -> None:
    """Verify recursive MIME traversal order, inline classification, and body filtering."""
    from alfred.services.attachment_extractor import collect_attachment_parts

    alt_parts = [
        {"mimeType": "text/plain", "body": {"data": _b64u(b"Plain")}},
        {"mimeType": "text/html", "body": {"data": _b64u(b"<p>Hi</p>")}},
    ]
    logo_part = {
        "mimeType": "image/png", "filename": "logo.png",
        "headers": [{"name": "Content-ID", "value": "<logo1@example.com>"}],
        "body": {"data": _b64u(b"\x89PNG"), "size": 4},
    }
    jpg_hdrs = [
        {"name": "Content-ID", "value": "<cid2@example.com>"},
        {"name": "Content-Disposition", "value": 'attachment; filename="photo.jpg"'},
    ]
    rel_parts = [
        {"mimeType": "multipart/alternative", "parts": alt_parts},
        logo_part,
    ]
    note_hdrs = [{"name": "Content-Disposition", "value": "inline"}]
    pdf_hdrs = [{"name": "Content-Disposition", "value": "attachment"}]
    payload: dict[str, Any] = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "multipart/related", "parts": rel_parts},
            {
                "mimeType": "image/jpeg", "filename": "photo.jpg",
                "headers": jpg_hdrs, "body": {"attachmentId": "att-jpg", "size": 200},
            },
            {
                "mimeType": "text/plain", "filename": "note.txt",
                "headers": note_hdrs, "body": {"data": _b64u(b"inline note"), "size": 11},
            },
            {
                "mimeType": "application/pdf", "filename": "../../bail.pdf",
                "headers": pdf_hdrs, "body": {"attachmentId": "att-pdf", "size": 1024},
            },
        ],
    }
    parts = collect_attachment_parts("msg-42", payload)
    assert [m.index for m, _ in parts] == [0, 1, 2, 3]
    assert [m.filename for m, _ in parts] == ["logo.png", "photo.jpg", "note.txt", "bail.pdf"]
    assert parts[0][0].is_inline is True and parts[0][1] == _b64u(b"\x89PNG")
    assert parts[1][0].is_inline is False and parts[1][0].attachment_id == "att-jpg"
    assert parts[2][0].is_inline is True and parts[2][1] == _b64u(b"inline note")
    assert parts[3][0].is_inline is False and parts[3][0].attachment_id == "att-pdf"


def test_extract_attachment_text_pdf_variants() -> None:
    """Verify extraction for native text PDF, blank scanned PDF, and corrupted PDF bytes."""
    from alfred.services.attachment_extractor import extract_attachment_text

    m, text, trunc = extract_attachment_text(
        _build_sample_pdf("Contrat Bail 2026"), "b.pdf", "application/pdf"
    )
    assert m == "pypdf_native_text" and "Contrat Bail 2026" in text and trunc is False
    empty_res = extract_attachment_text(_build_sample_pdf(""), "s.pdf", "application/pdf")
    assert empty_res == ("empty_or_scanned_pdf", "", False)
    bad_res = extract_attachment_text(b"%PDF-bad", "bad.pdf", "application/pdf")
    assert bad_res == ("corrupted_pdf", "", False)


def test_extract_attachment_text_docx_xlsx_and_zip_guards() -> None:
    """Verify DOCX/XLSX text extraction and anti-XXE / anti-zip-bomb archive guards."""
    from alfred.services.attachment_extractor import (
        MAX_SINGLE_XML_BYTES,
        MAX_ZIP_MEMBERS,
        extract_attachment_text,
    )

    docx = _build_docx_bytes(["Article 1", "Montant 1500 EUR"])
    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    method, text, trunc = extract_attachment_text(docx, "accord.docx", docx_mime)
    assert method == "docx_xml" and trunc is False
    assert "Article 1" in text and "Montant 1500 EUR" in text

    xxe = _build_docx_bytes(["Hack"], extra_xml='<!DOCTYPE foo [<!ENTITY xxe "leak">]>')
    assert extract_attachment_text(xxe, "xxe.docx", "")[0] == "corrupted_or_unsafe_archive"

    many = {f"word/extra_{i}.xml": b"<x/>" for i in range(MAX_ZIP_MEMBERS + 1)}
    many["word/document.xml"] = b"<w:document><w:body/></w:document>"
    assert extract_attachment_text(_build_zip_bytes(many), "many.docx", "")[0] == (
        "corrupted_or_unsafe_archive"
    )
    huge_xml = (
        b"<w:document><w:body><w:t>"
        + b"A" * (MAX_SINGLE_XML_BYTES + 64)
        + b"</w:t></w:body></w:document>"
    )
    assert extract_attachment_text(_build_zip_bytes({"word/document.xml": huge_xml}), "b.docx", "")[
        0
    ] == "corrupted_or_unsafe_archive"
    assert extract_attachment_text(b"not-a-zip", "c.docx", "")[0] == "corrupted_or_unsafe_archive"

    xlsx = _build_xlsx_bytes(
        ["Loyer", "Caution"], [[("s", "0"), ("n", "1200")], [("s", "1"), ("", "500")]]
    )
    xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    xm, xt, xtr = extract_attachment_text(xlsx, "budget.xlsx", xlsx_mime)
    assert xm == "xlsx_xml" and xtr is False
    assert "Loyer" in xt and "1200" in xt and "Caution" in xt


def test_extract_attachment_text_html_structured_binary_and_truncation() -> None:
    """Verify HTML stripping, structured UTF-8 formats, binary fallback, and max_chars limit."""
    from alfred.services.attachment_extractor import extract_attachment_text

    raw_html = (
        b"<html><script>bad()</script><style>p{}</style>"
        b"<body><p>Re&ccedil;u &amp; OK</p></body></html>"
    )
    hm, ht, htr = extract_attachment_text(raw_html, "avis.html", "text/html")
    assert hm == "html_plain_text" and htr is False
    assert "Reçu & OK" in ht and "bad()" not in ht

    for fname, mime, raw in (
        ("data.csv", "text/csv", b"col1,col2\n10,20"),
        ("meta.json", "application/json", b'{"status": "ok"}'),
        ("rdv.ics", "text/calendar", b"BEGIN:VCALENDAR\nSUMMARY:RDV\nEND:VCALENDAR"),
        ("note.md", "text/markdown", b"# Titre\nTexte"),
    ):
        m, t, tr = extract_attachment_text(raw, fname, mime)
        assert m == "utf8_text" and tr is False and t

    mtr, ttr, truncated = extract_attachment_text(
        b"0123456789ABCDEF", "long.txt", "text/plain", max_chars=8
    )
    assert mtr == "utf8_text" and truncated is True and len(ttr) <= 8
    assert extract_attachment_text(b"\x89PNG\r\n", "i.png", "image/png") == (
        "binary_not_extractable", "", False
    )


def test_bridge_read_selection_raw_clients_and_protected_drive_guard(tmp_path: Path) -> None:
    """Verify selector priority, get_raw_client(), ambiguity ValueError, and Drive vault guard.

    Args:
        tmp_path: Pytest temporary directory for isolated state and settings.
    """
    inline = {
        "mimeType": "image/png", "filename": "logo.png",
        "headers": [{"name": "Content-ID", "value": "<logo>"}],
        "body": {"data": _b64u(b"\x89PNG"), "size": 4},
    }
    att = {
        "mimeType": "text/plain", "filename": "Contrat_2026_Final.txt",
        "headers": [{"name": "Content-Disposition", "value": "attachment"}],
        "body": {"attachmentId": "att-txt", "size": 19},
    }
    payload = {"mimeType": "multipart/mixed", "parts": [inline, att]}
    bridge, gmail, drive, gm_cli, drv_cli = _make_bridge(
        tmp_path, payload, att_blobs={"att-txt": b"Texte contrat final"}, protected_ids=["vault-99"]
    )
    assert gmail.get_raw_client() is gm_cli and drive.get_raw_client() is drv_cli
    assert bridge.read_message_attachment("m1").filename == "Contrat_2026_Final.txt"
    assert bridge.read_message_attachment("m1", attachment_id="att-txt").text == (
        "Texte contrat final"
    )
    assert bridge.read_message_attachment("m1", index=0).extraction_method == (
        "binary_not_extractable"
    )
    assert bridge.read_message_attachment("m1", filename="contrat_2026_final.txt").index == 1
    assert bridge.read_message_attachment("m1", filename="2026_Final").index == 1
    with pytest.raises(ValueError):
        bridge.read_message_attachment("m1", filename="inexistant.pdf")
    with pytest.raises(PermissionError):
        bridge.transfer_attachment_to_drive("m1", parent_id="vault-99")

    multi = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "filename": "report_a.txt", "body": {"data": _b64u(b"A")}},
            {"mimeType": "text/plain", "filename": "report_b.txt", "body": {"data": _b64u(b"B")}},
        ],
    }
    ambiguous, _, _, _, _ = _make_bridge(tmp_path / "m", multi)
    with pytest.raises(ValueError):
        ambiguous.read_message_attachment("m1")
    with pytest.raises(ValueError):
        ambiguous.read_message_attachment("m1", filename="report_")


def test_delegation_resolve_spawn_timeout_brain_vs_muscle(tmp_path: Path) -> None:
    """Verify resolve_spawn_timeout assigns 600s to orchestrators and 180s to muscle projects.

    Args:
        tmp_path: Pytest temporary directory for isolated AlfredPaths.
    """
    assert DelegationService.BRAIN_COMMAND_TIMEOUT_SEC == 600
    assert DelegationService.DEFAULT_COMMAND_TIMEOUT_SEC == 180
    dlg = DelegationService(paths=AlfredPaths.from_env(tmp_path), settings=AlfredSettings())
    for brain in ("alfred-evolve", "alfred-life", "alfred-onboard", "evolve", "life", "onboard"):
        assert dlg.resolve_spawn_timeout(brain) == 600
    for muscle in ("alfred-inbox", "alfred-calendar", "alfred-tasks", "inbox", "calendar", "tasks"):
        assert dlg.resolve_spawn_timeout(muscle) == 180


def test_daemon_sync_is_stale_running_conversation(tmp_path: Path) -> None:
    """Verify _is_stale_running_conversation requires existing old artifacts and respects grace.

    Args:
        tmp_path: Pytest temporary directory for simulated Antigravity artifacts.
    """
    agy_dir = tmp_path / "agy"
    (agy_dir / "conversations").mkdir(parents=True)
    svc = AgyDaemonSyncService(
        state_dir=tmp_path / "state", agy_data_dir=agy_dir, stale_running_grace_sec=300
    )
    cid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert svc._is_stale_running_conversation(cid) is False
    db_file = agy_dir / "conversations" / f"{cid}.db"
    db_file.write_bytes(b"db")
    assert svc._is_stale_running_conversation(cid) is False
    old_ns = int((time.time() - 400) * 1_000_000_000)
    os.utime(db_file, ns=(old_ns, old_ns))
    assert svc._is_stale_running_conversation(cid) is True
    brain_dir = agy_dir / "brain" / cid
    brain_dir.mkdir(parents=True)
    assert svc._is_stale_running_conversation(cid) is False
    os.utime(brain_dir, ns=(old_ns, old_ns))
    assert svc._is_stale_running_conversation(cid) is True
    no_grace = AgyDaemonSyncService(tmp_path / "s2", agy_dir, stale_running_grace_sec=0)
    assert no_grace._is_stale_running_conversation(cid) is False
