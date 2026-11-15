"""CLI subcommand registration and dispatcher for Google Tasks, Contacts, and Workspace Suite."""

from __future__ import annotations

import argparse
import json
from typing import Any

from alfred.controllers.calendar_cli import print_skipped_disabled
from alfred.domain.models import AlfredSettings
from alfred.services.google_workspace_suite import GoogleWorkspaceSuiteService

TASK_COMMANDS = frozenset(
    {
        "list-tasklists",
        "list-tasks",
        "create-task",
        "complete-task",
        "update-task",
        "delete-task",
    }
)
CONTACT_COMMANDS = frozenset({"search-contacts", "get-contact", "create-contact"})
SUITE_COMMANDS = frozenset({"create-doc", "read-sheet", "append-sheet"})


def register_tasks_contacts_suite_parsers(sub: Any) -> None:
    """Register Google Tasks, Google Contacts, and opt-in Workspace Suite CLI subcommands.

    Args:
        sub: Argparse subparsers collection attached to top-level alfred parser.
    """
    p_tls = sub.add_parser("list-tasklists")
    p_tls.add_argument("--max-results", type=int, default=100)

    p_lt = sub.add_parser("list-tasks")
    p_lt.add_argument("--tasklist", default=None)
    p_lt.add_argument("--include-completed", action="store_true")
    p_lt.add_argument("--max-results", type=int, default=100)

    p_ct = sub.add_parser("create-task")
    p_ct.add_argument("--title", required=True)
    p_ct.add_argument("--notes", default="")
    p_ct.add_argument("--due", dest="due_iso", default="")
    p_ct.add_argument("--tasklist", default=None)
    p_ct.add_argument("--parent", dest="parent_id", default="")

    p_comp = sub.add_parser("complete-task")
    p_comp.add_argument("--task-id", required=True)
    p_comp.add_argument("--tasklist", default=None)

    p_ut = sub.add_parser("update-task")
    p_ut.add_argument("--task-id", required=True)
    p_ut.add_argument("--title", default=None)
    p_ut.add_argument("--notes", default=None)
    p_ut.add_argument("--due", dest="due_iso", default=None)
    p_ut.add_argument("--status", default=None)
    p_ut.add_argument("--tasklist", default=None)

    p_dt = sub.add_parser("delete-task")
    p_dt.add_argument("--task-id", required=True)
    p_dt.add_argument("--tasklist", default=None)

    _register_contacts_and_suite_parsers(sub)


def _register_contacts_and_suite_parsers(sub: Any) -> None:
    """Register Google Contacts and Google Docs/Sheets CLI subparsers."""
    p_sc = sub.add_parser("search-contacts")
    p_sc.add_argument("--query", required=True)
    p_sc.add_argument("--page-size", type=int, default=10)
    p_sc.add_argument("--no-other-contacts", action="store_true")

    p_gc = sub.add_parser("get-contact")
    p_gc.add_argument("--resource-name", required=True)

    p_cc = sub.add_parser("create-contact")
    p_cc.add_argument("--given-name", required=True)
    p_cc.add_argument("--family-name", default="")
    p_cc.add_argument("--email", default="")
    p_cc.add_argument("--phone", default="")
    p_cc.add_argument("--organization", default="")

    p_doc = sub.add_parser("create-doc")
    p_doc.add_argument("--title", required=True)
    p_doc.add_argument("--text", default="")

    p_rsh = sub.add_parser("read-sheet")
    p_rsh.add_argument("--spreadsheet-id", required=True)
    p_rsh.add_argument("--range", dest="range_a1", required=True)

    p_ash = sub.add_parser("append-sheet")
    p_ash.add_argument("--spreadsheet-id", required=True)
    p_ash.add_argument("--range", dest="range_a1", required=True)
    p_ash.add_argument("--value", action="append", default=[])


def dispatch_tasks_contacts_suite(
    cmd: str,
    tasks_service: Any,
    contacts_service: Any,
    suite_service: GoogleWorkspaceSuiteService | None,
    settings: AlfredSettings,
    args: argparse.Namespace,
) -> int:
    """Dispatch Google Tasks, Contacts, or Suite CLI commands with feature-flag gating.

    Args:
        cmd: Subcommand name from TASK_COMMANDS, CONTACT_COMMANDS, or SUITE_COMMANDS.
        tasks_service: Wired TasksService or duck-typed double.
        contacts_service: Wired ContactsService or duck-typed double.
        suite_service: Optional GoogleWorkspaceSuiteService instance.
        settings: Runtime settings providing modular feature flags.
        args: Parsed CLI namespace.
    """
    if cmd in TASK_COMMANDS:
        if not settings.features.tasks_management:
            return print_skipped_disabled("tasks_management", cmd)
        payload = _exec_task_cmd(cmd, tasks_service, args)
    elif cmd in CONTACT_COMMANDS:
        if not settings.features.contacts_lookup:
            return print_skipped_disabled("contacts_lookup", cmd)
        payload = _exec_contact_cmd(cmd, contacts_service, args)
    else:
        if not settings.features.google_docs_suite or suite_service is None:
            return print_skipped_disabled("google_docs_suite", cmd)
        payload = _exec_suite_cmd(cmd, suite_service, args)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _exec_task_cmd(cmd: str, svc: Any, args: argparse.Namespace) -> Any:
    """Execute a Google Tasks subcommand and return JSON-serializable data."""
    if cmd == "list-tasklists":
        return [it.model_dump(mode="json") for it in svc.list_tasklists(int(args.max_results))]
    if cmd == "list-tasks":
        items = svc.list_tasks(
            tasklist_id=args.tasklist,
            include_completed=bool(args.include_completed),
            max_results=int(args.max_results),
        )
        return [it.model_dump(mode="json") for it in items]
    if cmd == "create-task":
        item = svc.create_task(
            title=str(args.title), notes=str(args.notes), due_iso=str(args.due_iso),
            tasklist_id=args.tasklist, parent_id=str(args.parent_id),
        )
        return item.model_dump(mode="json")
    if cmd == "complete-task":
        return svc.complete_task(
            task_id=str(args.task_id), tasklist_id=args.tasklist
        ).model_dump(mode="json")
    if cmd == "update-task":
        return svc.update_task(
            task_id=str(args.task_id), title=args.title, notes=args.notes,
            due_iso=args.due_iso, status=args.status, tasklist_id=args.tasklist,
        ).model_dump(mode="json")
    svc.delete_task(task_id=str(args.task_id), tasklist_id=args.tasklist)
    return {"deleted_task_id": str(args.task_id)}


def _exec_contact_cmd(cmd: str, svc: Any, args: argparse.Namespace) -> Any:
    """Execute a Google Contacts subcommand and return JSON-serializable data."""
    if cmd == "search-contacts":
        hits = svc.search_contacts(
            query=str(args.query),
            page_size=int(args.page_size),
            include_other_contacts=not bool(args.no_other_contacts),
        )
        return [c.model_dump(mode="json") for c in hits]
    if cmd == "get-contact":
        return svc.get_contact(str(args.resource_name)).model_dump(mode="json")
    return svc.create_contact(
        given_name=str(args.given_name), family_name=str(args.family_name),
        email=str(args.email), phone=str(args.phone), organization=str(args.organization),
    ).model_dump(mode="json")


def _exec_suite_cmd(
    cmd: str, svc: GoogleWorkspaceSuiteService, args: argparse.Namespace
) -> Any:
    """Execute a Google Docs / Sheets subcommand and return JSON-serializable data."""
    if cmd == "create-doc":
        doc_id = svc.create_doc(title=str(args.title), initial_text=str(args.text))
        return {"document_id": doc_id, "title": str(args.title)}
    if cmd == "read-sheet":
        return {"values": svc.read_sheet_values(str(args.spreadsheet_id), str(args.range_a1))}
    return svc.append_sheet_row(
        str(args.spreadsheet_id), str(args.range_a1), [str(v) for v in args.value]
    )
