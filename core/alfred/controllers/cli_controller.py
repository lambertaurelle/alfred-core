"""Command-line controller exposing Alfred V8.0 Family Office Chief of Staff workflows."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from alfred.controllers.app_factory import build_cli_controller
from alfred.controllers.calendar_cli import (
    dispatch_calendar_command,
    print_skipped_disabled,
    register_calendar_parsers,
)
from alfred.controllers.onboarding_cli import (
    ONBOARDING_AND_BRIDGE_COMMANDS,
    dispatch_onboarding_and_bridges,
    register_onboarding_and_bridge_parsers,
)
from alfred.controllers.routine_cli import (
    handle_archive_email,
    handle_backfill_inbox_archives,
    handle_install_routines,
    handle_read_thread,
    handle_run_routines,
    handle_search_gmail,
    handle_send_draft,
    handle_status_routines,
    handle_sync_agy_ui,
    register_routine_and_gmail_parsers,
)
from alfred.controllers.tasks_cli import (
    CONTACT_COMMANDS,
    SUITE_COMMANDS,
    TASK_COMMANDS,
    dispatch_tasks_contacts_suite,
    register_tasks_contacts_suite_parsers,
)
from alfred.domain.models import AlfredPaths, AlfredSettings, FrontmatterSchema
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
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
from alfred.services.workspace_service import WorkspaceService

__all__ = ["CliController", "build_cli_controller"]

CALENDAR_COMMANDS = frozenset(
    {
        "list-calendars",
        "query-freebusy",
        "list-events",
        "hold-allowlist",
        "create-event",
        "update-event",
        "confirm-event",
        "delete-event",
    }
)
ALL_TASKS_SUITE_COMMANDS = TASK_COMMANDS | CONTACT_COMMANDS | SUITE_COMMANDS


class CliController:
    """Dispatch CLI subcommands across Onboarding, Knowledge, Inbox, Calendar, Tasks, & Evolve."""

    def __init__(
        self,
        knowledge_service: KnowledgeService,
        drive_service: DriveService,
        inbox_service: InboxTriageService,
        calendar_service: Any,
        evolve_service: EvolveWorktreeService,
        state_store: StateStore,
        gmail_service: GmailService | None = None,
        oauth_service: GoogleOAuthService | None = None,
        timezone: str = "Europe/Paris",
        settings: AlfredSettings | None = None,
        workspace_service: WorkspaceService | None = None,
        agy_manager: AgySessionManager | None = None,
        routine_service: RoutineService | None = None,
        tasks_service: Any = None,
        contacts_service: Any = None,
        suite_service: GoogleWorkspaceSuiteService | None = None,
        delegation_service: DelegationService | None = None,
        onboarding_service: OnboardingService | None = None,
        signature_service: SignatureVaultService | None = None,
        mcp_service: McpBridgeService | None = None,
        paths: AlfredPaths | None = None,
    ) -> None:
        """Initialize the V8 CLI controller with injected domain services.

        Args:
            knowledge_service: 3-tier YAML frontmatter knowledge service.
            drive_service: Google Drive scaffolder, discovery, and native reader service.
            inbox_service: New-email triage service with 3 exclusive outputs.
            calendar_service: Google Calendar v3 multi-calendar connector.
            evolve_service: Isolated Mono-Repo git worktree and PR service.
            state_store: Local atomic state facade.
            gmail_service: Optional Gmail API connector.
            oauth_service: Optional GoogleOAuthService for interactive OAuth2 bootstrap.
            timezone: Configured local timezone string (default 'Europe/Paris').
            settings: Optional AlfredSettings instance.
            workspace_service: Optional Antigravity 6-workspace provisioner.
            agy_manager: Optional Antigravity session manager for live Gate spawning.
            routine_service: Optional RoutineService orchestrating systemd & periodic jobs.
            tasks_service: Optional Google Tasks v1 service.
            contacts_service: Optional Google People v1 contacts service.
            suite_service: Optional Google Workspace Suite (Docs/Sheets/Slides/Forms) service.
            delegation_service: Optional specialist cross-project DelegationService.
            onboarding_service: Optional OnboardingService for status and OAuth guides.
            signature_service: Optional SignatureVaultService for human-consented PDF signing.
            mcp_service: Optional McpBridgeService for OAuth 2.1 Gemini App bridge.
            paths: Optional resolved AlfredPaths layout.
        """
        self._knowledge = knowledge_service
        self._drive = drive_service
        self._inbox = inbox_service
        self._calendar = calendar_service
        self._evolve = evolve_service
        self._store = state_store
        self._gmail = gmail_service
        self._oauth = oauth_service
        self._settings = settings or AlfredSettings(timezone=timezone)
        self._timezone = settings.timezone if settings is not None else timezone
        self._workspaces = workspace_service
        self._agy = agy_manager
        self._routines = routine_service
        self._tasks = tasks_service
        self._contacts = contacts_service
        self._suite = suite_service
        self._delegation = delegation_service
        self._onboarding = onboarding_service
        self._signature = signature_service
        self._mcp = mcp_service
        self._paths = paths

    def run(self, argv: list[str]) -> int:
        """Parse CLI arguments and execute the requested Alfred V8 workflow.

        Args:
            argv: Command-line argument list excluding program name.
        """
        args = self._build_parser().parse_args(argv)
        cmd = str(args.command)
        if cmd not in {"status-onboarding", "oauth-guide", "apply-onboarding", "auth-google"}:
            self._knowledge.bootstrap_templates_if_empty()
        if cmd in CALENDAR_COMMANDS:
            return dispatch_calendar_command(
                cmd, self._calendar, self._settings, self._store,
                self._gmail, self._agy, self._timezone, args,
            )
        if cmd in ALL_TASKS_SUITE_COMMANDS:
            return dispatch_tasks_contacts_suite(
                cmd, self._tasks, self._contacts, self._suite, self._settings, args
            )
        if cmd in ONBOARDING_AND_BRIDGE_COMMANDS:
            return self._dispatch_onboarding_bridge(cmd, args)
        handler = self._core_handlers().get(cmd)
        return handler(args) if handler is not None else 1

    def _dispatch_onboarding_bridge(self, cmd: str, args: argparse.Namespace) -> int:
        """Ensure lazy fallback services exist and dispatch onboarding/bridge commands."""
        paths = self._paths or AlfredPaths.from_env()
        onb = self._onboarding or OnboardingService(paths, self._settings, self._store)
        dlg = self._delegation or DelegationService(
            paths, self._settings, self._store, self._agy, self._gmail
        )
        sig = self._signature or SignatureVaultService(paths, self._settings, self._store)
        mcp = self._mcp or McpBridgeService(
            paths, self._settings, self._store, self._agy, self._knowledge
        )
        return dispatch_onboarding_and_bridges(
            cmd, onb, dlg, sig, mcp, self._gmail, self._drive, self._agy, self._settings, args
        )

    def _core_handlers(self) -> dict[str, Callable[[argparse.Namespace], int]]:
        """Return handlers for auth, workspaces, knowledge, drive, inbox, routines, & evolve."""
        return {
            "auth-google": self._handle_auth_google,
            "init-workspaces": lambda a: self._handle_init_workspaces(bool(a.prune_legacy)),
            "scan-knowledge": lambda a: self._handle_scan_knowledge(str(a.query)),
            "load-knowledge": lambda a: self._handle_load_knowledge(str(a.doc)),
            "upsert-knowledge": self._handle_upsert_knowledge,
            "init-drive": lambda a: self._handle_init_drive(bool(a.execute)),
            "search-drive": lambda a: self._handle_search_drive(str(a.query)),
            "sync-drive": lambda _a: self._handle_sync_drive(),
            "read-drive": lambda a: self._handle_read_drive(str(a.file_id)),
            "poll-inbox": self._handle_poll_inbox,
            "send-draft": lambda a: handle_send_draft(self._gmail, self._settings, a),
            "archive-email": lambda a: handle_archive_email(self._gmail, a, self._settings),
            "backfill-inbox-archives": lambda _a: handle_backfill_inbox_archives(
                self._inbox, self._agy
            ),
            "sync-agy-ui": lambda a: handle_sync_agy_ui(self._agy, a),
            "evolve": self._handle_evolve,
            "run-routines": lambda a: handle_run_routines(self._routines, a),
            "install-routines": lambda a: handle_install_routines(self._routines, a),
            "status-routines": lambda a: handle_status_routines(self._routines, a),
            "search-gmail": lambda a: handle_search_gmail(self._gmail, a),
            "read-thread": lambda a: handle_read_thread(self._gmail, a),
        }

    def _build_parser(self) -> argparse.ArgumentParser:
        """Construct the top-level argparse command parser."""
        parser = argparse.ArgumentParser(prog="alfred", description="Alfred V8.0 Family Office CLI")
        sub = parser.add_subparsers(dest="command", required=True)

        p_auth = sub.add_parser("auth-google")
        p_auth.add_argument("--port", type=int, default=0)
        p_auth.add_argument("--no-browser", action="store_true")
        p_auth.add_argument("--print-url", action="store_true")
        p_auth.add_argument("--redirect-url", default="")

        p_ws = sub.add_parser("init-workspaces")
        p_ws.add_argument("--prune-legacy", action="store_true")

        self._register_knowledge_and_drive_parsers(sub)
        self._register_inbox_and_evolve_parsers(sub)
        register_calendar_parsers(sub)
        register_tasks_contacts_suite_parsers(sub)
        register_onboarding_and_bridge_parsers(sub)
        register_routine_and_gmail_parsers(sub)
        return parser

    def _register_knowledge_and_drive_parsers(self, sub: Any) -> None:
        """Register Tier-1/2/3 knowledge and Google Drive subcommands."""
        p_know = sub.add_parser("scan-knowledge")
        p_know.add_argument("--query", default="")

        p_load = sub.add_parser("load-knowledge")
        p_load.add_argument("--doc", required=True)

        p_ups = sub.add_parser("upsert-knowledge")
        for flag in ("--file", "--id", "--title", "--domain", "--summary", "--body-file"):
            p_ups.add_argument(flag, required=True)
        p_ups.add_argument("--keyword", action="append", default=[])
        p_ups.add_argument("--drive-source-id", action="append", default=[])

        p_drv = sub.add_parser("init-drive")
        p_drv.add_argument("--execute", action="store_true")

        p_sdrv = sub.add_parser("search-drive")
        p_sdrv.add_argument("--query", required=True)

        sub.add_parser("sync-drive")

        p_rdrv = sub.add_parser("read-drive")
        p_rdrv.add_argument("--file-id", required=True)

    def _register_inbox_and_evolve_parsers(self, sub: Any) -> None:
        """Register Gmail inbox triage, draft sending, and Mono-Repo evolve subcommands."""
        p_poll = sub.add_parser("poll-inbox")
        p_poll.add_argument("--spawn-agy", action="store_true")
        p_poll.add_argument("--cognitive-agy", action="store_true")

        p_dr = sub.add_parser("send-draft")
        p_dr.add_argument("--draft-id", required=True)
        for flag in ("--thread-id", "--to", "--subject", "--body"):
            p_dr.add_argument(flag, default="")
        p_dr.add_argument("--keep-in-inbox", action="store_true")
        p_dr.add_argument("--unstar", action="store_true")

        p_ev = sub.add_parser("evolve")
        for flag in ("--id", "--title", "--description"):
            p_ev.add_argument(flag, required=True)
        p_ev.add_argument("--execute", action="store_true")
        p_ev.add_argument("--spawn-agy", action="store_true")
        p_ev.add_argument("--worktree", default="")

    def _handle_init_workspaces(self, prune_legacy: bool) -> int:
        """Provision the 6 specialist Antigravity projects and print JSON status."""
        if self._workspaces is None:
            raise RuntimeError("WorkspaceService is not wired on CliController.")
        statuses = self._workspaces.init_workspaces(prune_legacy=prune_legacy)
        print(json.dumps([s.model_dump(mode="json") for s in statuses], indent=2))
        return 0

    def _maybe_spawn_gate(
        self, workspace: str, source_id: str, plan_md: str, spawn: bool
    ) -> None:
        """Optionally materialize a live Antigravity Web Gate session for a workspace."""
        if spawn and self._agy is not None:
            self._agy.create_session(
                workspace=workspace, source_message_id=source_id,
                implementation_plan_md=plan_md, spawn_live=True,
            )
            self._agy.daemon_sync.sync_if_needed()

    def _handle_auth_google(self, args: argparse.Namespace) -> int:
        """Execute interactive, print-url, or redirect-url OAuth2 consent flow."""
        if self._oauth is None:
            raise RuntimeError("GoogleOAuthService is not wired on CliController.")
        port = int(args.port)
        print_url = bool(args.print_url)
        redirect_url = str(args.redirect_url).strip()
        if print_url and redirect_url:
            raise ValueError("Cannot pass both --print-url and --redirect-url simultaneously.")
        if print_url:
            eff_port = port if port > 0 else 8085
            auth_url = self._oauth.start_headless_auth(port=eff_port)
            payload = {
                "auth_url": auth_url,
                "redirect_uri": f"http://localhost:{eff_port}/",
                "status": "PENDING_CODE",
            }
            print(json.dumps(payload, indent=2))
            return 0
        if redirect_url:
            token_path = self._oauth.complete_headless_auth(redirect_url)
            print(json.dumps({"token_path": str(token_path), "status": "OK"}, indent=2))
            return 0
        token_path = self._oauth.bootstrap_interactive_token(
            port=port, open_browser=not bool(args.no_browser)
        )
        print(json.dumps({"token_path": str(token_path), "status": "OK"}, indent=2))
        return 0

    def _handle_scan_knowledge(self, query: str) -> int:
        """Print Tier-1 YAML frontmatters matching query or all frontmatters."""
        items = (
            self._knowledge.select_relevant_frontmatters(query)
            if query
            else self._knowledge.scan_frontmatters()
        )
        payload = [{"file": fn, **fm.model_dump(mode="json")} for fn, fm in items]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    def _handle_load_knowledge(self, doc_id_or_filename: str) -> int:
        """Load a Tier-2 reference .md document and print its KnowledgeDocument JSON."""
        doc = self._knowledge.load_document(doc_id_or_filename)
        print(json.dumps(doc.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0

    def _handle_upsert_knowledge(self, args: argparse.Namespace) -> int:
        """Upsert a Tier-2 reference .md file with Drive backup and print its JSON."""
        fm = FrontmatterSchema(
            id=str(args.id), title=str(args.title), domain=str(args.domain),
            summary=str(args.summary), keywords=[str(k) for k in args.keyword],
            drive_source_ids=[str(d) for d in args.drive_source_id],
        )
        body = (
            sys.stdin.read()
            if str(args.body_file) == "-"
            else Path(str(args.body_file)).read_text(encoding="utf-8")
        )
        doc = self._knowledge.upsert_document(str(args.file), fm, body)
        print(json.dumps(doc.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0

    def _handle_init_drive(self, execute: bool) -> int:
        """Run Stage-2 init-drive Gate plan or Stage-3 taxonomy persistence."""
        if not self._settings.features.drive_knowledge_sync:
            return print_skipped_disabled("drive_knowledge_sync", "init-drive")
        if execute:
            doc = self._drive.execute_init_drive_taxonomy(self._knowledge)
            print(
                json.dumps(doc.frontmatter.model_dump(mode="json"), indent=2, ensure_ascii=False)
            )
            return 0
        _, plan_md = self._drive.init_drive()
        print(plan_md)
        return 0

    def _handle_search_drive(self, query: str) -> int:
        """Search the local 0-token Google Drive metadata index and print DriveEntry JSON."""
        hits = self._drive.search_index(query)
        print(json.dumps([h.model_dump(mode="json") for h in hits], indent=2, ensure_ascii=False))
        return 0

    def _handle_sync_drive(self) -> int:
        """Synchronize incremental Google Drive changes and print summary JSON."""
        if not self._settings.features.drive_knowledge_sync:
            return print_skipped_disabled("drive_knowledge_sync", "sync-drive")
        index = self._drive.sync_changes()
        summary = {
            "folder_count": index.folder_count,
            "file_count": index.file_count,
            "start_page_token": index.start_page_token,
            "last_synced_at": index.last_synced_at,
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    def _handle_read_drive(self, file_id: str) -> int:
        """Extract UTF-8 text from a Google Drive file natively (0-OCR first) and print it."""
        print(self._drive.read_file_text(file_id))
        return 0

    def _handle_poll_inbox(self, args: argparse.Namespace) -> int:
        """Poll newly arrived Gmail messages with optional live agy spawn and cognitive triage."""
        if not self._settings.features.inbox_auto_triage:
            return print_skipped_disabled("inbox_auto_triage", "poll-inbox")
        if args.spawn_agy or args.cognitive_agy:
            decisions = self._inbox.poll_new_messages(
                spawn_live_agy=True if args.spawn_agy else None,
                use_cognitive_agy=bool(args.cognitive_agy),
            )
        else:
            decisions = self._inbox.poll_new_messages()
        if decisions and self._agy is not None:
            self._agy.daemon_sync.sync_if_needed()
        out = [d.model_dump(mode="json") for d in decisions]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    def _handle_evolve(self, args: argparse.Namespace) -> int:
        """Generate Stage-2 evolution Gate plan or execute Stage-3 Mono-Repo PR creation."""
        if args.execute:
            wt = (
                Path(str(args.worktree))
                if args.worktree
                else self._evolve.create_isolated_worktree(str(args.id))
            )
            pr = self._evolve.execute_and_open_pr(
                proposal_id=str(args.id), title=str(args.title),
                body=str(args.description), worktree=wt,
            )
            print(json.dumps(pr.model_dump(mode="json"), indent=2, ensure_ascii=False))
            return 0
        _, plan_md = self._evolve.prepare_evolution_plan(
            str(args.id), str(args.title), str(args.description)
        )
        self._maybe_spawn_gate("alfred-evolve", f"evolve:{args.id}", plan_md, bool(args.spawn_agy))
        print(plan_md)
        return 0
