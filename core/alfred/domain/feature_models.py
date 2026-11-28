"""Modular feature flags, OAuth2 scope mapping, identity aliases, and model routing schemas."""

from __future__ import annotations

from email.utils import formataddr, parseaddr

from pydantic import BaseModel, ConfigDict, Field

FEATURE_OAUTH_MAP: dict[str, tuple[str, ...]] = {
    "drive_knowledge_sync": ("https://www.googleapis.com/auth/drive",),
    "drive_watcher": ("https://www.googleapis.com/auth/drive",),
    "gmail_drafts_and_send": (
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.settings.basic",
    ),
    "inbox_auto_triage": (
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/gmail.settings.basic",
    ),
    "calendar_management": ("https://www.googleapis.com/auth/calendar",),
    "tasks_management": ("https://www.googleapis.com/auth/tasks",),
    "contacts_lookup": (
        "https://www.googleapis.com/auth/contacts",
        "https://www.googleapis.com/auth/contacts.other.readonly",
    ),
    "google_docs_suite": (
        "https://www.googleapis.com/auth/documents",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/presentations",
        "https://www.googleapis.com/auth/forms.body",
        "https://www.googleapis.com/auth/forms.responses.readonly",
    ),
}

FEATURE_GCP_APIS_MAP: dict[str, tuple[str, ...]] = {
    "drive_knowledge_sync": ("drive.googleapis.com", "sheets.googleapis.com"),
    "drive_watcher": ("drive.googleapis.com", "sheets.googleapis.com"),
    "gmail_drafts_and_send": ("gmail.googleapis.com",),
    "inbox_auto_triage": ("gmail.googleapis.com",),
    "calendar_management": ("calendar-json.googleapis.com",),
    "tasks_management": ("tasks.googleapis.com",),
    "contacts_lookup": ("people.googleapis.com",),
    "google_docs_suite": (
        "docs.googleapis.com",
        "sheets.googleapis.com",
        "slides.googleapis.com",
        "forms.googleapis.com",
    ),
}

ORCHESTRATOR_WORKSPACE_ROLES: frozenset[str] = frozenset({"onboard", "life", "evolve"})


def _normalize_sender_list(raw_val: object) -> list[str]:
    """Normalize a YAML list of RFC 2822 senders into lowercase bare email addresses."""
    if not isinstance(raw_val, list):
        return []
    return [
        addr
        for x in raw_val
        if str(x).strip() and (addr := (parseaddr(str(x))[1] or str(x)).strip().lower())
    ]


def _normalize_domain_list(raw_val: object) -> list[str]:
    """Normalize a YAML list of domains by stripping whitespace and leading '@' or '.'."""
    if not isinstance(raw_val, list):
        return []
    return [dom for x in raw_val if (dom := str(x).strip().lower().lstrip("@."))]


class FeatureFlagsConfig(BaseModel):
    """Per-capability activation flags enabling graceful skip when disabled by user."""

    model_config = ConfigDict(frozen=True)

    drive_knowledge_sync: bool = True
    inbox_auto_triage: bool = True
    gmail_drafts_and_send: bool = True
    calendar_management: bool = True
    tasks_management: bool = True
    contacts_lookup: bool = True
    google_docs_suite: bool = False
    signature_delegation: bool = False
    mcp_gemini_bridge: bool = False
    drive_watcher: bool = False

    @classmethod
    def from_dict(cls, raw: object) -> FeatureFlagsConfig:
        """Parse FeatureFlagsConfig from a YAML mapping or return safe defaults.

        Args:
            raw: Optional mapping extracted from settings YAML under 'features'.

        Returns:
            Validated FeatureFlagsConfig value object.
        """
        if not isinstance(raw, dict):
            return cls()
        return cls(
            drive_knowledge_sync=bool(raw.get("drive_knowledge_sync", True)),
            inbox_auto_triage=bool(raw.get("inbox_auto_triage", True)),
            gmail_drafts_and_send=bool(raw.get("gmail_drafts_and_send", True)),
            calendar_management=bool(raw.get("calendar_management", True)),
            tasks_management=bool(raw.get("tasks_management", True)),
            contacts_lookup=bool(raw.get("contacts_lookup", True)),
            google_docs_suite=bool(raw.get("google_docs_suite", False)),
            signature_delegation=bool(raw.get("signature_delegation", False)),
            mcp_gemini_bridge=bool(raw.get("mcp_gemini_bridge", False)),
            drive_watcher=bool(raw.get("drive_watcher", False)),
        )

    def is_enabled(self, feature_name: str) -> bool:
        """Return True if a named feature flag is enabled.

        Args:
            feature_name: Attribute name on FeatureFlagsConfig.

        Returns:
            Boolean activation status (True for core non-optional capabilities).
        """
        val = getattr(self, feature_name, None)
        return bool(val) if isinstance(val, bool) else True

    def enabled_feature_names(self) -> list[str]:
        """Return ordered list of currently enabled feature names.

        Returns:
            List of enabled feature flag identifiers.
        """
        data = self.model_dump(mode="json")
        return [name for name, active in data.items() if bool(active)]

    def compute_oauth_scopes(self) -> list[str]:
        """Compute deduplicated OAuth2 scopes strictly required by enabled features.

        Returns:
            Ordered list of Google OAuth2 scope URLs.
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for feat_name in self.enabled_feature_names():
            for scope in FEATURE_OAUTH_MAP.get(feat_name, ()):
                if scope not in seen:
                    seen.add(scope)
                    ordered.append(scope)
        return ordered

    def compute_gcp_apis(self) -> list[str]:
        """Compute deduplicated Google Cloud API identifiers required by enabled features.

        Returns:
            Ordered list of Google Cloud service domain names.
        """
        seen: set[str] = set()
        ordered: list[str] = []
        for feat_name in self.enabled_feature_names():
            for api in FEATURE_GCP_APIS_MAP.get(feat_name, ()):
                if api not in seen:
                    seen.add(api)
                    ordered.append(api)
        return ordered


class IdentityConfig(BaseModel):
    """Gmail sendAs outbound identity and Calendar attendee self-exclusion rules."""

    model_config = ConfigDict(frozen=True)

    default_send_as_email: str = ""
    default_send_as_display_name: str = ""
    calendar_organizer_email: str = ""
    never_invite_emails: list[str] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: object) -> IdentityConfig:
        """Build IdentityConfig from a YAML mapping or return empty defaults.

        Args:
            raw: Optional mapping extracted from settings YAML under 'identity'.

        Returns:
            Validated IdentityConfig value object.
        """
        if not isinstance(raw, dict):
            return cls()
        raw_never = raw.get("never_invite_emails", [])
        never_list = (
            [str(e).strip().lower() for e in raw_never if str(e).strip()]
            if isinstance(raw_never, list)
            else []
        )
        return cls(
            default_send_as_email=str(raw.get("default_send_as_email", "")).strip().lower(),
            default_send_as_display_name=str(raw.get("default_send_as_display_name", "")).strip(),
            calendar_organizer_email=str(raw.get("calendar_organizer_email", "")).strip().lower(),
            never_invite_emails=never_list,
        )

    def format_from_header(self) -> str:
        """Render RFC 2822 From header value when default_send_as_email is configured.

        Returns:
            Formatted From header string, or empty string when unset.
        """
        email = self.default_send_as_email.strip()
        if not email or "@" not in email:
            return ""
        if self.default_send_as_display_name:
            return formataddr((self.default_send_as_display_name, email))
        return email

    def blocked_calendar_attendees(
        self,
        owner_addresses: frozenset[str] = frozenset(),
    ) -> frozenset[str]:
        """Compute lowercase personal/alias emails that must never be invited on Calendar.

        Args:
            owner_addresses: Authenticated mailbox & sendAs addresses discovered via Gmail API.

        Returns:
            Frozenset of lowercase blocked attendee email addresses.
        """
        blocked: set[str] = {e.strip().lower() for e in self.never_invite_emails if "@" in e}
        for candidate in (self.default_send_as_email, self.calendar_organizer_email):
            if "@" in candidate:
                blocked.add(candidate.strip().lower())
        blocked.update(addr.strip().lower() for addr in owner_addresses if "@" in addr)
        return frozenset(blocked)


class ModelRoutingConfig(BaseModel):
    """Brain vs Muscle model routing across Antigravity specialist projects."""

    model_config = ConfigDict(frozen=True)

    orchestrator_model: str = "gemini-pro-orchestrator"
    light_model: str = "gemini-pro-orchestrator-neon"
    light_effort: str = ""
    trace_model: str = "gemini-pro-orchestrator-neon"
    trace_effort: str = ""

    @classmethod
    def from_dict(cls, raw: object) -> ModelRoutingConfig:
        """Parse ModelRoutingConfig from settings YAML under 'models'.

        Args:
            raw: Optional dictionary from YAML configuration.

        Returns:
            Validated ModelRoutingConfig value object.
        """
        if not isinstance(raw, dict):
            return cls()
        return cls(
            orchestrator_model=str(raw.get("orchestrator_model", "gemini-pro-orchestrator")),
            light_model=str(raw.get("light_model", "gemini-pro-orchestrator-neon")),
            light_effort=str(raw.get("light_effort", "")),
            trace_model=str(raw.get("trace_model", "gemini-pro-orchestrator-neon")),
            trace_effort=str(raw.get("trace_effort", "")),
        )

    def is_brain_workspace(self, workspace_or_role: str) -> bool:
        """Return True when workspace_or_role maps to a Carbon Brain workspace role.

        Args:
            workspace_or_role: Role ('life', 'evolve', 'onboard'...) or slug ('alfred-life').

        Returns:
            True when normalized role belongs to ORCHESTRATOR_WORKSPACE_ROLES.
        """
        role = workspace_or_role.strip().lower()
        if role.startswith("alfred-"):
            role = role[7:]
        return role in ORCHESTRATOR_WORKSPACE_ROLES

    def resolve_for_workspace(
        self, workspace_or_role: str, force_orchestrator: bool = False
    ) -> tuple[str, str | None]:
        """Return (model_id, effort_or_none) for a specialist workspace or escalated call.

        Args:
            workspace_or_role: Role ('life', 'evolve', 'inbox'...) or slug ('alfred-life').
            force_orchestrator: When True, escalate muscle workspaces to orchestrator_model.

        Returns:
            Tuple of (agy_model_id, optional_effort_flag).
        """
        is_brain = force_orchestrator or self.is_brain_workspace(workspace_or_role)
        model = self.orchestrator_model if is_brain else self.light_model
        if "orchestrator" in model.lower():
            return (model, None)
        effort = None if is_brain else (self.light_effort.strip() or None)
        return (model, effort)

    def resolve_trace(self) -> tuple[str, str | None]:
        """Return (model_id, effort_or_none) for lightweight archived audit traces.

        Returns:
            Tuple of (trace_model_id, optional_effort_flag).
        """
        if "orchestrator" in self.trace_model.lower():
            return (self.trace_model, None)
        return (self.trace_model, self.trace_effort.strip() or None)


class InboxFilterConfig(BaseModel):
    """Zero-token deterministic eligibility settings for alfred-inbox polling."""

    model_config = ConfigDict(frozen=True)

    bypass_calendar_invites: bool = True
    skip_promotions_social_forums: bool = True
    silent_creates_agy_session: bool = False
    denylist_senders: list[str] = Field(default_factory=list)
    denylist_domains: list[str] = Field(default_factory=list)
    allowlist_senders: list[str] = Field(default_factory=list)
    allowlist_domains: list[str] = Field(default_factory=list)
    allowlist_automated_senders: list[str] = Field(default_factory=list)
    allowlist_automated_domains: list[str] = Field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: object) -> InboxFilterConfig:
        """Build InboxFilterConfig from settings YAML under 'inbox'.

        Args:
            raw: Optional dictionary from YAML configuration.

        Returns:
            Validated InboxFilterConfig instance.
        """
        if not isinstance(raw, dict):
            return cls()
        raw_s, raw_d = raw.get("denylist_senders", []), raw.get("denylist_domains", [])
        return cls(
            bypass_calendar_invites=bool(raw.get("bypass_calendar_invites", True)),
            skip_promotions_social_forums=bool(raw.get("skip_promotions_social_forums", True)),
            silent_creates_agy_session=bool(raw.get("silent_creates_agy_session", False)),
            denylist_senders=(
                [str(x).strip().lower() for x in raw_s if str(x).strip()]
                if isinstance(raw_s, list)
                else []
            ),
            denylist_domains=(
                [str(x).strip().lower().lstrip("@") for x in raw_d if str(x).strip()]
                if isinstance(raw_d, list)
                else []
            ),
            allowlist_senders=_normalize_sender_list(raw.get("allowlist_senders", [])),
            allowlist_domains=_normalize_domain_list(raw.get("allowlist_domains", [])),
            allowlist_automated_senders=_normalize_sender_list(
                raw.get("allowlist_automated_senders", [])
            ),
            allowlist_automated_domains=_normalize_domain_list(
                raw.get("allowlist_automated_domains", [])
            ),
        )


class SecurityConfig(BaseModel):
    """Runtime security guardrails configuration for signature vault and inbox firewall."""

    model_config = ConfigDict(frozen=True)

    protected_signature_drive_ids: list[str] = Field(default_factory=list)
    require_spf_dkim_for_evolve_email: bool = True

    @classmethod
    def from_dict(cls, raw: object) -> SecurityConfig:
        """Parse SecurityConfig from settings YAML under 'security'.

        Args:
            raw: Optional dictionary from YAML configuration.

        Returns:
            Validated SecurityConfig value object.
        """
        if not isinstance(raw, dict):
            return cls()
        raw_ids = raw.get("protected_signature_drive_ids", [])
        return cls(
            protected_signature_drive_ids=(
                [str(i).strip() for i in raw_ids if str(i).strip()]
                if isinstance(raw_ids, list)
                else []
            ),
            require_spf_dkim_for_evolve_email=bool(
                raw.get("require_spf_dkim_for_evolve_email", True)
            ),
        )


class McpServerConfig(BaseModel):
    """Streamable HTTP + OAuth 2.1 configuration for the Gemini App MCP bridge."""

    model_config = ConfigDict(frozen=True)

    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    public_url: str = ""
    sync_wait_seconds: int = Field(default=25, ge=3, le=120)

    @classmethod
    def from_dict(cls, raw: object) -> McpServerConfig:
        """Parse McpServerConfig from settings YAML under 'mcp'.

        Args:
            raw: Optional dictionary from YAML configuration.

        Returns:
            Validated McpServerConfig instance.
        """
        if not isinstance(raw, dict):
            return cls()
        return cls(
            host=str(raw.get("host", "127.0.0.1")),
            port=int(raw.get("port", 8765)),
            public_url=str(raw.get("public_url", "")).strip().rstrip("/"),
            sync_wait_seconds=int(raw.get("sync_wait_seconds", 25)),
        )
