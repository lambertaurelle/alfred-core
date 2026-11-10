"""Command-line controller exposing Alfred V7.1 Lean Stage-1/2/3 workflows."""

from __future__ import annotations

import argparse
import calendar as pycal
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from alfred.domain.models import NounouContract, VacationPeriod
from alfred.repositories.state_store import StateStore
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.evolve_worktree_service import EvolveWorktreeService
from alfred.services.gmail_service import GmailService
from alfred.services.google_auth import GoogleOAuthService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.nounou_service import NounouService
from alfred.services.pajemploi_calculator import PajemploiCalculator


class CliController:
    """Dispatch CLI subcommands across Auth, Drive, Knowledge, Inbox, Nounou, and Evolve."""

    def __init__(
        self,
        knowledge_service: KnowledgeService,
        drive_service: DriveService,
        inbox_service: InboxTriageService,
        nounou_service: NounouService,
        pajemploi_calculator: PajemploiCalculator,
        calendar_service: CalendarService,
        evolve_service: EvolveWorktreeService,
        state_store: StateStore,
        gmail_service: GmailService | None = None,
        oauth_service: GoogleOAuthService | None = None,
        timezone: str = "Europe/Paris",
    ) -> None:
        """Initialize the CLI controller with injected domain services.

        Args:
            knowledge_service: 3-tier YAML frontmatter knowledge service.
            drive_service: Google Drive scaffolder and native reader service.
            inbox_service: New-email triage service with 3 exclusive outputs.
            nounou_service: Childcare planning and vacation notice service.
            pajemploi_calculator: Deterministic monthly Pajemploi calculator.
            calendar_service: Google Calendar v3 connector.
            evolve_service: Isolated git worktree and Dual-Repo PR service.
            state_store: Local atomic state facade.
            gmail_service: Optional Gmail API connector for post-Gate draft actions.
            oauth_service: Optional GoogleOAuthService for interactive OAuth2 bootstrap.
            timezone: Configured local timezone string (default 'Europe/Paris').
        """
        self._knowledge = knowledge_service
        self._drive = drive_service
        self._inbox = inbox_service
        self._nounou = nounou_service
        self._pajemploi = pajemploi_calculator
        self._calendar = calendar_service
        self._evolve = evolve_service
        self._store = state_store
        self._gmail = gmail_service
        self._oauth = oauth_service
        self._timezone = timezone

    def run(self, argv: list[str]) -> int:
        """Parse CLI arguments and execute the requested Alfred workflow.

        Args:
            argv: Command-line argument list excluding program name.

        Returns:
            POSIX exit status code (0 on success).
        """
        self._knowledge.bootstrap_templates_if_empty()
        args = self._build_parser().parse_args(argv)
        cmd = str(args.command)

        if cmd == "auth-google":
            return self._handle_auth_google(int(args.port), not bool(args.no_browser))
        if cmd == "scan-knowledge":
            return self._handle_scan_knowledge(str(args.query))
        if cmd == "init-drive":
            return self._handle_init_drive(bool(args.execute))
        if cmd == "poll-inbox":
            decisions = self._inbox.poll_new_messages()
            print(json.dumps([d.model_dump(mode="json") for d in decisions], indent=2))
            return 0
        if cmd == "send-draft":
            return self._handle_send_draft(args)
        if cmd == "init-nounou":
            return self._handle_init_nounou(Path(args.contract_json), bool(args.execute))
        if cmd == "seed-nounou":
            ref_dt = date.fromisoformat(args.date) if args.date else date.today()
            _, plan_md = self._nounou.seed_next_month_schedule(ref_dt, bool(args.apply))
            print(plan_md)
            return 0
        if cmd == "check-vacation":
            alert = self._nounou.evaluate_vacation_notice(
                VacationPeriod(start_date=args.start, end_date=args.end, source=args.source),
                reference_date=date.today(),
                persist=bool(args.persist),
            )
            print(alert.implementation_plan_markdown)
            return 0
        if cmd == "close-pajemploi":
            today = date.today()
            yr = int(args.year) if args.year else today.year
            mo = int(args.month) if args.month else today.month
            return self._handle_close_pajemploi(yr, mo)
        if cmd == "evolve":
            return self._handle_evolve(args)
        return 1

    def _build_parser(self) -> argparse.ArgumentParser:
        """Construct the top-level argparse command parser."""
        parser = argparse.ArgumentParser(prog="alfred", description="Alfred V7.1 Lean CLI")
        sub = parser.add_subparsers(dest="command", required=True)

        p_auth = sub.add_parser("auth-google")
        p_auth.add_argument("--port", type=int, default=0)
        p_auth.add_argument("--no-browser", action="store_true")

        p_know = sub.add_parser("scan-knowledge")
        p_know.add_argument("--query", default="")

        p_drv = sub.add_parser("init-drive")
        p_drv.add_argument("--execute", action="store_true")

        sub.add_parser("poll-inbox")

        p_dr = sub.add_parser("send-draft")
        p_dr.add_argument("--draft-id", required=True)
        p_dr.add_argument("--thread-id", default="")
        p_dr.add_argument("--to", default="")
        p_dr.add_argument("--subject", default="")
        p_dr.add_argument("--body", default="")

        p_init_n = sub.add_parser("init-nounou")
        p_init_n.add_argument("--contract-json", required=True)
        p_init_n.add_argument("--execute", action="store_true")

        p_seed = sub.add_parser("seed-nounou")
        p_seed.add_argument("--date", default="")
        p_seed.add_argument("--apply", action="store_true")

        p_vac = sub.add_parser("check-vacation")
        p_vac.add_argument("--start", required=True)
        p_vac.add_argument("--end", required=True)
        p_vac.add_argument("--source", default="parents")
        p_vac.add_argument("--persist", action="store_true")

        p_paj = sub.add_parser("close-pajemploi")
        p_paj.add_argument("--year", type=int, default=0)
        p_paj.add_argument("--month", type=int, default=0)

        p_ev = sub.add_parser("evolve")
        p_ev.add_argument("--id", required=True)
        p_ev.add_argument("--title", required=True)
        p_ev.add_argument("--description", required=True)
        p_ev.add_argument("--execute", action="store_true")
        p_ev.add_argument("--core-worktree", default="")
        p_ev.add_argument("--instance-worktree", default="")
        return parser

    def _handle_auth_google(self, port: int, open_browser: bool) -> int:
        """Execute interactive OAuth2 InstalledAppFlow consent and write token.json."""
        if self._oauth is None:
            raise RuntimeError("GoogleOAuthService is not wired on CliController.")
        token_path = self._oauth.bootstrap_interactive_token(port=port, open_browser=open_browser)
        print(json.dumps({"token_path": str(token_path), "status": "OK"}, indent=2))
        return 0

    def _handle_init_drive(self, execute: bool) -> int:
        """Run Stage-2 init-drive Gate plan or Stage-3 taxonomy persistence."""
        if execute:
            doc = self._drive.execute_init_drive_taxonomy(self._knowledge)
            print(json.dumps(doc.frontmatter.model_dump(mode="json"), indent=2, ensure_ascii=False))
            return 0
        _, plan_md = self._drive.init_drive()
        print(plan_md)
        return 0

    def _handle_send_draft(self, args: argparse.Namespace) -> int:
        """Update (optional) and send a Gmail draft post-Gate approval."""
        if self._gmail is None:
            raise RuntimeError("GmailService is not wired on CliController.")
        draft_id = str(args.draft_id)
        if args.body:
            draft_id = self._gmail.update_draft(
                draft_id=draft_id,
                thread_id=str(args.thread_id),
                to_addr=str(args.to),
                subject=str(args.subject),
                body_text=str(args.body),
            )
        sent_id = self._gmail.send_draft(draft_id)
        print(json.dumps({"draft_id": draft_id, "sent_message_id": sent_id}, indent=2))
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

    def _handle_init_nounou(self, contract_json_path: Path, execute: bool) -> int:
        """Generate Gate plan or execute Garde Nounou initialization from contract JSON."""
        raw = json.loads(contract_json_path.read_text(encoding="utf-8"))
        contract = NounouContract.model_validate(raw)
        if execute:
            saved = self._nounou.execute_init_nounou(contract)
            print(json.dumps(saved.model_dump(mode="json"), indent=2, ensure_ascii=False))
            return 0
        print(self._nounou.prepare_init_nounou_plan(contract))
        return 0

    def _handle_close_pajemploi(self, year: int, month: int) -> int:
        """Fetch Garde Nounou events for (year, month) in local TZ and print Pajemploi plan."""
        contract = self._store.nounou_contract.load()
        _, last_day = pycal.monthrange(year, month)
        tz = ZoneInfo(self._timezone)
        t_min = datetime(year, month, 1, 0, 0, 0, tzinfo=tz).isoformat()
        t_max = datetime(year, month, last_day, 23, 59, 59, tzinfo=tz).isoformat()
        events = (
            self._calendar.list_events(contract.calendar_id, t_min, t_max)
            if contract.calendar_id
            else []
        )
        report = self._pajemploi.calculate_monthly_report(year, month, contract, events)
        print(report.implementation_plan_markdown)
        return 0

    def _handle_evolve(self, args: argparse.Namespace) -> int:
        """Generate Stage-2 evolution Gate plan or execute Stage-3 Dual-Repo PR creation."""
        if args.execute:
            if args.core_worktree and args.instance_worktree:
                core_wt = Path(args.core_worktree)
                inst_wt = Path(args.instance_worktree)
            else:
                core_wt, inst_wt = self._evolve.create_isolated_worktrees(str(args.id))
            prs = self._evolve.execute_and_open_dual_prs(
                proposal_id=str(args.id),
                title=str(args.title),
                body=str(args.description),
                core_worktree=core_wt,
                instance_worktree=inst_wt,
            )
            print(json.dumps([p.model_dump(mode="json") for p in prs], indent=2))
            return 0
        _, plan_md = self._evolve.prepare_evolution_plan(args.id, args.title, args.description)
        print(plan_md)
        return 0
