"""Alfred V7.1 Lean CLI entrypoint and dependency injection bootstrap (< 80 lines)."""

from __future__ import annotations

import sys

from alfred.controllers.cli_controller import CliController
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.evolve_worktree_service import EvolveWorktreeService
from alfred.services.gmail_service import GmailService
from alfred.services.google_auth import GoogleOAuthService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.nounou_service import NounouService
from alfred.services.pajemploi_calculator import PajemploiCalculator


def build_controller(root_override: str | None = None) -> CliController:
    """Wire Clean Architecture repositories, services, and CLI controller.

    Args:
        root_override: Optional override for ALFRED_ROOT_DIR.

    Returns:
        Fully wired CliController instance.
    """
    paths = AlfredPaths.from_env(root_override)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    oauth = GoogleOAuthService(paths=paths, settings=settings)
    drive = DriveService(paths=paths, settings=settings, state_store=store, oauth_service=oauth)
    token_file = paths.secrets_dir / "token.json"
    backup = drive.backup_markdown_file
    knowledge = KnowledgeService(
        paths=paths,
        settings=settings,
        state_store=store,
        backup_fn=lambda fn, raw: backup(fn, raw) if token_file.exists() else "",
    )
    gmail = GmailService(paths=paths, settings=settings, oauth_service=oauth)
    calendar = CalendarService(paths=paths, settings=settings, oauth_service=oauth)
    agy = AgySessionManager(paths=paths, settings=settings, state_store=store)
    inbox = InboxTriageService(store, gmail, knowledge, drive, agy)
    nounou = NounouService(store, calendar, knowledge, settings)
    evolve = EvolveWorktreeService(paths=paths, settings=settings, state_store=store)
    return CliController(
        knowledge,
        drive,
        inbox,
        nounou,
        PajemploiCalculator(),
        calendar,
        evolve,
        store,
        gmail_service=gmail,
        oauth_service=oauth,
        timezone=settings.timezone,
    )


def main(argv: list[str] | None = None) -> int:
    """Execute the Alfred CLI entrypoint.

    Args:
        argv: Optional argument list; defaults to sys.argv[1:].

    Returns:
        Exit status code.
    """
    return build_controller().run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
