"""Handwritten signature vault with SHA-256 pinning, one-shot tickets, and human consent."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import shutil
import struct
from pathlib import Path

from pypdf import PdfReader, PdfWriter, Transformation

from alfred.domain.models import AlfredPaths, AlfredSettings, utc_now_iso
from alfred.domain.security_models import (
    SignatureAuditEvent,
    SignatureConsentTicket,
    SignaturePlacementRect,
    SignatureTicketStatus,
)
from alfred.repositories.state_store import StateStore

FORBIDDEN_SIGNATURE_WORKSPACES: frozenset[str] = frozenset(
    {"inbox", "alfred-inbox", "mcp", "alfred-mcp"}
)


class SignatureFeatureDisabledError(RuntimeError):
    """Raised when signature_delegation is disabled in settings.features (Graceful Skip)."""


class SignatureVaultService:
    """Isolated handwritten signature vault enforcing explicit human consent and SHA-256 pin."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
    ) -> None:
        """Initialize the SignatureVaultService.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings including features.signature_delegation.
            state_store: Atomic local state store holding tickets and audit JSONL.
        """
        self._paths = paths
        self._settings = settings
        self._state_store = state_store

    def is_enabled(self) -> bool:
        """Return True when signature_delegation is explicitly enabled by the user."""
        return bool(self._settings.features.signature_delegation)

    def prepare_signature(
        self,
        source_pdf: Path,
        rect: SignaturePlacementRect | None = None,
        reason: str = "Document signature approved by user",
        workspace: str = "alfred-life",
        preview_out: Path | None = None,
        signed_out: Path | None = None,
    ) -> SignatureConsentTicket:
        """Validate unsigned PDF, create preview without stamp, and issue a SHA-256 ticket.

        Args:
            source_pdf: Path to the unsigned PDF document on disk.
            rect: Optional page index and bounding box for signature placement.
            reason: Explicit human-readable purpose shown in the Blocking Gate.
            workspace: Originating workspace (blocked in alfred-inbox and mcp).
            preview_out: Optional destination path for preview PDF copy.
            signed_out: Optional destination path for final signed PDF.

        Returns:
            Persisted SignatureConsentTicket in PENDING_CONSENT state.
        """
        self._assert_enabled_and_allowed_workspace(workspace, action_label="PREPARE")
        resolved_src = source_pdf.resolve()
        if not resolved_src.exists():
            raise FileNotFoundError(f"Unsigned source PDF not found: {resolved_src}")
        raw_bytes = resolved_src.read_bytes()
        doc_sha = hashlib.sha256(raw_bytes).hexdigest()
        reader = PdfReader(io.BytesIO(raw_bytes))
        placement = rect or SignaturePlacementRect()
        if placement.page_index >= len(reader.pages):
            page_cnt = len(reader.pages)
            raise ValueError(
                f"Signature page_index {placement.page_index} out of range ({page_cnt} pages)."
            )

        req_id = f"sig-{secrets.token_hex(8)}"
        previews_dir = self._paths.cache_dir / "signature_previews"
        previews_dir.mkdir(parents=True, exist_ok=True)
        eff_preview = (preview_out or (previews_dir / f"{req_id}_preview.pdf")).resolve()
        eff_signed = (
            signed_out or resolved_src.with_name(f"{resolved_src.stem}_signed.pdf")
        ).resolve()
        eff_preview.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved_src, eff_preview)

        ticket = SignatureConsentTicket(
            request_id=req_id,
            source_pdf_path=str(resolved_src),
            preview_pdf_path=str(eff_preview),
            signed_output_path=str(eff_signed),
            document_sha256=doc_sha,
            reason=reason.strip() or "Signature document",
            workspace=workspace,
            rect=placement,
            status=SignatureTicketStatus.PENDING_CONSENT,
        )
        self._state_store.signature_requests.update(
            lambda cur: cur.model_copy(update={"requests": {**cur.requests, req_id: ticket}})
        )
        self._log_audit(req_id, doc_sha, "PREPARE", workspace, f"Preview: {eff_preview}")
        return ticket

    def apply_signature(
        self,
        request_id: str,
        workspace: str = "alfred-life",
        transcript_path: Path | None = None,
    ) -> Path:
        """Verify explicit user approval + bit-exact SHA-256, stamp PDF in RAM, and burn ticket.

        Args:
            request_id: Single-use ticket identifier returned by prepare_signature.
            workspace: Executing workspace (strictly forbidden in alfred-inbox and mcp).
            transcript_path: Optional Antigravity transcript.jsonl path for USER_EXPLICIT check.

        Returns:
            Resolved Path to the generated signed PDF document.
        """
        self._assert_enabled_and_allowed_workspace(
            workspace, action_label="APPLY", req_id=request_id
        )
        ticket = self._validate_ticket_and_consent(request_id, workspace, transcript_path)
        src_path = Path(ticket.source_pdf_path)
        if not src_path.exists():
            raise FileNotFoundError(f"Source PDF missing at apply time: {src_path}")
        current_bytes = src_path.read_bytes()
        current_sha = hashlib.sha256(current_bytes).hexdigest()
        if not hmac.compare_digest(current_sha, ticket.document_sha256):
            self._log_audit(
                request_id,
                current_sha,
                "DENY_HASH_MISMATCH",
                workspace,
                f"Expected {ticket.document_sha256}",
            )
            raise PermissionError("Unsigned PDF SHA-256 changed after prepare-signature; aborting.")

        stamp_bytes = self._load_vault_stamp_pdf_bytes()
        out_path = Path(ticket.signed_output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self._merge_stamp_in_memory(current_bytes, stamp_bytes, ticket.rect, out_path)

        consumed = ticket.model_copy(
            update={"status": SignatureTicketStatus.CONSUMED, "consumed_at": utc_now_iso()}
        )
        self._state_store.signature_requests.update(
            lambda cur: cur.model_copy(update={"requests": {**cur.requests, request_id: consumed}})
        )
        self._log_audit(request_id, current_sha, "APPLY", workspace, f"Signed: {out_path}")
        return out_path

    def _validate_ticket_and_consent(
        self,
        request_id: str,
        workspace: str,
        transcript_path: Path | None,
    ) -> SignatureConsentTicket:
        """Load pending signature ticket and enforce fresh USER_EXPLICIT consent turn."""
        state = self._state_store.signature_requests.load()
        ticket = state.requests.get(request_id)
        if ticket is None:
            self._log_audit(request_id, "", "DENY_UNKNOWN_TICKET", workspace, "Ticket not found")
            raise PermissionError(f"Unknown signature request_id: '{request_id}'")
        if ticket.status != SignatureTicketStatus.PENDING_CONSENT:
            self._log_audit(
                request_id,
                ticket.document_sha256,
                "DENY_REPLAY",
                workspace,
                f"Status={ticket.status}",
            )
            raise PermissionError(f"Signature ticket '{request_id}' is already {ticket.status}.")
        eff_transcript = transcript_path or self._transcript_from_env()
        if eff_transcript is not None and not self._has_explicit_user_consent(
            eff_transcript, ticket.created_at
        ):
            self._log_audit(
                request_id,
                ticket.document_sha256,
                "DENY_NO_USER_TURN",
                workspace,
                str(eff_transcript),
            )
            raise PermissionError(
                "Explicit USER_EXPLICIT consent turn after ticket creation not found in transcript."
            )
        return ticket

    def _assert_enabled_and_allowed_workspace(
        self, workspace: str, action_label: str, req_id: str = ""
    ) -> None:
        """Enforce feature gate and workspace isolation before touching signature state."""
        norm_ws = workspace.strip().lower()
        if norm_ws in FORBIDDEN_SIGNATURE_WORKSPACES:
            self._log_audit(req_id, "", f"DENY_WORKSPACE_{action_label}", workspace, "Forbidden")
            raise PermissionError(
                f"Handwritten signature is strictly forbidden inside workspace '{workspace}'."
            )
        if not self.is_enabled():
            raise SignatureFeatureDisabledError(
                "SKIPPED_DISABLED: features.signature_delegation is disabled in settings.yaml."
            )

    def _load_vault_stamp_pdf_bytes(self) -> bytes:
        """Read signature.pdf / signature_stamp.pdf / signature.png from signature_vault/."""
        for pdf_name in ("signature.pdf", "signature_stamp.pdf"):
            pdf_candidate = self._paths.signature_vault_dir / pdf_name
            if pdf_candidate.is_file():
                return pdf_candidate.read_bytes()
        for png_name in ("signature.png", "signature_stamp.png"):
            png_candidate = self._paths.signature_vault_dir / png_name
            if png_candidate.is_file():
                return self._png_to_single_page_pdf_bytes(png_candidate.read_bytes())
        raise FileNotFoundError(
            f"Missing signature.pdf (or signature_stamp.pdf / signature.png) in "
            f"{self._paths.signature_vault_dir}; place your signature stamp in the vault."
        )

    def _png_to_single_page_pdf_bytes(self, png_bytes: bytes) -> bytes:
        """Convert PNG stamp dimensions into a valid 1-page PDF byte stream."""
        width, height = 180, 60
        if len(png_bytes) >= 24 and png_bytes[:8] == b"\x89PNG\r\n\x1a\n":
            w_px, h_px = struct.unpack(">II", png_bytes[16:24])
            if w_px > 0 and h_px > 0:
                width, height = int(w_px), int(h_px)
        writer = PdfWriter()
        writer.add_blank_page(width=width, height=height)
        buf = io.BytesIO()
        writer.write(buf)
        return buf.getvalue()

    def _merge_stamp_in_memory(
        self,
        doc_bytes: bytes,
        stamp_bytes: bytes,
        rect: SignaturePlacementRect,
        out_path: Path,
    ) -> None:
        """Overlay the 1-page stamp PDF onto doc_bytes at rect in RAM and write out_path."""
        doc_reader = PdfReader(io.BytesIO(doc_bytes))
        stamp_page = PdfReader(io.BytesIO(stamp_bytes)).pages[0]
        stamp_w = float(stamp_page.mediabox.width) or 1.0
        stamp_h = float(stamp_page.mediabox.height) or 1.0
        tx = Transformation().scale(rect.width / stamp_w, rect.height / stamp_h).translate(
            rect.x, rect.y
        )
        writer = PdfWriter()
        for idx, page in enumerate(doc_reader.pages):
            added_page = writer.add_page(page)
            if idx == rect.page_index:
                added_page.merge_transformed_page(stamp_page, tx)
        with out_path.open("wb") as handle:
            writer.write(handle)

    def _transcript_from_env(self) -> Path | None:
        """Resolve optional ALFRED_TRANSCRIPT_PATH environment variable when set."""
        raw = os.environ.get("ALFRED_TRANSCRIPT_PATH", "").strip()
        return Path(raw).resolve() if raw else None

    def _has_explicit_user_consent(self, transcript_path: Path, ticket_created_at: str) -> bool:
        """Verify transcript.jsonl has a USER_EXPLICIT / USER_INPUT step since ticket creation."""
        if not transcript_path.exists():
            return False
        for line in transcript_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                step = json.loads(line)
            except ValueError:
                continue
            src = str(step.get("source", ""))
            stype = str(step.get("type", ""))
            created = str(step.get("created_at", ""))
            if (src == "USER_EXPLICIT" or stype == "USER_INPUT") and (
                not created or created >= ticket_created_at
            ):
                return True
        return False

    def _log_audit(
        self, req_id: str, sha256: str, action: str, workspace: str, details: str
    ) -> None:
        """Append an immutable SignatureAuditEvent to signature_audit.jsonl."""
        self._state_store.signature_audit.append(
            SignatureAuditEvent(
                request_id=req_id,
                document_sha256=sha256,
                action=action,
                workspace=workspace,
                details=details,
            )
        )
