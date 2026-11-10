"""Google Workspace OAuth2 credential loader, auto-refresher, and interactive bootstrap."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.repositories.json_repository import atomic_write_text


class GoogleOAuthService:
    """Manage OAuth2 token loading, expiry refresh persistence, and InstalledAppFlow consent."""

    def __init__(self, paths: AlfredPaths, settings: AlfredSettings) -> None:
        """Initialize the Google OAuth2 credential manager.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings including oauth_scopes.
        """
        self._paths = paths
        self._settings = settings

    def load_credentials(self) -> Any:
        """Load OAuth2 user credentials from token.json and refresh/persist when expired.

        Returns:
            Valid google.oauth2.credentials.Credentials object.

        Raises:
            RuntimeError: If token.json is missing or expired without a refresh_token.
        """
        token_file = self._paths.secrets_dir / "token.json"
        if not token_file.exists():
            raise RuntimeError(
                "Google OAuth2 token.json is missing; run 'alfred auth-google' after placing "
                "client_secrets.json inside instance/secrets/."
            )
        from google.oauth2.credentials import Credentials

        creds_cls: Any = Credentials
        creds = creds_cls.from_authorized_user_file(
            str(token_file),
            scopes=self._settings.oauth_scopes,
        )
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                from google.auth.transport.requests import Request

                creds.refresh(Request())
                atomic_write_text(token_file, str(creds.to_json()))
            else:
                raise RuntimeError(
                    "Google OAuth2 token.json is invalid/expired without refresh_token; "
                    "re-run 'alfred auth-google'."
                )
        return creds

    def build_api_client(self, api_name: str, api_version: str) -> Any:
        """Build an authenticated Google Workspace discovery client with refreshed credentials.

        Args:
            api_name: Discovery service name ('drive', 'sheets', 'gmail', 'calendar').
            api_version: Discovery service version ('v3', 'v4', 'v1').

        Returns:
            Initialized Google API discovery resource.
        """
        from googleapiclient.discovery import build

        creds = self.load_credentials()
        return build(api_name, api_version, credentials=creds)

    def bootstrap_interactive_token(
        self,
        port: int = 0,
        open_browser: bool = True,
    ) -> Path:
        """Run interactive OAuth2 InstalledAppFlow from client_secrets.json and save token.json.

        Args:
            port: Local loopback callback port (0 selects an available ephemeral port).
            open_browser: Whether to open the authorization URL in a local browser.

        Returns:
            Resolved Path to the persisted instance/secrets/token.json file.

        Raises:
            FileNotFoundError: If neither client_secrets.json nor client_secret.json exists.
        """
        secrets_file = self._resolve_client_secrets_file()
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(secrets_file),
            scopes=self._settings.oauth_scopes,
        )
        creds = flow.run_local_server(port=port, open_browser=open_browser)
        token_file = self._paths.secrets_dir / "token.json"
        atomic_write_text(token_file, str(creds.to_json()))
        return token_file

    def _resolve_client_secrets_file(self) -> Path:
        """Locate client_secrets.json or client_secret.json inside instance/secrets/."""
        for candidate_name in ("client_secrets.json", "client_secret.json"):
            candidate = self._paths.secrets_dir / candidate_name
            if candidate.exists():
                return candidate
        raise FileNotFoundError(
            f"Missing client_secrets.json in {self._paths.secrets_dir}; "
            "download Desktop OAuth2 client credentials from Google Cloud Console."
        )
