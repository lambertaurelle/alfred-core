"""CLI subcommand registration and handlers for Gmail MIME attachments and Drive transfer."""

from __future__ import annotations

import argparse
import json
from typing import Any

from alfred.controllers.calendar_cli import print_skipped_disabled
from alfred.domain.models import AlfredSettings
from alfred.services.gmail_drive_bridge_service import GmailDriveBridgeService

ATTACHMENT_COMMANDS = frozenset(
    {
        "list-attachments",
        "read-attachment",
        "save-attachment-to-drive",
    }
)


def register_attachment_parsers(sub: Any) -> None:
    """Register list-attachments, read-attachment, and save-attachment-to-drive parsers.

    Args:
        sub: Argparse subparsers collection attached to top-level alfred parser.
    """
    p_list = sub.add_parser("list-attachments")
    p_list.add_argument("--message-id", required=True)

    p_read = sub.add_parser("read-attachment")
    p_read.add_argument("--message-id", required=True)
    p_read.add_argument("--filename", "--attachment", dest="filename", default="")
    p_read.add_argument("--index", type=int, default=None)
    p_read.add_argument("--attachment-id", default="")
    p_read.add_argument("--max-chars", type=int, default=20000)

    p_save = sub.add_parser("save-attachment-to-drive")
    p_save.add_argument("--message-id", required=True)
    p_save.add_argument("--parent-id", "--folder-id", dest="parent_id", required=True)
    p_save.add_argument("--filename", "--attachment", dest="filename", default="")
    p_save.add_argument("--index", type=int, default=None)
    p_save.add_argument("--attachment-id", default="")
    p_save.add_argument("--name", "--rename", dest="target_name", default="")
    p_save.add_argument("--overwrite", action="store_true")


def dispatch_attachment_command(
    cmd: str,
    bridge: GmailDriveBridgeService | None,
    settings: AlfredSettings,
    args: argparse.Namespace,
) -> int:
    """Dispatch attachment listing, in-memory reading, or Drive transfer CLI command.

    Args:
        cmd: Subcommand name from ATTACHMENT_COMMANDS.
        bridge: Wired GmailDriveBridgeService instance.
        settings: Runtime settings checking drive_knowledge_sync feature gate.
        args: Parsed CLI namespace.

    Returns:
        POSIX exit status code (0 on success).
    """
    if cmd == "save-attachment-to-drive" and not (
        settings.features.drive_knowledge_sync or settings.features.drive_watcher
    ):
        return print_skipped_disabled("drive_knowledge_sync", "save-attachment-to-drive")
    if bridge is None:
        raise RuntimeError("GmailDriveBridgeService is not wired on CliController.")
    if cmd == "list-attachments":
        items = bridge.list_message_attachments(str(args.message_id))
        print(json.dumps([m.model_dump(mode="json") for m in items], indent=2, ensure_ascii=False))
        return 0
    if cmd == "read-attachment":
        content = bridge.read_message_attachment(
            str(args.message_id),
            filename=str(args.filename),
            index=args.index,
            attachment_id=str(args.attachment_id),
            max_chars=int(args.max_chars),
        )
        print(json.dumps(content.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0
    res = bridge.transfer_attachment_to_drive(
        str(args.message_id),
        parent_id=str(args.parent_id),
        filename=str(args.filename),
        index=args.index,
        attachment_id=str(args.attachment_id),
        target_name=str(args.target_name),
        overwrite=bool(args.overwrite),
    )
    print(json.dumps(res.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0
