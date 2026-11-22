"""Domain schemas for Google Cloud SDK inspection and automated GCP OAuth2 bootstrap."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GcloudStatus(BaseModel):
    """Read-only diagnostic status of local Google Cloud SDK installation and configuration."""

    model_config = ConfigDict(frozen=True)

    installed: bool = False
    binary_path: str = ""
    active_account: str = ""
    current_project: str = ""
    authenticated: bool = False


class GcpBootstrapReport(BaseModel):
    """Sanitized status report produced by GcpBootstrapService without raw OAuth secrets."""

    status: str
    gcloud_installed: bool = False
    gcloud_installed_now: bool = False
    gcloud_binary_path: str = ""
    gcloud_account: str = ""
    project_id: str = ""
    project_created: bool = False
    apis_enabled: list[str] = Field(default_factory=list)
    oauth_client_configured: bool = False
    client_secrets_present: bool = Field(default=False, exclude=True)
    token_present: bool = False
    workspaces_initialized: bool = False
    routines_installed: bool = False
    audience_url: str = ""
    clients_url: str = ""
    next_actions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _sync_client_presence_flags(self) -> GcpBootstrapReport:
        """Keep serialized oauth_client_configured aligned with client_secrets_present."""
        eff = bool(self.oauth_client_configured or self.client_secrets_present)
        self.oauth_client_configured = eff
        self.client_secrets_present = eff
        return self
