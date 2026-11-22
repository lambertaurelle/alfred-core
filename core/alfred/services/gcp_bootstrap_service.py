"""Automated Google Cloud SDK bootstrap, project/API enablement, and OAuth2 onboarding."""

from __future__ import annotations

import platform
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.domain.onboarding_models import GcloudStatus, GcpBootstrapReport
from alfred.services.gcp_sdk_helpers import (
    default_sdk_downloader,
    default_subprocess_runner,
    is_valid_project_id,
    select_sdk_url,
    validate_and_save_client_json,
    validate_project_id,
    validate_sdk_download_url,
    validate_tar_members,
)


class GcpBootstrapService:
    """Inspect and automate gcloud CLI install, auth, project/APIs, and OAuth setup."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings | None = None,
        *,
        runner_fn: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        downloader_fn: Callable[[str, Path], None] | None = None,
        input_fn: Callable[[str], str] | None = None,
        install_root: Path | None = None,
        machine_arch: str | None = None,
    ) -> None:
        """Initialize the automated GCP bootstrap service.

        Args:
            paths: Resolved AlfredPaths layout.
            settings: Optional AlfredSettings providing feature flags and OAuth scopes.
            runner_fn: Optional subprocess runner test double.
            downloader_fn: Optional archive downloader taking (url, dest_path).
            input_fn: Optional interactive prompt reader for JSON pasting / OAuth fallback.
            install_root: Optional base directory for local gcloud SDK (default ~/.local).
            machine_arch: Optional CPU architecture override (default platform.machine()).
        """
        self._paths = paths
        self._settings = settings or AlfredSettings.load(paths)
        self._runner = runner_fn or default_subprocess_runner
        self._downloader = downloader_fn or default_sdk_downloader
        self._input_fn = input_fn
        self._install_root = install_root
        self._machine_arch = machine_arch

    def _resolve_install_root(self) -> Path:
        """Resolve local SDK parent directory (~/.local unless explicitly injected)."""
        return (self._install_root or (Path.home() / ".local")).resolve()

    def locate_gcloud_binary(self) -> Path | None:
        """Locate an installed gcloud binary in PATH or ~/.local/google-cloud-sdk/bin.

        Returns:
            Path to the gcloud executable, or None if not installed.
        """
        which_bin = shutil.which("gcloud")
        if which_bin:
            return Path(which_bin)
        candidates = [
            self._resolve_install_root() / "google-cloud-sdk" / "bin" / "gcloud",
            Path.home() / ".local" / "google-cloud-sdk" / "bin" / "gcloud",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def inspect_gcloud(self) -> GcloudStatus:
        """Inspect gcloud installation, active account, and active project safely.

        Returns:
            GcloudStatus snapshot (never raises on missing binary).
        """
        gcloud_bin = self.locate_gcloud_binary()
        if gcloud_bin is None:
            return GcloudStatus(installed=False)
        account = self._read_gcloud_config(gcloud_bin, "account")
        project = self._read_gcloud_config(gcloud_bin, "project")
        return GcloudStatus(
            installed=True,
            binary_path=str(gcloud_bin),
            active_account=account,
            current_project=project,
            authenticated=bool(account),
        )

    def ensure_gcloud_installed(
        self,
        auto_install: bool = True,
        download_url_override: str | None = None,
    ) -> tuple[Path | None, bool]:
        """Locate gcloud or safely install the official multi-arch archive into ~/.local.

        Args:
            auto_install: When True, download and install the official SDK if missing.
            download_url_override: Optional override URL validated against dl.google.com.

        Returns:
            Tuple of (gcloud_binary_path_or_None, installed_during_this_call).
        """
        if download_url_override is not None:
            validate_sdk_download_url(download_url_override)
        existing = self.locate_gcloud_binary()
        if existing is not None:
            return existing, False
        if not auto_install:
            return None, False

        arch = self._machine_arch or platform.machine()
        url = (
            validate_sdk_download_url(download_url_override)
            if download_url_override is not None
            else select_sdk_url(arch)
        )
        install_root = self._resolve_install_root()
        install_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            archive = Path(tmp_dir) / "google-cloud-cli.tar.gz"
            self._downloader(url, archive)
            with tarfile.open(archive, "r:gz") as tf:
                safe_members = validate_tar_members(tf, install_root)
                tf.extractall(path=install_root, members=safe_members)

        install_sh = install_root / "google-cloud-sdk" / "install.sh"
        if install_sh.is_file():
            self._runner(
                [
                    str(install_sh), "--quiet", "--usage-reporting=false",
                    "--path-update=false", "--disable-prompts",
                ],
                cwd=install_root,
            )
        bin_path = install_root / "google-cloud-sdk" / "bin" / "gcloud"
        if not bin_path.is_file():
            raise RuntimeError(f"gcloud binary missing after SDK installation under {bin_path}")
        return bin_path, True

    def authenticate_gcloud(self, gcloud_bin: Path, interactive: bool = True) -> str:
        """Ensure gcloud has an active authenticated account or trigger SSH-safe login.

        Args:
            gcloud_bin: Resolved path to the gcloud executable.
            interactive: When True, invoke 'gcloud auth login --no-launch-browser' if needed.

        Returns:
            Active authenticated Google account email.

        Raises:
            RuntimeError: If unauthenticated in non-interactive mode or login fails.
        """
        account = self._read_gcloud_config(gcloud_bin, "account")
        if account:
            return account
        if not interactive:
            raise RuntimeError(
                "gcloud is not authenticated; run 'gcloud auth login --no-launch-browser'."
            )
        login_res = self._runner([str(gcloud_bin), "auth", "login", "--no-launch-browser"])
        if login_res.returncode != 0:
            raise RuntimeError(f"gcloud auth login failed: {login_res.stderr.strip()}")
        account = self._read_gcloud_config(gcloud_bin, "account")
        if not account:
            raise RuntimeError("gcloud auth login completed but no active account is configured.")
        return account

    def ensure_project_and_enable_apis(
        self,
        gcloud_bin: Path,
        project_id: str | None = None,
        project_name: str = "Alfred Family Office",
    ) -> tuple[str, bool, list[str]]:
        """Verify/create a GCP project and enable only APIs required by FeatureFlagsConfig.

        Args:
            gcloud_bin: Resolved path to the gcloud executable.
            project_id: Optional explicit GCP project ID (validated before any subprocess).
            project_name: Display name used when creating a brand-new GCP project.

        Returns:
            Tuple of (project_id, created_now, enabled_api_list).
        """
        if project_id is not None:
            eff_pid = validate_project_id(project_id)
        else:
            current = self._read_gcloud_config(gcloud_bin, "project")
            eff_pid = (
                validate_project_id(current)
                if current and is_valid_project_id(current)
                else f"alfred-fo-{secrets.token_hex(4)}"
            )

        desc_res = self._runner([str(gcloud_bin), "projects", "describe", eff_pid])
        created = desc_res.returncode != 0
        if created:
            create_res = self._runner(
                [str(gcloud_bin), "projects", "create", eff_pid, f"--name={project_name}"]
            )
            if create_res.returncode != 0:
                raise RuntimeError(
                    f"Failed to create GCP project '{eff_pid}': {create_res.stderr.strip()}"
                )
        self._runner([str(gcloud_bin), "config", "set", "project", eff_pid])
        apis = self._settings.features.compute_gcp_apis()
        if apis:
            enable_res = self._runner(
                [str(gcloud_bin), "services", "enable", *apis, f"--project={eff_pid}"]
            )
            if enable_res.returncode != 0:
                raise RuntimeError(
                    f"Failed to enable GCP APIs on '{eff_pid}': {enable_res.stderr.strip()}"
                )
        return eff_pid, created, apis

    def maybe_ingest_oauth_client_json(
        self,
        project_id: str,
        interactive: bool = True,
    ) -> bool:
        """Check or interactively ingest Desktop OAuth2 client JSON atomically at 0600.

        Args:
            project_id: Active GCP project ID used to display targeted Console links.
            interactive: When True and no file exists, prompt user to paste OAuth JSON.

        Returns:
            True if valid OAuth client JSON exists (or was ingested), False otherwise.

        Raises:
            ValueError: If pasted JSON is malformed or missing OAuth client credentials.
        """
        if self._resolve_existing_client_file() is not None:
            return True
        if not interactive:
            return False

        audience_url = f"https://console.cloud.google.com/auth/audience?project={project_id}"
        clients_url = f"https://console.cloud.google.com/auth/clients?project={project_id}"
        print(
            "\n=== Création du client Desktop OAuth2 Google Cloud ===\n"
            f"1. Écran de consentement (Audience) : {audience_url}\n"
            f"2. Client OAuth Desktop : {clients_url}\n"
            "3. Téléchargez le fichier JSON Desktop App et collez son contenu sur une ligne :\n"
        )
        reader = self._input_fn if self._input_fn is not None else input
        raw_text = reader("Contenu JSON du client OAuth Desktop : ").strip()
        if not raw_text:
            raise ValueError("Empty OAuth Desktop JSON input.")
        validate_and_save_client_json(self._paths.secrets_dir, raw_text)
        return True

    def run_full_bootstrap(
        self,
        *,
        project_id: str | None = None,
        project_name: str = "Alfred Family Office",
        auto_install_gcloud: bool = True,
        interactive: bool = True,
        run_oauth_token: bool = True,
        finish_setup: bool = False,
        oauth_service: Any | None = None,
        workspace_service: Any | None = None,
        routine_service: Any | None = None,
        open_browser: bool = False,
    ) -> GcpBootstrapReport:
        """Orchestrate end-to-end GCP bootstrap from gcloud install through OAuth & routines.

        Args:
            project_id: Optional explicit GCP project ID (validated before any subprocess).
            project_name: Display name for project creation.
            auto_install_gcloud: Automatically install official gcloud CLI if absent.
            interactive: Enable interactive gcloud auth login and OAuth JSON paste prompts.
            run_oauth_token: Generate instance/secrets/token.json when missing.
            finish_setup: Also initialize 6 Antigravity workspaces and systemd routines.
            oauth_service: Optional GoogleOAuthService override.
            workspace_service: Optional WorkspaceService override.
            routine_service: Optional RoutineService override.
            open_browser: Whether OAuth consent may attempt opening a local browser.

        Returns:
            Sanitized GcpBootstrapReport containing status, URLs, and next actions.
        """
        if project_id is not None:
            validate_project_id(project_id)
        gcloud_bin, installed_now = self.ensure_gcloud_installed(auto_install=auto_install_gcloud)
        if gcloud_bin is None:
            return GcpBootstrapReport(
                status="GCLOUD_MISSING", gcloud_installed=False,
                next_actions=["Install Google Cloud SDK or rerun without --no-install-gcloud."],
            )
        try:
            account = self.authenticate_gcloud(gcloud_bin, interactive=interactive)
        except RuntimeError:
            return GcpBootstrapReport(
                status="NEEDS_GCLOUD_AUTH", gcloud_installed=True,
                gcloud_installed_now=installed_now, gcloud_binary_path=str(gcloud_bin),
                next_actions=["Run 'gcloud auth login --no-launch-browser' then rerun bootstrap."],
            )
        eff_pid, created, apis = self.ensure_project_and_enable_apis(
            gcloud_bin, project_id=project_id, project_name=project_name
        )
        return self._complete_oauth_and_setup(
            gcloud_bin=gcloud_bin, installed_now=installed_now, account=account,
            project_id=eff_pid, project_created=created, apis=apis,
            interactive=interactive, run_oauth_token=run_oauth_token,
            finish_setup=finish_setup, oauth_service=oauth_service,
            workspace_service=workspace_service, routine_service=routine_service,
            open_browser=open_browser,
        )

    def _complete_oauth_and_setup(
        self,
        *,
        gcloud_bin: Path,
        installed_now: bool,
        account: str,
        project_id: str,
        project_created: bool,
        apis: list[str],
        interactive: bool,
        run_oauth_token: bool,
        finish_setup: bool,
        oauth_service: Any | None,
        workspace_service: Any | None,
        routine_service: Any | None,
        open_browser: bool,
    ) -> GcpBootstrapReport:
        """Finalize Desktop OAuth JSON ingestion, token generation, and workspace/routine init."""
        audience_url = f"https://console.cloud.google.com/auth/audience?project={project_id}"
        clients_url = f"https://console.cloud.google.com/auth/clients?project={project_id}"
        base_kw: dict[str, Any] = {
            "gcloud_installed": True, "gcloud_installed_now": installed_now,
            "gcloud_binary_path": str(gcloud_bin), "gcloud_account": account,
            "project_id": project_id, "project_created": project_created,
            "apis_enabled": apis, "audience_url": audience_url, "clients_url": clients_url,
        }
        if not self.maybe_ingest_oauth_client_json(project_id, interactive=interactive):
            return GcpBootstrapReport(
                status="NEEDS_OAUTH_CLIENT_JSON", **base_kw,
                next_actions=[
                    f"Configure consent screen at {audience_url}",
                    f"Create Desktop OAuth client at {clients_url} and rerun interactively.",
                ],
            )
        token_file = self._paths.secrets_dir / "token.json"
        if not token_file.is_file() and run_oauth_token:
            from alfred.services.google_auth import GoogleOAuthService

            eff_oauth = oauth_service or GoogleOAuthService(
                self._paths, self._settings, input_fn=self._input_fn
            )
            eff_oauth.bootstrap_interactive_token(open_browser=open_browser)
        if not token_file.is_file():
            return GcpBootstrapReport(
                status="NEEDS_OAUTH_TOKEN", oauth_client_configured=True, token_present=False,
                next_actions=["Run 'alfred auth-google --no-browser' to generate token.json."],
                **base_kw,
            )
        ws_done = bool(finish_setup and workspace_service is not None)
        if ws_done and workspace_service is not None:
            workspace_service.init_workspaces(prune_legacy=True)
        rt_done = bool(finish_setup and routine_service is not None)
        if rt_done and routine_service is not None:
            routine_service.install_systemd_timers()
        return GcpBootstrapReport(
            status="READY", oauth_client_configured=True, token_present=True,
            workspaces_initialized=ws_done, routines_installed=rt_done, **base_kw,
        )

    def _read_gcloud_config(self, gcloud_bin: Path, key: str) -> str:
        """Read a gcloud config property cleanly, mapping '(unset)' to empty string."""
        res = self._runner([str(gcloud_bin), "config", "get-value", key])
        if res.returncode != 0:
            return ""
        val = res.stdout.strip()
        return "" if val in {"", "(unset)"} else val

    def _resolve_existing_client_file(self) -> Path | None:
        """Return existing OAuth Desktop client JSON file path if present in secrets_dir."""
        for name in ("client_secrets.json", "client_secret.json"):
            candidate = self._paths.secrets_dir / name
            if candidate.is_file():
                return candidate
        return None
