"""Google Workspace OAuth2 credential loader, auto-refresher, and interactive bootstrap."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.repositories.json_repository import atomic_write_text


class GoogleOAuthService:
    """Manage OAuth2 token loading, expiry refresh persistence, and InstalledAppFlow consent."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        """Initialize the Google OAuth2 credential manager.

        Args:
            paths: Resolved AlfredPaths filesystem layout.
            settings: Runtime structural settings including oauth_scopes.
            input_fn: Optional input callback for interactive SSH/headless redirect prompts.
        """
        self._paths = paths
        self._settings = settings
        self._input_fn = input_fn

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
                self._write_secret_file(token_file, str(creds.to_json()))
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

    def start_headless_auth(self, port: int = 8085) -> str:
        """Generate a PKCE OAuth2 authorization URL and persist pending CSRF/verifier state.

        Args:
            port: Local loopback port encoded into the redirect URI (defaults to 8085 if <= 0).

        Returns:
            Google OAuth2 consent URL for the operator to open in any browser.

        Raises:
            FileNotFoundError: If neither client_secrets.json nor client_secret.json exists.
        """
        eff_port = port if port > 0 else 8085
        secrets_file = self._resolve_client_secrets_file()
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(secrets_file),
            self._settings.oauth_scopes,
        )
        redirect_uri = f"http://localhost:{eff_port}/"
        flow.redirect_uri = redirect_uri
        auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
        raw_verifier = getattr(flow, "code_verifier", None)
        code_verifier = str(raw_verifier) if raw_verifier is not None else None
        pending_file = self._paths.secrets_dir / "oauth_pending.json"
        pending_payload: dict[str, Any] = {
            "state": str(state),
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "scopes": list(self._settings.oauth_scopes),
        }
        self._write_secret_file(pending_file, json.dumps(pending_payload, indent=2) + "\n")
        return str(auth_url)

    def complete_headless_auth(self, redirect_url_or_code: str) -> Path:
        """Complete a two-step PKCE OAuth2 flow from a redirected localhost URL or raw code.

        Args:
            redirect_url_or_code: Full localhost redirect URL or raw authorization code string.

        Returns:
            Resolved Path to the persisted instance/secrets/token.json file.

        Raises:
            FileNotFoundError: If oauth_pending.json or client_secrets.json is missing.
            ValueError: If anti-CSRF state validation fails or authorization code is invalid.
            RuntimeError: If oauth_pending.json is malformed or incomplete.
        """
        pending_file = self._paths.secrets_dir / "oauth_pending.json"
        expected_state, redirect_uri, scopes, code_verifier = self._load_pending_oauth(
            pending_file
        )
        code = self._extract_auth_code(redirect_url_or_code, expected_state)
        secrets_file = self._resolve_client_secrets_file()
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(str(secrets_file), scopes)
        flow.redirect_uri = redirect_uri
        if code_verifier is not None:
            flow.code_verifier = code_verifier
        flow.fetch_token(code=code)
        token_file = self._paths.secrets_dir / "token.json"
        self._write_secret_file(token_file, str(flow.credentials.to_json()))
        pending_file.unlink(missing_ok=True)
        return token_file

    def bootstrap_interactive_token(
        self,
        port: int = 0,
        open_browser: bool = True,
    ) -> Path:
        """Run interactive OAuth2 InstalledAppFlow from client_secrets.json and save token.json.

        Uses two-step PKCE paste flow when open_browser is False in an interactive SSH/TTY
        session (or when input_fn is injected), otherwise runs the local loopback server.

        Args:
            port: Local loopback callback port (0 selects an available ephemeral port).
            open_browser: Whether to open the authorization URL in a local browser.

        Returns:
            Resolved Path to the persisted instance/secrets/token.json file.

        Raises:
            FileNotFoundError: If neither client_secrets.json nor client_secret.json exists.
            ValueError: If pasted redirect URL/code is empty or fails state validation.
        """
        if not open_browser and (self._input_fn is not None or sys.stdin.isatty()):
            eff_port = port if port > 0 else 8085
            auth_url = self.start_headless_auth(port=eff_port)
            print(
                "\n=== Google OAuth2 Authentication (Headless / SSH Mode) ===\n"
                "1. Open the following URL in your local browser:\n"
                f"   {auth_url}\n\n"
                "2. After consent, the browser will show an error on\n"
                f"   http://localhost:{eff_port}/?state=...&code=... (expected over SSH).\n"
                "3. Copy the full URL from the address bar (or the code) below:\n"
            )
            reader = self._input_fn if self._input_fn is not None else input
            pasted = reader("Redirect URL or OAuth2 code: ").strip()
            if not pasted:
                raise ValueError("Empty OAuth2 redirect URL or authorization code.")
            return self.complete_headless_auth(pasted)

        secrets_file = self._resolve_client_secrets_file()
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(secrets_file),
            self._settings.oauth_scopes,
        )
        creds = flow.run_local_server(port=port, open_browser=open_browser)
        token_file = self._paths.secrets_dir / "token.json"
        self._write_secret_file(token_file, str(creds.to_json()))
        return token_file

    def _load_pending_oauth(self, pending_file: Path) -> tuple[str, str, list[str], str | None]:
        """Load and validate pending PKCE state, redirect URI, scopes, and verifier."""
        if not pending_file.is_file():
            raise FileNotFoundError(
                f"Missing {pending_file}; run 'alfred auth-google --print-url' first."
            )
        try:
            raw_pending = json.loads(pending_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Malformed OAuth2 pending state in {pending_file}: {exc}") from exc
        if not isinstance(raw_pending, dict):
            raise RuntimeError(
                f"Malformed OAuth2 pending state in {pending_file}: expected JSON object."
            )
        expected_state = str(raw_pending.get("state", "")).strip()
        redirect_uri = str(raw_pending.get("redirect_uri", "")).strip()
        raw_scopes = raw_pending.get("scopes")
        if (
            not expected_state
            or not redirect_uri
            or not isinstance(raw_scopes, list)
            or not raw_scopes
        ):
            raise RuntimeError(f"Incomplete OAuth2 pending state in {pending_file}.")
        raw_verifier = raw_pending.get("code_verifier")
        code_verifier = str(raw_verifier) if raw_verifier is not None else None
        return expected_state, redirect_uri, [str(s) for s in raw_scopes], code_verifier

    def _extract_auth_code(self, redirect_url_or_code: str, expected_state: str) -> str:
        """Extract authorization code from a redirect URL/query string or raw code."""
        raw_input = redirect_url_or_code.strip()
        if not raw_input:
            raise ValueError("Empty OAuth2 redirect URL or authorization code.")
        has_url_syntax = any(
            token in raw_input for token in ("://", "?", "&", "code=", "state=", "error=")
        )
        if not has_url_syntax:
            return raw_input
        query_str = (
            urlsplit(raw_input).query
            if ("://" in raw_input or "?" in raw_input)
            else raw_input
        )
        params = parse_qs(query_str, keep_blank_values=False)
        if params.get("error"):
            err = params["error"][0]
            raise ValueError(f"OAuth2 authorization error returned by Google: {err}")
        returned_state = params.get("state", [""])[0].strip()
        if returned_state != expected_state:
            raise ValueError("Invalid OAuth2 state token (CSRF verification failed).")
        code = params.get("code", [""])[0].strip()
        if not code:
            raise ValueError("Missing OAuth2 authorization code in redirect URL.")
        return code

    def _write_secret_file(self, target_path: Path, content: str) -> None:
        """Atomically persist secret file content with strict 0600 POSIX permissions."""
        atomic_write_text(target_path, content)
        os.chmod(target_path, 0o600)

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
