"""Family Office onboarding inspection, 1-click GCP OAuth guide, and non-destructive settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from alfred.domain.feature_models import FeatureFlagsConfig, IdentityConfig, McpServerConfig
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.repositories.json_repository import atomic_write_text
from alfred.repositories.state_store import StateStore


class OnboardingService:
    """Manage first-run and live re-run onboarding status, OAuth2 guides, and settings.yaml."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings | None = None,
        state_store: StateStore | None = None,
    ) -> None:
        """Initialize the onboarding service.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Optional loaded AlfredSettings (defaults to AlfredSettings.load(paths)).
            state_store: Optional atomic StateStore instance.
        """
        self._paths = paths
        self._settings = settings or AlfredSettings.load(paths)
        self._state_store = state_store or StateStore(paths)

    def inspect_live_status(self) -> dict[str, Any]:
        """Inspect live instance state, OAuth scope delta, and knowledge files without raw secrets.

        Returns:
            Dictionary describing FIRST_RUN vs LIVE_RERUN, scope coverage, and active features.
        """
        settings_exists = self._paths.settings_path.is_file()
        client_secrets_exists = any(
            (self._paths.secrets_dir / name).is_file()
            for name in ("client_secrets.json", "client_secret.json")
        )
        token_file = self._paths.secrets_dir / "token.json"
        token_exists = token_file.is_file()
        granted_scopes = self._read_granted_scopes(token_file) if token_exists else []
        required_scopes = self._settings.features.compute_oauth_scopes()
        granted_set = set(granted_scopes)
        missing_scopes = [scope for scope in required_scopes if scope not in granted_set]

        knowledge_files = (
            sorted(p.name for p in self._paths.knowledge_dir.glob("*.md") if p.is_file())
            if self._paths.knowledge_dir.is_dir()
            else []
        )
        drive_indexed = self._is_drive_indexed()
        mcp_configured = bool(
            (self._paths.secrets_dir / "mcp_oauth.json").is_file()
            or self._settings.mcp.public_url
        )
        is_live = any(
            (
                settings_exists,
                client_secrets_exists,
                token_exists,
                drive_indexed,
                bool(knowledge_files),
            )
        )
        return {
            "mode": "LIVE_RERUN" if is_live else "FIRST_RUN",
            "settings_exists": settings_exists,
            "client_secrets_exists": client_secrets_exists,
            "token_exists": token_exists,
            "granted_scopes": granted_scopes,
            "required_scopes": required_scopes,
            "missing_scopes": missing_scopes,
            "enabled_features": self._settings.features.enabled_feature_names(),
            "identity": self._settings.identity.model_dump(mode="json"),
            "knowledge_files": knowledge_files,
            "drive_indexed": drive_indexed,
            "mcp_configured": mcp_configured,
        }

    def render_oauth_guide(self, features: FeatureFlagsConfig | None = None) -> str:
        """Render a clickable Markdown Google Cloud OAuth2 setup guide strictly for enabled flags.

        Args:
            features: Optional FeatureFlagsConfig override (defaults to active settings.features).

        Returns:
            Markdown guide containing 1-click GCP Console links and exact OAuth2 scope URLs.
        """
        active_features = features or self._settings.features
        apis = active_features.compute_gcp_apis()
        scopes = active_features.compute_oauth_scopes()
        api_lines = "\n".join(
            f"- [{api}](https://console.cloud.google.com/apis/library/{api})" for api in apis
        )
        scope_lines = "\n".join(f"- `{scope}`" for scope in scopes)
        secrets_dest = self._paths.secrets_dir / "client_secrets.json"
        return (
            "# Guide OAuth 2.0 Google Cloud — Alfred Family Office\n\n"
            "## 1. Créer ou sélectionner le projet Google Cloud\n"
            "- Ouvrir : [Créer un projet GCP](https://console.cloud.google.com/projectcreate)\n\n"
            "## 2. Activer uniquement les APIs requises (liens 1-clic)\n"
            f"{api_lines or '- (Aucune API Google activée)'}\n\n"
            "## 3. Configurer Google Auth Platform & Scopes OAuth 2.0\n"
            "- Configurer les scopes : "
            "[Google Auth Scopes](https://console.cloud.google.com/auth/scopes)\n"
            f"{scope_lines or '- (Aucun scope OAuth requis)'}\n\n"
            "## 4. Créer le client Desktop OAuth 2.0 (`client_secrets.json`)\n"
            "- Ouvrir : [Google Auth Clients](https://console.cloud.google.com/auth/clients)\n"
            "- Créer un client de type **Application de bureau (Desktop App)** et enregistrer le "
            f"fichier sous `{secrets_dest}` (`client_secrets.json`).\n\n"
            "## 5. Générer `token.json` (Local ou Serveur SSH Headless)\n"
            "- Poste local : `alfred auth-google`\n"
            "- Serveur distant (SSH) : `alfred auth-google --no-browser` "
            "(coller l'URL `http://localhost:...` ou le `code`), ou en 2 étapes : "
            "`alfred auth-google --print-url` puis `alfred auth-google --redirect-url \"<url>\"`.\n"
        )

    def apply_onboarding_settings(
        self,
        features: FeatureFlagsConfig | None = None,
        identity: IdentityConfig | None = None,
        mcp: McpServerConfig | None = None,
    ) -> Path:
        """Persist updated features, identity, mcp, and computed scopes into settings.yaml.

        Leaves existing instance/data/knowledge/*.md files 100% untouched byte-for-byte.

        Args:
            features: Optional updated FeatureFlagsConfig.
            identity: Optional updated IdentityConfig.
            mcp: Optional updated McpServerConfig.

        Returns:
            Resolved Path to the written instance/config/settings.yaml file.
        """
        eff_features = features if features is not None else self._settings.features
        eff_identity = identity if identity is not None else self._settings.identity
        eff_mcp = mcp if mcp is not None else self._settings.mcp
        eff_scopes = eff_features.compute_oauth_scopes()

        updated_settings = self._settings.model_copy(
            update={
                "features": eff_features,
                "identity": eff_identity,
                "mcp": eff_mcp,
                "oauth_scopes": eff_scopes,
            }
        )
        payload = self._build_settings_yaml_dict(updated_settings)
        rendered = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        atomic_write_text(self._paths.settings_path, rendered)
        self._settings = updated_settings
        return self._paths.settings_path

    def _read_granted_scopes(self, token_file: Path) -> list[str]:
        """Extract only OAuth scope identifiers from token.json without exposing token secrets."""
        try:
            raw = json.loads(token_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(raw, dict):
            return []
        raw_scopes = raw.get("scopes")
        if isinstance(raw_scopes, list):
            return [str(s).strip() for s in raw_scopes if str(s).strip()]
        scope_str = raw.get("scope")
        if isinstance(scope_str, str) and scope_str.strip():
            return [part for part in scope_str.split() if part]
        return []

    def _is_drive_indexed(self) -> bool:
        """Return True when drive_metadata_index.json exists and holds indexed Drive state."""
        if not self._paths.drive_index_path.is_file():
            return False
        idx = self._state_store.drive_index.load()
        return bool(
            idx.start_page_token or idx.file_count > 0 or idx.folder_count > 0 or idx.entries
        )

    def _build_settings_yaml_dict(self, cfg: AlfredSettings) -> dict[str, Any]:
        """Serialize AlfredSettings into canonical nested YAML dictionary for settings.yaml."""
        inbox_dump = cfg.inbox_filters.model_dump(mode="json")
        return {
            "project_name": cfg.project_name,
            "timezone": cfg.timezone,
            "language": cfg.language,
            "features": cfg.features.model_dump(mode="json"),
            "identity": cfg.identity.model_dump(mode="json"),
            "models": cfg.models.model_dump(mode="json"),
            "google": {
                "referentiel_drive_folder_name": cfg.referentiel_drive_folder_name,
                "primary_calendar_id": cfg.primary_calendar_id,
                "default_tasklist_id": cfg.default_tasklist_id,
                "oauth_scopes": list(cfg.oauth_scopes),
            },
            "inbox": {
                "archive_processed_emails_by_default": cfg.archive_processed_emails_by_default,
                **inbox_dump,
            },
            "calendar": cfg.calendar.model_dump(mode="json"),
            "security": cfg.security.model_dump(mode="json"),
            "mcp": cfg.mcp.model_dump(mode="json"),
            "antigravity": {
                "binary_path": cfg.agy_binary,
                "remote_control_service": cfg.agy_remote_control_service,
                "sync_daemon_on_changes": cfg.agy_sync_daemon_on_changes,
                "idle_wal_grace_sec": cfg.agy_idle_wal_grace_sec,
                "workspaces": dict(cfg.workspaces),
            },
            "knowledge": {"max_body_lines": cfg.max_knowledge_body_lines},
            "evolve": {"core_base_branch": cfg.core_base_branch},
            "routines": cfg.routines.model_dump(mode="json"),
            "extensions": dict(cfg.extensions_config),
        }
