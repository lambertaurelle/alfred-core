"""Command-line controller exposing Alfred V7.1 Lean Stage-1/2/3 workflows."""

from __future__ import annotations

import argparse
import calendar as pycal
import json
import sys
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alfred.domain.models import (
    AlfredSettings,
    FrontmatterSchema,
    NounouContract,
    VacationPeriod,
)
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
        settings: AlfredSettings | None = None,
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
            settings: Optional AlfredSettings instance for calendar and runtime config.
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
        self._settings = settings or AlfredSettings(timezone=timezone)
        self._timezone = settings.timezone if settings is not None else timezone

    def run(self, argv: list[str]) -> int:
        """Parse CLI arguments and execute the requested Alfred workflow.

        Args:
            argv: Command-line argument list excluding program name.

        Returns:
            POSIX exit status code (0 on success).
        """
        args = self._build_parser().parse_args(argv)
        self._knowledge.bootstrap_templates_if_empty()
        handlers: dict[str, Callable[[argparse.Namespace], int]] = {
            "auth-google": lambda a: self._handle_auth_google(int(a.port), not bool(a.no_browser)),
            "scan-knowledge": lambda a: self._handle_scan_knowledge(str(a.query)),
            "load-knowledge": lambda a: self._handle_load_knowledge(str(a.doc)),
            "upsert-knowledge": self._handle_upsert_knowledge,
            "init-drive": lambda a: self._handle_init_drive(bool(a.execute)),
            "search-drive": lambda a: self._handle_search_drive(str(a.query)),
            "sync-drive": lambda _a: self._handle_sync_drive(),
            "read-drive": lambda a: self._handle_read_drive(str(a.file_id)),
            "poll-inbox": self._handle_poll_inbox,
            "send-draft": self._handle_send_draft,
            "list-events": self._handle_list_events,
            "init-nounou": lambda a: self._handle_init_nounou(
                Path(a.contract_json), bool(a.execute)
            ),
            "seed-nounou": self._handle_seed_nounou,
            "check-vacation": self._handle_check_vacation,
            "close-pajemploi": self._handle_close_pajemploi_cmd,
            "evolve": self._handle_evolve,
        }
        handler = handlers.get(str(args.command))
        return handler(args) if handler is not None else 1

    def _build_parser(self) -> argparse.ArgumentParser:
        """Construct the top-level argparse command parser."""
        parser = argparse.ArgumentParser(prog="alfred", description="Alfred V7.1 Lean CLI")
        sub = parser.add_subparsers(dest="command", required=True)

        p_auth = sub.add_parser("auth-google")
        p_auth.add_argument("--port", type=int, default=0)
        p_auth.add_argument("--no-browser", action="store_true")

        self._register_knowledge_and_drive_parsers(sub)
        self._register_inbox_and_calendar_parsers(sub)
        self._register_nounou_and_evolve_parsers(sub)
        return parser

    def _register_knowledge_and_drive_parsers(self, sub: Any) -> None:
        """Register Tier-1/2/3 knowledge and Google Drive subcommands."""
        p_know = sub.add_parser("scan-knowledge")
        p_know.add_argument("--query", default="")

        p_load = sub.add_parser("load-knowledge")
        p_load.add_argument("--doc", required=True)

        p_ups = sub.add_parser("upsert-knowledge")
        p_ups.add_argument("--file", required=True)
        p_ups.add_argument("--id", required=True)
        p_ups.add_argument("--title", required=True)
        p_ups.add_argument("--domain", required=True)
        p_ups.add_argument("--summary", required=True)
        p_ups.add_argument("--keyword", action="append", default=[])
        p_ups.add_argument("--drive-source-id", action="append", default=[])
        p_ups.add_argument("--body-file", required=True)

        p_drv = sub.add_parser("init-drive")
        p_drv.add_argument("--execute", action="store_true")

        p_sdrv = sub.add_parser("search-drive")
        p_sdrv.add_argument("--query", required=True)

        sub.add_parser("sync-drive")

        p_rdrv = sub.add_parser("read-drive")
        p_rdrv.add_argument("--file-id", required=True)

    def _register_inbox_and_calendar_parsers(self, sub: Any) -> None:
        """Register Gmail inbox triage, draft sending, and calendar listing subcommands."""
        p_poll = sub.add_parser("poll-inbox")
        p_poll.add_argument("--spawn-agy", action="store_true")
        p_poll.add_argument("--cognitive-agy", action="store_true")

        p_dr = sub.add_parser("send-draft")
        p_dr.add_argument("--draft-id", required=True)
        p_dr.add_argument("--thread-id", default="")
        p_dr.add_argument("--to", default="")
        p_dr.add_argument("--subject", default="")
        p_dr.add_argument("--body", default="")

        p_ev_list = sub.add_parser("list-events")
        p_ev_list.add_argument("--calendar", default="primary")
        p_ev_list.add_argument("--from", dest="from_iso", required=True)
        p_ev_list.add_argument("--to", dest="to_iso", required=True)

    def _register_nounou_and_evolve_parsers(self, sub: Any) -> None:
        """Register Garde Nounou childcare, Pajemploi, and Dual-Repo evolve subcommands."""
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

    def _handle_auth_google(self, port: int, open_browser: bool) -> int:
        """Execute interactive OAuth2 InstalledAppFlow consent and write token.json."""
        if self._oauth is None:
            raise RuntimeError("GoogleOAuthService is not wired on CliController.")
        token_path = self._oauth.bootstrap_interactive_token(port=port, open_browser=open_browser)
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
            id=str(args.id),
            title=str(args.title),
            domain=str(args.domain),
            summary=str(args.summary),
            keywords=[str(k) for k in args.keyword],
            drive_source_ids=[str(d) for d in args.drive_source_id],
        )
        body = (
            sys.stdin.read()
            if str(args.body_file) == "-"
            else Path(args.body_file).read_text(encoding="utf-8")
        )
        doc = self._knowledge.upsert_document(str(args.file), fm, body)
        print(json.dumps(doc.model_dump(mode="json"), indent=2, ensure_ascii=False))
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

    def _handle_search_drive(self, query: str) -> int:
        """Search the local 0-token Google Drive metadata index and print DriveEntry JSON."""
        hits = self._drive.search_index(query)
        print(json.dumps([h.model_dump(mode="json") for h in hits], indent=2, ensure_ascii=False))
        return 0

    def _handle_sync_drive(self) -> int:
        """Synchronize incremental Google Drive changes and print summary JSON."""
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
        if args.spawn_agy or args.cognitive_agy:
            decisions = self._inbox.poll_new_messages(
                spawn_live_agy=True if args.spawn_agy else None,
                use_cognitive_agy=bool(args.cognitive_agy),
            )
        else:
            decisions = self._inbox.poll_new_messages()
        payload = [d.model_dump(mode="json") for d in decisions]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
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

    def _normalize_calendar_bound(self, raw_iso: str, is_end: bool) -> tuple[str, datetime]:
        """Normalize an ISO date or timestamp into an RFC3339 string and aware datetime."""
        text = raw_iso.strip()
        tz = ZoneInfo(self._timezone)
        if "T" not in text and " " not in text:
            day = date.fromisoformat(text)
            hour, minute, second = (23, 59, 59) if is_end else (0, 0, 0)
            aware = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=tz)
            return aware.isoformat(), aware
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            aware = parsed.replace(tzinfo=tz)
            return aware.isoformat(), aware
        return text, parsed

    def _handle_list_events(self, args: argparse.Namespace) -> int:
        """Resolve primary/nounou/explicit calendar ID and print listed events as JSON."""
        cal_spec = str(args.calendar)
        if cal_spec == "primary":
            cal_id = self._settings.primary_calendar_id
        elif cal_spec == "nounou":
            contract_cal_id = (
                self._store.nounou_contract.load().calendar_id
                if self._store.nounou_contract.exists()
                else ""
            )
            cal_id = contract_cal_id or (self._store.bookmarks.load().nounou_calendar_id or "")
            if not cal_id:
                raise ValueError(
                    "Garde Nounou calendar is not initialized; run 'alfred init-nounou --execute'."
                )
        else:
            cal_id = cal_spec
        t_min, start_dt = self._normalize_calendar_bound(str(args.from_iso), is_end=False)
        t_max, end_dt = self._normalize_calendar_bound(str(args.to_iso), is_end=True)
        if end_dt < start_dt:
            raise ValueError(
                f"Calendar --to '{args.to_iso}' precedes --from '{args.from_iso}'."
            )
        events = self._calendar.list_events(cal_id, t_min, t_max)
        print(json.dumps([e.model_dump(mode="json") for e in events], indent=2, ensure_ascii=False))
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

    def _handle_seed_nounou(self, args: argparse.Namespace) -> int:
        """Seed or preview M+1 Garde Nounou schedule."""
        ref_dt = date.fromisoformat(args.date) if args.date else date.today()
        _, plan_md = self._nounou.seed_next_month_schedule(ref_dt, bool(args.apply))
        print(plan_md)
        return 0

    def _handle_check_vacation(self, args: argparse.Namespace) -> int:
        """Evaluate ~1-month contractual vacation notice compliance."""
        alert = self._nounou.evaluate_vacation_notice(
            VacationPeriod(start_date=args.start, end_date=args.end, source=args.source),
            reference_date=date.today(),
            persist=bool(args.persist),
        )
        print(alert.implementation_plan_markdown)
        return 0

    def _handle_close_pajemploi_cmd(self, args: argparse.Namespace) -> int:
        """Parse optional year/month flags and print deterministic Pajemploi closing plan."""
        today = date.today()
        yr = int(args.year) if args.year else today.year
        mo = int(args.month) if args.month else today.month
        return self._handle_close_pajemploi(yr, mo)

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
