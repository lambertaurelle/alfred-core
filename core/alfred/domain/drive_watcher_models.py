"""Domain models and configuration schemas for Google Drive governance and 2-speed watcher."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_EXCLUDED_DRIVE_PATH_PREFIXES: tuple[str, ...] = (
    "/Backups",
    "/Photos",
    "/Archives_Froides",
    "/node_modules",
    "/.git",
)

DEFAULT_EXCLUDED_DRIVE_MIME_PREFIXES: tuple[str, ...] = (
    "image/",
    "video/",
    "audio/",
    "application/zip",
    "application/x-tar",
    "application/gzip",
    "application/x-7z-compressed",
)


def _utc_now_iso() -> str:
    """Return the current UTC timestamp formatted as an ISO-8601 string."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class DriveChangeKind(StrEnum):
    """Classification of a Google Drive delta item relative to the local index."""

    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    MOVED = "MOVED"
    TRASHED = "TRASHED"


class DriveWatcherOutputType(StrEnum):
    """Exclusive outputs for the Speed-2 Google Drive watcher cognitive classifier."""

    SILENT_NO_ACTION = "SILENT_NO_ACTION"
    SILENT_AUTO_FILED = "SILENT_AUTO_FILED"
    GATE_REQUIRED = "GATE_REQUIRED"


class DriveGovernanceActionType(StrEnum):
    """Non-destructive governance mutations permitted inside workspace alfred-drive."""

    MOVE = "MOVE"
    RENAME = "RENAME"
    CREATE_FOLDER = "CREATE_FOLDER"
    CREATE_SHORTCUT = "CREATE_SHORTCUT"
    UPSERT_KNOWLEDGE = "UPSERT_KNOWLEDGE"


class DriveWatcherConfig(BaseModel):
    """Deterministic pre-filtering and batching configuration for alfred-drive watcher."""

    model_config = ConfigDict(frozen=True)

    coalesce_max_files_per_batch: int = Field(default=25, ge=1, le=200)
    excluded_path_prefixes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_DRIVE_PATH_PREFIXES)
    )
    excluded_mime_prefixes: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_DRIVE_MIME_PREFIXES)
    )
    max_extracted_chars_per_file: int = Field(default=12000, ge=500, le=100000)

    @classmethod
    def from_dict(cls, raw: object) -> DriveWatcherConfig:
        """Parse DriveWatcherConfig from settings YAML under 'drive_watcher'.

        Args:
            raw: Optional dictionary extracted from YAML configuration.

        Returns:
            Validated DriveWatcherConfig value object.
        """
        if not isinstance(raw, dict):
            return cls()
        raw_paths = raw.get("excluded_path_prefixes")
        raw_mimes = raw.get("excluded_mime_prefixes")
        paths = (
            [str(p).strip() for p in raw_paths if str(p).strip()]
            if isinstance(raw_paths, list)
            else list(DEFAULT_EXCLUDED_DRIVE_PATH_PREFIXES)
        )
        mimes = (
            [str(m).strip() for m in raw_mimes if str(m).strip()]
            if isinstance(raw_mimes, list)
            else list(DEFAULT_EXCLUDED_DRIVE_MIME_PREFIXES)
        )
        return cls(
            coalesce_max_files_per_batch=int(raw.get("coalesce_max_files_per_batch", 25)),
            excluded_path_prefixes=paths,
            excluded_mime_prefixes=mimes,
            max_extracted_chars_per_file=int(raw.get("max_extracted_chars_per_file", 12000)),
        )


class DriveDeltaItem(BaseModel):
    """Normalized Speed-1 eligible Google Drive file change item."""

    model_config = ConfigDict(frozen=True)

    file_id: str
    name: str
    mime_type: str
    parents: list[str] = Field(default_factory=list)
    path: str = ""
    modified_time: str = ""
    md5_checksum: str = ""
    head_revision_id: str = ""
    owned_by_me: bool = True
    shared: bool = False
    change_kind: DriveChangeKind = DriveChangeKind.MODIFIED

    @property
    def version_fingerprint(self) -> str:
        """Return deterministic revision+checksum+mtime fingerprint for deduplication."""
        return f"{self.head_revision_id}:{self.md5_checksum}:{self.modified_time}"


class DriveDeltaReport(BaseModel):
    """Speed-1 zero-token delta scan summary with eligible items and skip reasons."""

    start_page_token_before: str = ""
    new_start_page_token: str = ""
    total_raw_changes: int = 0
    eligible_items: list[DriveDeltaItem] = Field(default_factory=list)
    skipped_reasons: dict[str, str] = Field(default_factory=dict)


class ProcessedDriveEvent(BaseModel):
    """Deduplication and anti-loop record for a processed or self-mutated Drive file."""

    file_id: str
    version_fingerprint: str
    recent_fingerprints: list[str] = Field(default_factory=list)
    parent_ids: list[str] = Field(default_factory=list)
    origin: str = "watcher"
    outcome: str = ""
    processed_at: str = Field(default_factory=_utc_now_iso)


class DriveWatcherLedger(BaseModel):
    """Persistent registry of processed Drive file fingerprints preventing infinite loops."""

    events: dict[str, ProcessedDriveEvent] = Field(default_factory=dict)

    def has_processed(self, file_id: str, version_fingerprint: str) -> bool:
        """Check whether file_id at version_fingerprint has already been processed.

        Args:
            file_id: Google Drive file identifier.
            version_fingerprint: Revision, MD5, and modifiedTime composite key.

        Returns:
            True if this exact file version was already processed or self-mutated.
        """
        existing = self.events.get(file_id)
        if existing is None:
            return False
        if existing.version_fingerprint == version_fingerprint:
            return True
        return version_fingerprint in existing.recent_fingerprints


class SanitizedDriveTextResult(BaseModel):
    """Sanitized Drive file name, path, and text with prompt-injection telemetry."""

    model_config = ConfigDict(frozen=True)

    clean_name: str
    clean_path: str
    clean_text: str
    suspicious_injection: bool = False
    matched_signals: list[str] = Field(default_factory=list)


class DriveGovernanceOp(BaseModel):
    """Single proposed non-destructive Drive filing or Tier-2 knowledge synthesis action."""

    model_config = ConfigDict(frozen=True)

    action: DriveGovernanceActionType
    file_id: str = ""
    target_parent_id: str = ""
    new_name: str = ""
    shortcut_target_id: str = ""
    knowledge_file: str = ""
    knowledge_id: str = ""
    knowledge_title: str = ""
    knowledge_domain: str = ""
    knowledge_summary: str = ""
    knowledge_keywords: list[str] = Field(default_factory=list)
    knowledge_body: str = ""


class DriveMutationResult(BaseModel):
    """Result descriptor of an executed non-destructive Google Drive governance mutation."""

    model_config = ConfigDict(frozen=True)

    action: DriveGovernanceActionType
    file_id: str
    name: str
    mime_type: str
    parent_id: str = ""
    path: str = ""
    version_fingerprint: str = ""
    shortcut_target_id: str = ""


class DriveWatcherBatchDecision(BaseModel):
    """Speed-2 cognitive and security outcome for one coalesced batch of Drive changes."""

    batch_id: str
    file_ids: list[str] = Field(default_factory=list)
    output_type: DriveWatcherOutputType
    reason: str
    applied_mutations: list[DriveMutationResult] = Field(default_factory=list)
    updated_knowledge_files: list[str] = Field(default_factory=list)
    session_id: str | None = None
    session_archived: bool = False
    implementation_plan_markdown: str | None = None
    suspicious_injection: bool = False
    validation_errors: list[str] = Field(default_factory=list)
