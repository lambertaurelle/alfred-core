"""CLI subcommand registration and handler for automated Google Cloud SDK & OAuth bootstrap."""

from __future__ import annotations

import argparse
import json
from typing import Any

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.services.gcp_bootstrap_service import GcpBootstrapService
from alfred.services.google_auth import GoogleOAuthService
from alfred.services.routine_service import RoutineService
from alfred.services.workspace_service import WorkspaceService

GCP_COMMANDS = frozenset({"bootstrap-gcp"})


def register_gcp_parsers(sub: Any) -> None:
    """Register the 'bootstrap-gcp' CLI subcommand and flags.

    Args:
        sub: Argparse subparsers collection attached to top-level alfred parser.
    """
    p_gcp = sub.add_parser("bootstrap-gcp")
    p_gcp.add_argument("--project-id", default=None)
    p_gcp.add_argument("--project-name", default="Alfred Family Office")
    p_gcp.add_argument("--non-interactive", action="store_true")
    p_gcp.add_argument("--no-install-gcloud", action="store_true")
    p_gcp.add_argument("--skip-oauth-token", action="store_true")
    p_gcp.add_argument("--finish-setup", action="store_true")
    p_gcp.add_argument("--open-browser", action="store_true")


def dispatch_gcp_command(
    paths: AlfredPaths | None,
    settings: AlfredSettings,
    oauth_service: GoogleOAuthService | None,
    workspace_service: WorkspaceService | None,
    routine_service: RoutineService | None,
    args: argparse.Namespace,
) -> int:
    """Execute 'alfred bootstrap-gcp' and print sanitized JSON report to stdout.

    Args:
        paths: Optional resolved AlfredPaths layout.
        settings: Active AlfredSettings configuration.
        oauth_service: Optional GoogleOAuthService instance.
        workspace_service: Optional WorkspaceService instance for --finish-setup.
        routine_service: Optional RoutineService instance for --finish-setup.
        args: Parsed CLI namespace.

    Returns:
        POSIX exit status code (0).
    """
    eff_paths = paths or AlfredPaths.from_env()
    svc = GcpBootstrapService(eff_paths, settings)
    report = svc.run_full_bootstrap(
        project_id=args.project_id,
        project_name=str(args.project_name),
        auto_install_gcloud=not bool(args.no_install_gcloud),
        interactive=not bool(args.non_interactive),
        run_oauth_token=not bool(args.skip_oauth_token),
        finish_setup=bool(args.finish_setup),
        oauth_service=oauth_service,
        workspace_service=workspace_service,
        routine_service=routine_service,
        open_browser=bool(args.open_browser),
    )
    print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0
