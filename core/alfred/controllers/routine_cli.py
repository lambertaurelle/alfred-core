"""CLI subcommand registration and handlers for periodic routines and read-only Gmail."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.routine_service import RoutineService


def register_routine_and_gmail_parsers(sub: Any) -> None:
    """Register periodic routine, archive backfill, and Stage-1 read-only Gmail CLI subcommands.

    Args:
        sub: Argparse subparsers collection attached to top-level alfred parser.
    """
    p_run = sub.add_parser("run-routines")
    p_run.add_argument("--only", choices=["all", "inbox", "periodic"], default="all")
    p_run.add_argument("--today", default="")

    p_inst = sub.add_parser("install-routines")
    p_inst.add_argument("--systemd-dir", default="")
    p_inst.add_argument("--no-enable", action="store_true")

    p_stat = sub.add_parser("status-routines")
    p_stat.add_argument("--systemd-dir", default="")

    sub.add_parser("backfill-inbox-archives")

    p_sgm = sub.add_parser("search-gmail")
    p_sgm.add_argument("--query", required=True)
    p_sgm.add_argument("--max-results", type=int, default=10)

    p_rth = sub.add_parser("read-thread")
    p_rth.add_argument("--thread-id", required=True)


def handle_backfill_inbox_archives(inbox_service: InboxTriageService) -> int:
    """Materialize real archived Antigravity conversations for synthetic agy_* silent records.

    Args:
        inbox_service: Wired InboxTriageService instance.

    Returns:
        POSIX exit status code (0 on success).
    """
    ids = inbox_service.backfill_silent_archives()
    payload = {"materialized_conversations": ids, "count": len(ids)}
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def handle_run_routines(routine_service: RoutineService | None, args: argparse.Namespace) -> int:
    """Execute periodic or inbox routines and print RoutineTickReport JSON.

    Args:
        routine_service: Wired RoutineService instance.
        args: Parsed CLI namespace containing --only and optional --today.

    Returns:
        POSIX exit status code (0 on success).
    """
    if routine_service is None:
        raise RuntimeError("RoutineService is not wired on CliController.")
    ref_day = date.fromisoformat(str(args.today)) if args.today else None
    report = routine_service.run_routines(scope=str(args.only), today=ref_day)
    print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0


def handle_install_routines(
    routine_service: RoutineService | None,
    args: argparse.Namespace,
) -> int:
    """Install systemd --user timer units and print written file paths as JSON.

    Args:
        routine_service: Wired RoutineService instance.
        args: Parsed CLI namespace containing optional --systemd-dir and --no-enable.

    Returns:
        POSIX exit status code (0 on success).
    """
    if routine_service is None:
        raise RuntimeError("RoutineService is not wired on CliController.")
    target_dir = Path(str(args.systemd_dir)) if args.systemd_dir else None
    written = routine_service.install_systemd_timers(
        systemd_user_dir=target_dir,
        enable_now=not bool(args.no_enable),
    )
    payload = {
        "written_units": [str(p) for p in written],
        "enabled": not bool(args.no_enable),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def handle_status_routines(
    routine_service: RoutineService | None,
    args: argparse.Namespace,
) -> int:
    """Print live status JSON for bookmarks, contract readiness, and systemd timers.

    Args:
        routine_service: Wired RoutineService instance.
        args: Parsed CLI namespace containing optional --systemd-dir.

    Returns:
        POSIX exit status code (0 on success).
    """
    if routine_service is None:
        raise RuntimeError("RoutineService is not wired on CliController.")
    target_dir = Path(str(args.systemd_dir)) if args.systemd_dir else None
    status = routine_service.get_status(systemd_user_dir=target_dir)
    print(json.dumps(status.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0


def handle_search_gmail(gmail_service: GmailService | None, args: argparse.Namespace) -> int:
    """Execute a read-only Gmail search query and print matching messages as JSON.

    Args:
        gmail_service: Wired GmailService instance.
        args: Parsed CLI namespace containing --query and --max-results.

    Returns:
        POSIX exit status code (0 on success).
    """
    if gmail_service is None:
        raise RuntimeError("GmailService is not wired on CliController.")
    hits = gmail_service.search_messages(str(args.query), max_results=int(args.max_results))
    print(json.dumps([m.model_dump(mode="json") for m in hits], indent=2, ensure_ascii=False))
    return 0


def handle_read_thread(gmail_service: GmailService | None, args: argparse.Namespace) -> int:
    """Fetch all messages in a Gmail thread read-only and print JSON array.

    Args:
        gmail_service: Wired GmailService instance.
        args: Parsed CLI namespace containing --thread-id.

    Returns:
        POSIX exit status code (0 on success).
    """
    if gmail_service is None:
        raise RuntimeError("GmailService is not wired on CliController.")
    msgs = gmail_service.get_thread_messages(str(args.thread_id))
    print(json.dumps([m.model_dump(mode="json") for m in msgs], indent=2, ensure_ascii=False))
    return 0
