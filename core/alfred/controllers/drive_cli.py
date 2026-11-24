"""CLI subcommands for non-destructive Google Drive governance and 2-speed watcher."""

from __future__ import annotations

import argparse
import json
from typing import Any

from alfred.controllers.calendar_cli import print_skipped_disabled
from alfred.domain.models import AlfredSettings
from alfred.services.agy_daemon_sync import DaemonSyncStatus
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_governance_service import DriveGovernanceService
from alfred.services.drive_watcher_service import DriveWatcherService

DRIVE_GOVERNANCE_COMMANDS = frozenset(
    {
        "watch-drive",
        "move-drive",
        "rename-drive",
        "create-drive-folder",
        "create-drive-shortcut",
    }
)


def register_drive_governance_parsers(sub: Any) -> None:
    """Register watch-drive and non-destructive Drive governance CLI subcommands.

    Args:
        sub: Argparse subparsers collection attached to top-level alfred parser.
    """
    p_watch = sub.add_parser("watch-drive")
    p_watch.add_argument("--spawn-agy", action="store_true")
    p_watch.add_argument("--cognitive-agy", action="store_true")

    p_move = sub.add_parser("move-drive")
    p_move.add_argument("--file-id", required=True)
    p_move.add_argument("--target-parent-id", default="")
    p_move.add_argument("--parent-id", default="")

    p_ren = sub.add_parser("rename-drive")
    p_ren.add_argument("--file-id", required=True)
    p_ren.add_argument("--name", required=True)

    p_fld = sub.add_parser("create-drive-folder")
    p_fld.add_argument("--name", required=True)
    p_fld.add_argument("--parent-id", default="")

    p_sc = sub.add_parser("create-drive-shortcut")
    p_sc.add_argument("--target-id", required=True)
    p_sc.add_argument("--parent-id", required=True)
    p_sc.add_argument("--name", default="")


def dispatch_drive_governance_command(
    cmd: str,
    governance_service: DriveGovernanceService,
    watcher_service: DriveWatcherService,
    agy_manager: AgySessionManager | None,
    settings: AlfredSettings,
    args: argparse.Namespace,
) -> int:
    """Dispatch watch-drive and non-destructive Drive governance CLI commands.

    Args:
        cmd: Subcommand identifier from DRIVE_GOVERNANCE_COMMANDS.
        governance_service: Wired DriveGovernanceService instance.
        watcher_service: Wired DriveWatcherService instance.
        agy_manager: Optional AgySessionManager for Antigravity UI reconciliation.
        settings: Runtime settings providing modular feature gates.
        args: Parsed CLI namespace.

    Returns:
        POSIX exit status code (0 on success).
    """
    if cmd == "watch-drive":
        return _handle_watch_drive(watcher_service, agy_manager, settings, args)

    if not settings.features.drive_knowledge_sync:
        return print_skipped_disabled("drive_knowledge_sync", cmd)

    if cmd == "move-drive":
        dest_parent = str(args.target_parent_id or args.parent_id).strip()
        if not dest_parent:
            raise ValueError("Specify --target-parent-id or --parent-id for move-drive.")
        result = governance_service.move_file(str(args.file_id), dest_parent)
    elif cmd == "rename-drive":
        result = governance_service.rename_file(str(args.file_id), str(args.name))
    elif cmd == "create-drive-folder":
        result = governance_service.create_folder(str(args.name), parent_id=str(args.parent_id))
    else:
        result = governance_service.create_shortcut(
            target_file_id=str(args.target_id),
            parent_id=str(args.parent_id),
            name=str(args.name),
        )
    print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    return 0


def _handle_watch_drive(
    watcher_service: DriveWatcherService,
    agy_manager: AgySessionManager | None,
    settings: AlfredSettings,
    args: argparse.Namespace,
) -> int:
    """Execute 2-speed Google Drive watcher tick when features.drive_watcher is enabled."""
    if not settings.features.drive_watcher:
        return print_skipped_disabled("drive_watcher", "watch-drive")
    decisions = watcher_service.poll_and_process(
        spawn_live_agy=True if bool(args.spawn_agy) else None,
        use_cognitive_agy=bool(args.cognitive_agy),
    )
    if (
        decisions
        and agy_manager is not None
        and agy_manager.daemon_sync.sync_if_needed().status == DaemonSyncStatus.DEFERRED_ACTIVE_TURN
    ):
        agy_manager.daemon_sync.spawn_deferred_sync_worker()
    print(json.dumps([d.model_dump(mode="json") for d in decisions], indent=2, ensure_ascii=False))
    return 0
