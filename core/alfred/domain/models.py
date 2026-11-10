"""Pure domain models and configuration schemas for Alfred V7.1 Lean."""

from __future__ import annotations

import os
import unicodedata
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_OAUTH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
)


def fold_accents(text: str) -> str:
    """Normalize text to lowercase ASCII-folded form by stripping Unicode diacritics.

    Args:
        text: Input string potentially containing French accented characters.

    Returns:
        Lowercase diacritic-stripped string for accent-insensitive matching.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def utc_now_iso() -> str:
    """Return the current UTC timestamp formatted as an ISO-8601 string.

    Returns:
        UTC ISO-8601 timestamp string ending with 'Z'.
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class AlfredPaths(BaseModel):
    """Resolved filesystem layout anchored on ALFRED_ROOT_DIR without hardcoded paths."""

    model_config = ConfigDict(frozen=True)

    root_dir: Path

    @classmethod
    def from_env(cls, override_root: Path | str | None = None) -> AlfredPaths:
        """Build resolved paths from override or ALFRED_ROOT_DIR (default /root/Alfred).

        Args:
            override_root: Optional explicit root directory path.

        Returns:
            Configured AlfredPaths value object.
        """
        if override_root is not None:
            resolved = Path(override_root).resolve()
        else:
            resolved = Path(os.environ.get("ALFRED_ROOT_DIR", "/root/Alfred")).resolve()
        return cls(root_dir=resolved)

    @property
    def core_dir(self) -> Path:
        """Return core directory path."""
        return self.root_dir / "core"

    @property
    def templates_knowledge_dir(self) -> Path:
        """Return reference knowledge templates directory path."""
        return self.core_dir / "templates" / "knowledge"

    @property
    def instance_dir(self) -> Path:
        """Return private instance overlay directory path."""
        return self.root_dir / "instance"

    @property
    def settings_path(self) -> Path:
        """Return instance settings YAML path."""
        return self.instance_dir / "config" / "settings.yaml"

    @property
    def example_settings_path(self) -> Path:
        """Return fallback example settings YAML path."""
        return self.core_dir / "config" / "settings.example.yaml"

    @property
    def secrets_dir(self) -> Path:
        """Return OAuth2 secrets directory path."""
        return self.instance_dir / "secrets"

    @property
    def knowledge_dir(self) -> Path:
        """Return local 3-tier YAML knowledge directory path."""
        return self.instance_dir / "data" / "knowledge"

    @property
    def cache_dir(self) -> Path:
        """Return local metadata and extraction cache directory path."""
        return self.instance_dir / "data" / "cache"

    @property
    def drive_index_path(self) -> Path:
        """Return Google Drive metadata index JSON path."""
        return self.cache_dir / "drive_metadata_index.json"

    @property
    def drive_read_cache_path(self) -> Path:
        """Return Google Drive content cache JSON path."""
        return self.cache_dir / "drive_read_cache.json"

    @property
    def state_dir(self) -> Path:
        """Return atomic JSON/JSONL state directory path."""
        return self.instance_dir / "data" / "state"

    @property
    def worktrees_dir(self) -> Path:
        """Return isolated git worktrees directory path."""
        return self.root_dir / "worktrees"


class AlfredSettings(BaseModel):
    """Structural runtime settings loaded from YAML without personal hardcoded values."""

    project_name: str = "Alfred Personal Chief of Staff"
    timezone: str = "Europe/Paris"
    language: str = "fr-FR"
    referentiel_drive_folder_name: str = "_ALFRED_REFERENTIEL_"
    nounou_calendar_name: str = "Garde Nounou"
    primary_calendar_id: str = "primary"
    oauth_scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_OAUTH_SCOPES))
    agy_binary: str = "agy"
    max_knowledge_body_lines: int = 150
    core_base_branch: str = "main"
    instance_base_branch: str = "main"

    @classmethod
    def load(cls, paths: AlfredPaths) -> AlfredSettings:
        """Load structural settings from instance/config/settings.yaml or fallback defaults.

        Args:
            paths: Resolved AlfredPaths layout.

        Returns:
            Validated AlfredSettings instance.
        """
        candidate = (
            paths.settings_path if paths.settings_path.exists() else paths.example_settings_path
        )
        if not candidate.exists():
            return cls()
        raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        google_cfg = raw.get("google", {})
        agy_cfg = raw.get("antigravity", {})
        know_cfg = raw.get("knowledge", {})
        evolve_cfg = raw.get("evolve", {})
        raw_scopes = google_cfg.get("oauth_scopes") or list(DEFAULT_OAUTH_SCOPES)
        return cls(
            project_name=str(raw.get("project_name", "Alfred Personal Chief of Staff")),
            timezone=str(raw.get("timezone", "Europe/Paris")),
            language=str(raw.get("language", "fr-FR")),
            referentiel_drive_folder_name=str(
                google_cfg.get("referentiel_drive_folder_name", "_ALFRED_REFERENTIEL_")
            ),
            nounou_calendar_name=str(google_cfg.get("nounou_calendar_name", "Garde Nounou")),
            primary_calendar_id=str(google_cfg.get("primary_calendar_id", "primary")),
            oauth_scopes=[str(s) for s in raw_scopes],
            agy_binary=str(agy_cfg.get("binary_path", "agy")),
            max_knowledge_body_lines=int(know_cfg.get("max_body_lines", 150)),
            core_base_branch=str(evolve_cfg.get("core_base_branch", "main")),
            instance_base_branch=str(evolve_cfg.get("instance_base_branch", "main")),
        )


class FrontmatterSchema(BaseModel):
    """Mandatory Tier-1 YAML frontmatter header for up-to-date reference .md files."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    keywords: list[str] = Field(default_factory=list)
    drive_source_ids: list[str] = Field(default_factory=list)
    updated_at: str = Field(default_factory=utc_now_iso)


class KnowledgeDocument(BaseModel):
    """Tier-2 loaded reference document combining YAML frontmatter and concise Markdown body."""

    frontmatter: FrontmatterSchema
    body: str
    file_name: str
    body_line_count: int = 0


class DriveEntry(BaseModel):
    """Metadata record for a single Google Drive folder or file in the local index."""

    file_id: str
    name: str
    mime_type: str
    parents: list[str] = Field(default_factory=list)
    modified_time: str = ""
    path: str = ""
    is_folder: bool = False
    trashed: bool = False


class DriveMetadataIndex(BaseModel):
    """Local 0-token metadata index of the Google Drive tree."""

    start_page_token: str = ""
    last_synced_at: str = ""
    folder_count: int = 0
    file_count: int = 0
    top_level_folders: list[str] = Field(default_factory=list)
    entries: dict[str, DriveEntry] = Field(default_factory=dict)


class DriveCachedContent(BaseModel):
    """Cached native text extraction keyed by (file_id, modified_time)."""

    file_id: str
    modified_time: str
    extraction_method: str
    text: str


class DriveReadCache(BaseModel):
    """Persistent cache mapping Drive file IDs to extracted text and modifiedTime."""

    items: dict[str, DriveCachedContent] = Field(default_factory=dict)


class GoogleSyncBookmarks(BaseModel):
    """Incremental synchronization tokens and Google resource identifiers."""

    gmail_history_id: str | None = None
    gmail_last_polled_at: str = ""
    drive_page_token: str | None = None
    drive_last_synced_at: str = ""
    nounou_calendar_id: str | None = None
    referentiel_folder_id: str | None = None


class ActiveSessionRecord(BaseModel):
    """Antigravity Web session lifecycle record."""

    session_id: str
    workspace: str
    status: str = "ACTIVE"
    outcome: str = ""
    source_message_id: str = ""
    plan_path: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    archived_at: str | None = None


class ActiveConversationsState(BaseModel):
    """Registry of spawned Antigravity Web sessions and archived silent sessions."""

    sessions: dict[str, ActiveSessionRecord] = Field(default_factory=dict)


class ProcessedItemEvent(BaseModel):
    """Append-only JSONL audit record for processed incoming items."""

    timestamp: str = Field(default_factory=utc_now_iso)
    item_type: str
    item_id: str
    sha256: str
    outcome: str
    details: str = ""


class KnowledgeAuditEvent(BaseModel):
    """Append-only JSONL audit record for reference knowledge updates and Drive backups."""

    timestamp: str = Field(default_factory=utc_now_iso)
    doc_id: str
    file_name: str
    action: str
    drive_backup_file_id: str = ""


class InboxOutputType(StrEnum):
    """Three exclusive outputs for the alfred-inbox new email routine."""

    SILENT_NO_ACTION = "SILENT_NO_ACTION"
    DRAFT_REPLY = "DRAFT_REPLY"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"


class EmailMessage(BaseModel):
    """Normalized incoming Gmail message model."""

    message_id: str
    thread_id: str
    sender: str
    recipient: str = ""
    subject: str
    snippet: str = ""
    body_text: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    label_ids: list[str] = Field(default_factory=list)


class InboxTriageDecision(BaseModel):
    """Deterministic and cognitive outcome of processing a single new email."""

    message_id: str
    output_type: InboxOutputType
    reason: str
    draft_id: str | None = None
    draft_body: str | None = None
    starred: bool = False
    session_id: str | None = None
    session_archived: bool = False
    implementation_plan_markdown: str | None = None
    research_brief: list[str] = Field(default_factory=list)
    suggested_knowledge_update_id: str | None = None


class ScheduledWorkday(BaseModel):
    """Contractual daily childcare schedule entry."""

    weekday: str
    start_time: str
    end_time: str
    hours: float


class VacationPeriod(BaseModel):
    """Registered vacation or leave interval for childcare planning."""

    start_date: str
    end_date: str
    source: str = "parents"
    notified_at: str | None = None
    label: str = "Vacances"

    @model_validator(mode="after")
    def _validate_date_order(self) -> VacationPeriod:
        """Ensure start_date and end_date are valid ISO dates with end_date >= start_date."""
        start_dt = date.fromisoformat(self.start_date)
        end_dt = date.fromisoformat(self.end_date)
        if end_dt < start_dt:
            raise ValueError(
                f"Vacation end_date '{self.end_date}' precedes start_date '{self.start_date}'."
            )
        if self.notified_at is not None:
            date.fromisoformat(self.notified_at)
        return self


class NounouContract(BaseModel):
    """Signed childcare contract specification for Garde Nounou and Pajemploi."""

    contract_id: str
    employee_name: str
    child_name: str
    drive_contract_file_id: str = ""
    calendar_id: str = ""
    hourly_rate_net: float
    overtime_multiplier: float = 1.10
    base_monthly_net_salary: float
    base_monthly_hours: int
    base_monthly_days: int
    maintenance_allowance_daily: float
    meal_allowance_daily: float
    vacation_notice_days: int = 30
    scheduled_workdays: list[ScheduledWorkday] = Field(default_factory=list)
    vacations: list[VacationPeriod] = Field(default_factory=list)


class CalendarEventItem(BaseModel):
    """Normalized Google Calendar event item."""

    event_id: str = ""
    summary: str
    start_iso: str
    end_iso: str
    description: str = ""


class VacationNoticeAlert(BaseModel):
    """Contractual ~1-month vacation notice check outcome."""

    vacation: VacationPeriod
    days_until_start: int
    required_notice_days: int
    is_compliant: bool
    notice_deadline_date: str
    implementation_plan_markdown: str


class PajemploiReport(BaseModel):
    """Deterministic monthly Pajemploi declaration summary computed at last_day - 3."""

    period_month: str
    employee_name: str
    heures_normales: int
    heures_complementaires_majorees: int
    jours_activite: int
    jours_conges_payes: int = 0
    salaire_net_total: float
    indemnites_entretien: float
    indemnites_repas: float
    total_virement_eur: float
    theoretical_working_hours: float
    actual_working_days: int
    absence_unpaid_hours: float
    deduction_cour_cassation_eur: float
    overtime_hours: float
    implementation_plan_markdown: str = ""


class PullRequestArtifact(BaseModel):
    """Generated Pull Request descriptor for one of the two Git repositories."""

    repo_name: str
    branch_name: str
    base_branch: str
    pr_url_or_ref: str
    status: str
    changed_files: list[str] = Field(default_factory=list)


class EvolutionProposalRecord(BaseModel):
    """Evolution lifecycle record persisted in evolution_proposals_ledger.json."""

    proposal_id: str
    title: str
    description: str
    status: str = "PENDING_GATE"
    worktree_path: str = ""
    pull_requests: list[PullRequestArtifact] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)


class EvolutionProposalsLedger(BaseModel):
    """Persistent ledger of system evolution proposals and dual-repo PRs."""

    proposals: dict[str, EvolutionProposalRecord] = Field(default_factory=dict)
