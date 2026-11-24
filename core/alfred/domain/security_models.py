"""Domain models for signature human consent, email sanitization, and specialist delegation."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def _utc_now_iso() -> str:
    """Return the current UTC timestamp formatted as an ISO-8601 string."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class SignatureTicketStatus(StrEnum):
    """Lifecycle states of a SHA-256-pinned handwritten signature request."""

    PENDING_CONSENT = "PENDING_CONSENT"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"


class SignaturePlacementRect(BaseModel):
    """PDF page index and bounding box (in points) for handwritten signature placement."""

    model_config = ConfigDict(frozen=True)

    page_index: int = Field(default=0, ge=0)
    x: float = 360.0
    y: float = 72.0
    width: float = Field(default=160.0, gt=10.0)
    height: float = Field(default=56.0, gt=10.0)


class SignatureConsentTicket(BaseModel):
    """Single-use signature authorization ticket pinned to an exact unsigned PDF SHA-256."""

    request_id: str
    source_pdf_path: str
    preview_pdf_path: str
    signed_output_path: str
    document_sha256: str
    reason: str
    workspace: str = "alfred-life"
    rect: SignaturePlacementRect = Field(default_factory=SignaturePlacementRect)
    status: SignatureTicketStatus = SignatureTicketStatus.PENDING_CONSENT
    created_at: str = Field(default_factory=_utc_now_iso)
    consumed_at: str | None = None


class SignatureRequestsState(BaseModel):
    """Persistent registry of pending and consumed signature consent tickets."""

    requests: dict[str, SignatureConsentTicket] = Field(default_factory=dict)


class SignatureAuditEvent(BaseModel):
    """Append-only JSONL audit event recorded for every prepare/apply/deny signature action."""

    timestamp: str = Field(default_factory=_utc_now_iso)
    request_id: str
    document_sha256: str
    action: str
    workspace: str
    details: str = ""


class SanitizedEmailResult(BaseModel):
    """Output of deterministic HTML/Unicode sanitization and prompt-injection detection."""

    model_config = ConfigDict(frozen=True)

    clean_text: str
    suspicious_injection: bool = False
    matched_signals: list[str] = Field(default_factory=list)


class DelegationTarget(StrEnum):
    """Canonical specialist targets addressable via Cross-Project DelegationService."""

    CALENDAR = "calendar"
    TASKS = "tasks"
    INBOX = "inbox"
    DRIVE = "drive"
    EVOLVE = "evolve"
    LIFE = "life"
    ONBOARD = "onboard"

    def __eq__(self, other: object) -> bool:
        """Compare DelegationTarget equal to matching string values under strict typing.

        Args:
            other: Candidate string or enum member to compare.

        Returns:
            True when string values match.
        """
        if isinstance(other, str):
            return str.__eq__(self, other)
        return NotImplemented

    def __hash__(self) -> int:
        """Return string hash consistent with __eq__."""
        return str.__hash__(self)


class DelegationMode(StrEnum):
    """Single-Gate execution mode vs dedicated Blocking Gate opening."""

    EXECUTE_APPROVED = "execute-approved"
    OPEN_GATE = "open-gate"


class DelegationRecord(BaseModel):
    """Structured result of delegating work into a specialist Antigravity project."""

    target_role: str
    target_project: str
    mode: DelegationMode
    status: str
    session_id: str = ""
    title: str = ""
    model_used: str = ""
    parent_session_id: str = ""
    details: str = ""
