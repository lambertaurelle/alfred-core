"""CLI subcommands for Family Office onboarding, delegation, signature vault, and MCP bridge."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from alfred.controllers.calendar_cli import print_skipped_disabled
from alfred.domain.feature_models import FeatureFlagsConfig, IdentityConfig, McpServerConfig
from alfred.domain.models import AlfredSettings
from alfred.domain.security_models import DelegationMode, SignaturePlacementRect
from alfred.services.agy_daemon_sync import DaemonSyncStatus
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.delegation_service import DelegationService
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService
from alfred.services.mcp_bridge_service import McpBridgeService
from alfred.services.onboarding_service import OnboardingService
from alfred.services.signature_vault_service import SignatureVaultService

ONBOARDING_AND_BRIDGE_COMMANDS = frozenset(
    {
        "status-onboarding",
        "oauth-guide",
        "apply-onboarding",
        "list-identities",
        "discover-drive",
        "repair-session-titles",
        "delegate",
        "prepare-signature",
        "apply-signature",
        "setup-mcp",
        "serve-mcp",
    }
)


def register_onboarding_and_bridge_parsers(sub: Any) -> None:
    """Register onboarding, delegation, handwritten signature, and MCP bridge subcommands.

    Args:
        sub: Argparse subparsers collection attached to top-level alfred parser.
    """
    p_onb = sub.add_parser("status-onboarding")
    p_onb.add_argument("--json", action="store_true")

    sub.add_parser("oauth-guide")

    p_id = sub.add_parser("list-identities")
    p_id.add_argument("--json", action="store_true")

    p_dd = sub.add_parser("discover-drive")
    p_dd.add_argument("--json", action="store_true")

    sub.add_parser("repair-session-titles")

    p_del = sub.add_parser("delegate")
    p_del.add_argument("--to", dest="target", required=True)
    p_del.add_argument("--title", required=True)
    p_del.add_argument("--brief", required=True)
    p_del.add_argument("--mode", default=DelegationMode.OPEN_GATE.value)
    p_del.add_argument("--parent-session", default="")
    p_del.add_argument("--source-workspace", default="alfred-life")
    p_del.add_argument("--source-message-id", default="")

    _register_apply_onboarding_parser(sub)
    _register_signature_and_mcp_parsers(sub)


def _register_apply_onboarding_parser(sub: Any) -> None:
    """Register apply-onboarding subcommand for modular features, identity, and MCP config."""
    p_app = sub.add_parser("apply-onboarding")
    p_app.add_argument("--enable-all", action="store_true")
    p_app.add_argument("--enable", action="append", default=[])
    p_app.add_argument("--disable", action="append", default=[])
    p_app.add_argument("--default-send-as", default=None)
    p_app.add_argument("--send-as-name", default=None)
    p_app.add_argument("--calendar-organizer", default=None)
    p_app.add_argument("--never-invite", action="append", default=[])
    p_app.add_argument("--mcp-host", default=None)
    p_app.add_argument("--mcp-port", type=int, default=None)
    p_app.add_argument("--mcp-public-url", default=None)


def _register_signature_and_mcp_parsers(sub: Any) -> None:
    """Register prepare-signature, apply-signature, setup-mcp, and serve-mcp subparsers."""
    p_psig = sub.add_parser("prepare-signature")
    p_psig.add_argument("--pdf", required=True)
    p_psig.add_argument("--reason", default="Document signature approved by user")
    p_psig.add_argument("--workspace", default="alfred-life")
    p_psig.add_argument("--page", type=int, default=0)
    p_psig.add_argument("--x", type=float, default=360.0)
    p_psig.add_argument("--y", type=float, default=72.0)
    p_psig.add_argument("--width", type=float, default=160.0)
    p_psig.add_argument("--height", type=float, default=56.0)

    p_asig = sub.add_parser("apply-signature")
    p_asig.add_argument("--request-id", required=True)
    p_asig.add_argument("--workspace", default="alfred-life")
    p_asig.add_argument("--transcript", default="")

    p_smcp = sub.add_parser("setup-mcp")
    p_smcp.add_argument("--pin", required=True)

    p_srv = sub.add_parser("serve-mcp")
    p_srv.add_argument("--host", default=None)
    p_srv.add_argument("--port", type=int, default=None)


def dispatch_onboarding_and_bridges(
    cmd: str,
    onboarding_service: OnboardingService,
    delegation_service: DelegationService,
    signature_service: SignatureVaultService,
    mcp_service: McpBridgeService,
    gmail_service: GmailService | None,
    drive_service: DriveService,
    agy_manager: AgySessionManager | None,
    settings: AlfredSettings,
    args: argparse.Namespace,
) -> int:
    """Dispatch onboarding, delegation, signature vault, and MCP bridge CLI commands.

    Args:
        cmd: Subcommand name from ONBOARDING_AND_BRIDGE_COMMANDS.
        onboarding_service: Wired OnboardingService instance.
        delegation_service: Wired DelegationService instance.
        signature_service: Wired SignatureVaultService instance.
        mcp_service: Wired McpBridgeService instance.
        gmail_service: Optional GmailService for identity listing and inbox sender checks.
        drive_service: Wired DriveService for Family Office discovery clustering.
        agy_manager: Optional AgySessionManager for title repair and daemon sync.
        settings: Runtime settings providing modular feature flags.
        args: Parsed CLI namespace.
    """
    if cmd == "oauth-guide":
        print(onboarding_service.render_oauth_guide())
        return 0
    if cmd == "serve-mcp":
        if not settings.features.mcp_gemini_bridge:
            return print_skipped_disabled("mcp_gemini_bridge", "serve-mcp")
        mcp_service.serve_forever(host=args.host, port=args.port)
        return 0
    if cmd in {"prepare-signature", "apply-signature"}:
        if not settings.features.signature_delegation:
            return print_skipped_disabled("signature_delegation", cmd)
        payload = _exec_signature_cmd(cmd, signature_service, args)
    elif cmd == "discover-drive":
        if not settings.features.drive_knowledge_sync:
            return print_skipped_disabled("drive_knowledge_sync", "discover-drive")
        payload = drive_service.build_family_office_discovery_map()
    elif cmd == "list-identities":
        if not settings.features.gmail_drafts_and_send and not settings.features.inbox_auto_triage:
            return print_skipped_disabled("gmail_drafts_and_send", "list-identities")
        payload = {
            "identities": gmail_service.list_send_as_identities() if gmail_service else []
        }
    elif cmd == "status-onboarding":
        payload = onboarding_service.inspect_live_status()
    elif cmd == "apply-onboarding":
        payload = _exec_apply_onboarding(onboarding_service, settings, args)
    elif cmd == "repair-session-titles":
        rep = agy_manager.repair_session_titles() if agy_manager is not None else []
        payload = {"repaired_session_ids": rep, "count": len(rep)}
    elif cmd == "delegate":
        payload = _exec_delegate(delegation_service, agy_manager, gmail_service, args)
    else:
        secret_path = mcp_service.setup_credentials(str(args.pin))
        payload = {"secret_path": str(secret_path), "status": "OK"}
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _exec_delegate(
    delegation_service: DelegationService,
    agy_manager: AgySessionManager | None,
    gmail_service: GmailService | None,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Execute cross-workspace delegation and flush daemon UI state when active."""
    norm_mode = str(args.mode).strip().lower().replace("_", "-")
    src_msg_id = str(args.source_message_id).strip()
    src_email = (
        gmail_service.get_message(src_msg_id) if src_msg_id and gmail_service else None
    )
    record = delegation_service.delegate(
        target=str(args.target), title=str(args.title), brief=str(args.brief),
        mode=norm_mode, parent_session_id=str(args.parent_session),
        source_workspace=str(args.source_workspace),
        source_email=src_email, source_message_id=src_msg_id,
    )
    if (
        record.status != "SKIPPED_DISABLED"
        and agy_manager is not None
        and agy_manager.daemon_sync.sync_if_needed().status == DaemonSyncStatus.DEFERRED_ACTIVE_TURN
    ):
        agy_manager.daemon_sync.spawn_deferred_sync_worker()
    return record.model_dump(mode="json")


def _exec_signature_cmd(
    cmd: str, signature_service: SignatureVaultService, args: argparse.Namespace
) -> dict[str, Any]:
    """Execute prepare-signature or apply-signature and return JSON payload."""
    if cmd == "prepare-signature":
        rect = SignaturePlacementRect(
            page_index=int(args.page), x=float(args.x), y=float(args.y),
            width=float(args.width), height=float(args.height),
        )
        ticket = signature_service.prepare_signature(
            source_pdf=Path(str(args.pdf)), rect=rect,
            reason=str(args.reason), workspace=str(args.workspace),
        )
        return ticket.model_dump(mode="json")
    transcript = Path(str(args.transcript)) if str(args.transcript).strip() else None
    signed = signature_service.apply_signature(
        request_id=str(args.request_id),
        workspace=str(args.workspace),
        transcript_path=transcript,
    )
    return {"signed_pdf_path": str(signed), "status": "SIGNED"}


def _exec_apply_onboarding(
    onboarding_service: OnboardingService,
    settings: AlfredSettings,
    args: argparse.Namespace,
) -> dict[str, Any]:
    """Compute updated FeatureFlagsConfig, IdentityConfig, and McpServerConfig and persist."""
    feat_data: dict[str, bool] = {k: bool(v) for k, v in settings.features.model_dump().items()}
    if bool(args.enable_all):
        feat_data = {k: True for k in feat_data}
    for raw_name in args.enable:
        name = str(raw_name).strip()
        if name not in feat_data:
            raise ValueError(f"Unknown feature flag in --enable: '{name}'")
        feat_data[name] = True
    for raw_name in args.disable:
        name = str(raw_name).strip()
        if name not in feat_data:
            raise ValueError(f"Unknown feature flag in --disable: '{name}'")
        feat_data[name] = False
    updated_features = FeatureFlagsConfig.from_dict(feat_data)
    updated_identity = _merge_identity_config(settings.identity, args)
    updated_mcp = _merge_mcp_config(settings.mcp, args)
    written_path = onboarding_service.apply_onboarding_settings(
        features=updated_features,
        identity=updated_identity,
        mcp=updated_mcp,
    )
    return {
        "settings_path": str(written_path),
        "enabled_features": updated_features.enabled_feature_names(),
        "oauth_scopes": updated_features.compute_oauth_scopes(),
        "identity": updated_identity.model_dump(mode="json"),
        "mcp": updated_mcp.model_dump(mode="json"),
        "status": "APPLIED",
    }


def _merge_mcp_config(
    current: McpServerConfig,
    args: argparse.Namespace,
) -> McpServerConfig:
    """Merge CLI MCP host, port, and public URL flags with existing McpServerConfig."""
    updates: dict[str, Any] = {}
    if args.mcp_host is not None:
        host = str(args.mcp_host).strip()
        if not host:
            raise ValueError("--mcp-host cannot be empty.")
        updates["host"] = host
    if args.mcp_port is not None:
        port = int(args.mcp_port)
        if not 1 <= port <= 65535:
            raise ValueError("--mcp-port must be between 1 and 65535.")
        updates["port"] = port
    if args.mcp_public_url is not None:
        updates["public_url"] = str(args.mcp_public_url).strip().rstrip("/")
    return current.model_copy(update=updates) if updates else current


def _merge_identity_config(
    current: IdentityConfig,
    args: argparse.Namespace,
) -> IdentityConfig:
    """Merge CLI identity flags with existing IdentityConfig values."""
    never_invite = list(current.never_invite_emails)
    if args.never_invite:
        seen = {email.strip().lower() for email in never_invite if email.strip()}
        for item in args.never_invite:
            for part in str(item).split(","):
                norm = part.strip().lower()
                if norm and norm not in seen:
                    seen.add(norm)
                    never_invite.append(norm)
    return IdentityConfig(
        default_send_as_email=(
            str(args.default_send_as).strip().lower()
            if args.default_send_as is not None
            else current.default_send_as_email
        ),
        default_send_as_display_name=(
            str(args.send_as_name).strip()
            if args.send_as_name is not None
            else current.default_send_as_display_name
        ),
        calendar_organizer_email=(
            str(args.calendar_organizer).strip().lower()
            if args.calendar_organizer is not None
            else current.calendar_organizer_email
        ),
        never_invite_emails=never_invite,
    )
