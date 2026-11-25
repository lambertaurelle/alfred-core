"""Pure domain models and configuration schemas for Alfred V8.0 Chief of Staff."""

from __future__ import annotations

import os
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from alfred.domain.attachment_models import AttachmentFilingTarget
from alfred.domain.calendar_models import (
    CalendarConfig,
    EvolutionProposalRecord,
    EvolutionProposalsLedger,
    HoldAllowlistState,
    PullRequestArtifact,
)
from alfred.domain.drive_watcher_models import DriveWatcherConfig
from alfred.domain.feature_models import (
    FeatureFlagsConfig,
    IdentityConfig,
    InboxFilterConfig,
    McpServerConfig,
    ModelRoutingConfig,
    SecurityConfig,
)
from alfred.domain.routine_models import RoutinesConfig, WorkspaceProjectStatus

__all__ = [
    "AttachmentFilingTarget", "CalendarConfig", "EvolutionProposalRecord",
    "EvolutionProposalsLedger", "FeatureFlagsConfig", "HoldAllowlistState", "IdentityConfig",
    "InboxFilterConfig", "McpServerConfig", "ModelRoutingConfig", "PullRequestArtifact",
    "RoutinesConfig", "SecurityConfig", "WorkspaceProjectStatus",
]


def _subdict(raw: dict[str, object], key: str) -> dict[str, object]:
    """Return nested dict under key or an empty dict."""
    val = raw.get(key)
    return dict(val) if isinstance(val, dict) else {}

DEFAULT_OAUTH_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/contacts.other.readonly",
)
DEFAULT_WORKSPACES: dict[str, str] = {
    "onboard": "alfred-onboard", "life": "alfred-life", "inbox": "alfred-inbox",
    "calendar": "alfred-calendar", "tasks": "alfred-tasks",
    "drive": "alfred-drive", "evolve": "alfred-evolve",
}


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
    def templates_prompts_dir(self) -> Path:
        """Return Markdown prompt templates directory path."""
        return self.core_dir / "templates" / "prompts"

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
        inst_example = self.instance_dir / "config" / "settings.example.yaml"
        if inst_example.exists():
            return inst_example
        return self.core_dir / "config" / "settings.example.yaml"

    @property
    def extensions_dir(self) -> Path:
        """Return personal active extensions directory path."""
        return self.instance_dir / "extensions"

    @property
    def examples_dir(self) -> Path:
        """Return versioned example extensions directory path."""
        return self.instance_dir / "examples"

    @property
    def secrets_dir(self) -> Path:
        """Return OAuth2 and runtime secrets directory path."""
        return self.instance_dir / "secrets"

    @property
    def signature_vault_dir(self) -> Path:
        """Return isolated handwritten signature vault directory path."""
        return self.secrets_dir / "signature_vault"

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

    @property
    def workspaces_dir(self) -> Path:
        """Return Antigravity workspace manifests directory path."""
        return self.core_dir / "workspaces"


class AlfredSettings(BaseModel):
    """Structural runtime settings loaded from YAML without personal hardcoded values."""

    project_name: str = "Alfred Family Office Chief of Staff"
    timezone: str = "Europe/Paris"
    language: str = "en-US"
    referentiel_drive_folder_name: str = "_ALFRED_REFERENTIEL_"
    primary_calendar_id: str = "primary"
    default_tasklist_id: str = "@default"
    oauth_scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_OAUTH_SCOPES))
    agy_binary: str = "agy"
    agy_remote_control_service: str = "antigravity-cli-daemon.service"
    agy_sync_daemon_on_changes: bool = True
    agy_idle_wal_grace_sec: int = 15
    archive_processed_emails_by_default: bool = True
    workspaces: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_WORKSPACES))
    max_knowledge_body_lines: int = 150
    core_base_branch: str = "main"
    features: FeatureFlagsConfig = Field(default_factory=FeatureFlagsConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    models: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)
    inbox_filters: InboxFilterConfig = Field(default_factory=InboxFilterConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    mcp: McpServerConfig = Field(default_factory=McpServerConfig)
    routines: RoutinesConfig = Field(default_factory=RoutinesConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    drive_watcher: DriveWatcherConfig = Field(default_factory=DriveWatcherConfig)
    extensions_config: dict[str, dict[str, object]] = Field(default_factory=dict)

    def resolve_workspace_project(self, workspace: str) -> str:
        """Resolve a workspace role or slug into its configured Antigravity project name.

        Args:
            workspace: Logical role ('inbox', 'calendar', 'tasks', 'drive', 'life', 'evolve'...).

        Returns:
            Target Antigravity project name.
        """
        cleaned = workspace.strip()
        if not cleaned:
            return self.workspaces.get("life", "alfred-life")
        if cleaned in self.workspaces:
            return self.workspaces[cleaned]
        if cleaned.startswith("alfred-") and cleaned[7:] in self.workspaces:
            return self.workspaces[cleaned[7:]]
        return cleaned

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
        return cls._from_raw_dict(raw if isinstance(raw, dict) else {})

    @classmethod
    def _from_raw_dict(cls, raw: dict[str, object]) -> AlfredSettings:
        """Build AlfredSettings from a parsed YAML dictionary."""
        g_dict, a_dict = _subdict(raw, "google"), _subdict(raw, "antigravity")
        i_dict, k_dict, e_dict = (
            _subdict(raw, "inbox"),
            _subdict(raw, "knowledge"),
            _subdict(raw, "evolve"),
        )
        features = FeatureFlagsConfig.from_dict(raw.get("features"))
        scopes = features.compute_oauth_scopes()
        raw_ws = a_dict.get("workspaces")
        ws_map = (
            {**DEFAULT_WORKSPACES, **{str(k): str(v) for k, v in raw_ws.items()}}
            if isinstance(raw_ws, dict)
            else dict(DEFAULT_WORKSPACES)
        )
        ext_cfg = {
            str(k): dict(v) for k, v in _subdict(raw, "extensions").items() if isinstance(v, dict)
        }
        return cls(
            project_name=str(raw.get("project_name", "Alfred Family Office Chief of Staff")),
            timezone=str(raw.get("timezone", "Europe/Paris")),
            language=str(raw.get("language", "en-US")),
            referentiel_drive_folder_name=str(
                g_dict.get("referentiel_drive_folder_name", "_ALFRED_REFERENTIEL_")
            ),
            primary_calendar_id=str(g_dict.get("primary_calendar_id", "primary")),
            default_tasklist_id=str(g_dict.get("default_tasklist_id", "@default")),
            oauth_scopes=scopes,
            agy_binary=str(a_dict.get("binary_path", "agy")),
            agy_remote_control_service=str(
                a_dict.get("remote_control_service", "antigravity-cli-daemon.service")
            ),
            agy_sync_daemon_on_changes=bool(a_dict.get("sync_daemon_on_changes", True)),
            agy_idle_wal_grace_sec=int(str(a_dict.get("idle_wal_grace_sec", 15))),
            archive_processed_emails_by_default=bool(
                i_dict.get("archive_processed_emails_by_default", True)
            ),
            workspaces=ws_map,
            max_knowledge_body_lines=int(str(k_dict.get("max_body_lines", 150))),
            core_base_branch=str(e_dict.get("core_base_branch", "main")),
            features=features,
            identity=IdentityConfig.from_dict(raw.get("identity")),
            models=ModelRoutingConfig.from_dict(raw.get("models")),
            inbox_filters=InboxFilterConfig.from_dict(i_dict),
            security=SecurityConfig.from_dict(raw.get("security")),
            mcp=McpServerConfig.from_dict(raw.get("mcp")),
            routines=RoutinesConfig.from_dict(raw.get("routines")),
            calendar=CalendarConfig.from_dict(raw.get("calendar")),
            drive_watcher=DriveWatcherConfig.from_dict(raw.get("drive_watcher")),
            extensions_config=ext_cfg,
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
    drive_watcher_page_token: str | None = None
    drive_watcher_last_polled_at: str = ""
    referentiel_folder_id: str | None = None


class ActiveSessionRecord(BaseModel):
    """Antigravity Web session lifecycle record."""

    session_id: str
    workspace: str
    status: str = "ACTIVE"
    title: str = ""
    outcome: str = ""
    source_message_id: str = ""
    parent_session_id: str = ""
    plan_path: str = ""
    created_at: str = Field(default_factory=utc_now_iso)
    archived_at: str | None = None


class ActiveConversationsState(BaseModel):
    """Registry of spawned Antigravity Web sessions and archived traces."""

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
    """Exclusive outputs for the alfred-inbox new email routine."""

    SILENT_NO_ACTION = "SILENT_NO_ACTION"
    DRAFT_REPLY = "DRAFT_REPLY"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"


class EmailMessage(BaseModel):
    """Normalized incoming Gmail message model including MIME metadata."""

    message_id: str
    thread_id: str
    sender: str
    recipient: str = ""
    subject: str
    snippet: str = ""
    body_text: str = ""
    raw_html: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    label_ids: list[str] = Field(default_factory=list)
    mime_types: list[str] = Field(default_factory=list)
    attachment_filenames: list[str] = Field(default_factory=list)


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
    calendar_session_id: str | None = None
    calendar_action_intent: str = "NONE"
    needs_orchestrator_study: bool = False
    suspicious_injection: bool = False
    skipped_pre_filter: bool = False
    keep_in_inbox: bool = False
    attachments_to_save: list[AttachmentFilingTarget] = Field(default_factory=list)


class CalendarEventItem(BaseModel):
    """Normalized Google Calendar event item with optional [HOLD], recurrence, & attendees."""

    event_id: str = ""
    summary: str
    start_iso: str
    end_iso: str
    description: str = ""
    location: str = ""
    attendees: list[str] = Field(default_factory=list)
    recurrence: list[str] = Field(default_factory=list)
    is_hold: bool = False
    thread_id: str = ""
    candidate_attendees: list[str] = Field(default_factory=list)
