"""Dependency injection factory wiring Alfred V8.0 repositories, services, and CLI controller."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.contacts_service import ContactsService
from alfred.services.delegation_service import DelegationService
from alfred.services.drive_service import DriveService
from alfred.services.evolve_worktree_service import EvolveWorktreeService
from alfred.services.gmail_service import GmailService
from alfred.services.google_auth import GoogleOAuthService
from alfred.services.google_workspace_suite import GoogleWorkspaceSuiteService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.mcp_bridge_service import McpBridgeService
from alfred.services.onboarding_service import OnboardingService
from alfred.services.routine_service import RoutineService
from alfred.services.signature_vault_service import SignatureVaultService
from alfred.services.tasks_service import TasksService
from alfred.services.workspace_service import WorkspaceService

if TYPE_CHECKING:
    from alfred.controllers.cli_controller import CliController


def build_cli_controller(
    paths: AlfredPaths | None = None,
    root_override: str | None = None,
    **service_overrides: Any,
) -> CliController:
    """Wire Clean Architecture V8 repositories, services, and CLI controller lazily.

    Args:
        paths: Optional pre-resolved AlfredPaths layout.
        root_override: Optional filesystem root override when paths is None.
        **service_overrides: Optional injected test doubles or custom service instances.

    Returns:
        Fully wired CliController instance without eager network calls.
    """
    from alfred.controllers.cli_controller import CliController

    eff_paths = paths or AlfredPaths.from_env(root_override)
    settings: AlfredSettings = service_overrides.get("settings") or AlfredSettings.load(eff_paths)
    store: StateStore = service_overrides.get("state_store") or StateStore(eff_paths)
    oauth: GoogleOAuthService = service_overrides.get("oauth_service") or GoogleOAuthService(
        paths=eff_paths, settings=settings
    )
    drive: DriveService = service_overrides.get("drive_service") or DriveService(
        paths=eff_paths, settings=settings, state_store=store, oauth_service=oauth
    )
    token_file = eff_paths.secrets_dir / "token.json"
    knowledge: KnowledgeService = service_overrides.get("knowledge_service") or KnowledgeService(
        paths=eff_paths,
        settings=settings,
        state_store=store,
        backup_fn=lambda fn, raw: (
            drive.backup_markdown_file(fn, raw)
            if token_file.exists() and settings.features.drive_knowledge_sync
            else ""
        ),
    )
    gmail: GmailService = service_overrides.get("gmail_service") or GmailService(
        paths=eff_paths, settings=settings, oauth_service=oauth
    )
    calendar: Any = service_overrides.get("calendar_service") or CalendarService(
        paths=eff_paths, settings=settings, oauth_service=oauth, state_store=store
    )
    tasks: Any = service_overrides.get("tasks_service") or TasksService(
        paths=eff_paths, settings=settings, oauth_service=oauth
    )
    contacts: Any = service_overrides.get("contacts_service") or ContactsService(
        paths=eff_paths, settings=settings, oauth_service=oauth
    )
    suite: GoogleWorkspaceSuiteService = service_overrides.get(
        "suite_service"
    ) or GoogleWorkspaceSuiteService(paths=eff_paths, settings=settings, oauth_service=oauth)
    agy: AgySessionManager = service_overrides.get("agy_manager") or AgySessionManager(
        paths=eff_paths, settings=settings, state_store=store
    )
    return _assemble_controller(
        CliController, eff_paths, settings, store, oauth, drive, knowledge,
        gmail, calendar, tasks, contacts, suite, agy, service_overrides,
    )


def _assemble_controller(
    controller_cls: type[CliController],
    paths: AlfredPaths,
    settings: AlfredSettings,
    store: StateStore,
    oauth: GoogleOAuthService,
    drive: DriveService,
    knowledge: KnowledgeService,
    gmail: GmailService,
    calendar: Any,
    tasks: Any,
    contacts: Any,
    suite: GoogleWorkspaceSuiteService,
    agy: AgySessionManager,
    overrides: dict[str, Any],
) -> CliController:
    """Finish wiring higher-level orchestration services and construct CliController."""
    workspaces: WorkspaceService = overrides.get("workspace_service") or WorkspaceService(
        paths=paths, settings=settings, agy_manager=agy
    )
    cal_for_inbox = calendar if isinstance(calendar, CalendarService) else None
    inbox: InboxTriageService = overrides.get("inbox_service") or InboxTriageService(
        store, gmail, knowledge, drive, agy, calendar_service=cal_for_inbox, settings=settings
    )
    routines: RoutineService = overrides.get("routine_service") or RoutineService(
        paths=paths, settings=settings, state_store=store,
        inbox_service=inbox, drive_service=drive, agy_manager=agy,
    )
    evolve: EvolveWorktreeService = overrides.get("evolve_service") or EvolveWorktreeService(
        paths=paths, settings=settings, state_store=store
    )
    delegation: DelegationService = overrides.get("delegation_service") or DelegationService(
        paths=paths, settings=settings, state_store=store, agy_manager=agy, gmail_service=gmail
    )
    onboarding: OnboardingService = overrides.get("onboarding_service") or OnboardingService(
        paths=paths, settings=settings, state_store=store
    )
    signature: SignatureVaultService = overrides.get("signature_service") or SignatureVaultService(
        paths=paths, settings=settings, state_store=store
    )
    mcp: McpBridgeService = overrides.get("mcp_service") or McpBridgeService(
        paths=paths, settings=settings, state_store=store,
        agy_manager=agy, knowledge_service=knowledge,
    )
    return controller_cls(
        knowledge_service=knowledge, drive_service=drive, inbox_service=inbox,
        calendar_service=calendar, evolve_service=evolve, state_store=store,
        gmail_service=gmail, oauth_service=oauth, settings=settings,
        workspace_service=workspaces, agy_manager=agy, routine_service=routines,
        tasks_service=tasks, contacts_service=contacts, suite_service=suite,
        delegation_service=delegation, onboarding_service=onboarding,
        signature_service=signature, mcp_service=mcp, paths=paths,
    )
