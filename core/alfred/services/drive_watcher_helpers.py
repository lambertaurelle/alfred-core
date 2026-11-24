"""Cognitive prompt/schema helpers and Markdown plan/trace renderers for DriveWatcherService."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from alfred.domain.drive_watcher_models import (
    DriveDeltaItem,
    DriveGovernanceActionType,
    DriveGovernanceOp,
    DriveMutationResult,
    DriveWatcherOutputType,
    SanitizedDriveTextResult,
)
from alfred.domain.models import AlfredSettings, KnowledgeDocument
from alfred.services.agy_archive_store import (
    extract_conversation_id,
    is_error_envelope,
    parse_json_object,
    unwrap_triage_payload,
)
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_security_sanitizer import DriveSecuritySanitizer

DRIVE_WATCHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "output_type": {
            "type": "string",
            "enum": ["SILENT_NO_ACTION", "SILENT_AUTO_FILED", "GATE_REQUIRED"],
        },
        "reason": {"type": "string"},
        "operations": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["output_type", "reason", "operations"],
}


def classify_drive_batch_with_agy(
    agy: AgySessionManager,
    settings: AlfredSettings,
    sanitizer: DriveSecuritySanitizer,
    items: Sequence[DriveDeltaItem],
    sanitized_map: Mapping[str, SanitizedDriveTextResult],
    docs: Sequence[KnowledgeDocument],
) -> tuple[DriveWatcherOutputType, str, list[DriveGovernanceOp]]:
    """Classify a batch of untrusted Drive items via agy CLI inside workspace alfred-drive.

    Args:
        agy: Antigravity session manager providing binary resolution and model flags.
        settings: Structural runtime settings.
        sanitizer: DriveSecuritySanitizer wrapping untrusted items in nonce tags.
        items: Eligible DriveDeltaItem batch.
        sanitized_map: Sanitized text/metadata keyed by file_id.
        docs: Loaded Tier-2 reference KnowledgeDocument list including taxonomy rules.

    Returns:
        Tuple of (DriveWatcherOutputType, concise reason, parsed DriveGovernanceOp list).
    """
    agy_bin = agy.locate_agy_binary()
    if agy_bin is None:
        return (DriveWatcherOutputType.GATE_REQUIRED, "agy CLI unavailable for Speed-2", [])
    kb = "\n\n".join(f"### {d.file_name}\n{d.body.strip()}" for d in docs) or "(None)"
    blocks = [
        sanitizer.wrap_untrusted_drive_item(
            it.file_id,
            sanitized_map[it.file_id].clean_name,
            sanitized_map[it.file_id].clean_path,
            it.mime_type,
            sanitized_map[it.file_id].clean_text[:4000],
        )[0]
        for it in items
    ]
    prompt = (
        "You are `drive-custodian` in `alfred-drive`. Classify this batch into "
        "`SILENT_NO_ACTION`, `SILENT_AUTO_FILED`, or `GATE_REQUIRED` "
        "(zero deletions, `.md` notes <= 150 lines).\n\n"
        f"## Knowledge Base\n{kb}\n\n## Drive Items\n" + "\n\n".join(blocks)
    )
    cmd = [
        agy_bin, "--project", settings.resolve_workspace_project("alfred-drive"),
        *agy.build_model_cli_flags("alfred-drive"),
        "--output-format", "json", "--json-schema", json.dumps(DRIVE_WATCHER_SCHEMA),
        "--print", prompt, "--disable-slash-commands",
    ]
    completed = agy.run_agy_command(cmd, cwd=agy.resolve_workspace_cwd("alfred-drive"))
    parsed = (
        parse_json_object(completed.stdout)
        if completed is not None and completed.returncode == 0
        else None
    )
    if parsed is None or is_error_envelope(parsed):
        return (DriveWatcherOutputType.GATE_REQUIRED, "agy Drive classification failed", [])
    agy.purge_classifier_session(extract_conversation_id(parsed))
    payload = unwrap_triage_payload(parsed)
    try:
        out_type = DriveWatcherOutputType(str(payload.get("output_type", "")))
        raw_ops = payload.get("operations")
        ops = [
            DriveGovernanceOp.model_validate(
                {**op, "action": DriveGovernanceActionType(op["action"])}
            )
            for op in (raw_ops if isinstance(raw_ops, list) else [])
            if isinstance(op, dict) and "action" in op
        ]
    except Exception:
        return (DriveWatcherOutputType.GATE_REQUIRED, "Invalid agy Drive payload", [])
    return (out_type, str(payload.get("reason") or "Analyzed by alfred-drive"), ops)


def render_drive_silent_trace(
    items: Sequence[DriveDeltaItem],
    output_type: DriveWatcherOutputType,
    reason: str,
    applied: Sequence[DriveMutationResult],
    knowledge_files: Sequence[str],
) -> str:
    """Render concise Markdown audit trace for an auto-archived [SILENT] Drive session.

    Args:
        items: Coalesced DriveDeltaItem batch processed in this tick.
        output_type: SILENT_NO_ACTION or SILENT_AUTO_FILED.
        reason: Concise classification rationale.
        applied: Executed non-destructive DriveMutationResult records.
        knowledge_files: Updated Tier-2 Markdown reference filenames.

    Returns:
        Formatted Markdown trace string.
    """
    file_bullets = "\n".join(f"- `{it.file_id}` — `{it.path or it.name}`" for it in items)
    mut_bullets = (
        "\n".join(
            f"- **{m.action.value}** `{m.file_id}` -> `{m.path or m.name}`" for m in applied
        )
        or "- No Google Drive mutations required."
    )
    know_bullets = "\n".join(f"- `{fn}`" for fn in knowledge_files) or "- No `.md` updates."
    return (
        f"# Automatic Trace `alfred-drive` (`{output_type.value}`)\n\n"
        f"- **Reason**: {reason}\n\n"
        f"## Analyzed Files ({len(items)})\n{file_bullets}\n\n"
        f"## Non-Destructive Mutations Applied\n{mut_bullets}\n\n"
        f"## Synced Knowledge Notes\n{know_bullets}\n"
    )


def render_drive_gate_plan(
    items: Sequence[DriveDeltaItem],
    sanitized_map: Mapping[str, SanitizedDriveTextResult],
    reason: str,
    proposed_ops: Sequence[DriveGovernanceOp],
    violations: Sequence[str],
    suspicious_injection: bool,
) -> str:
    """Render blocking Gate implementation_plan.md when Drive governance requires human review.

    Args:
        items: Coalesced DriveDeltaItem batch escalated to Gate.
        sanitized_map: Sanitized metadata and text per file_id.
        reason: Escalation rationale.
        proposed_ops: Candidate operations withheld pending user approval.
        violations: Policy or line-count violations caught by the deterministic firewall.
        suspicious_injection: True when stealth prompt-injection signals were detected.

    Returns:
        Markdown string persisted as implementation_plan.md.
    """
    alerts: list[str] = []
    if suspicious_injection:
        alerts.append(
            "> [!CAUTION]\n> Suspicious prompt-injection signal detected in Drive file. "
            "Zero automatic mutations executed."
        )
    if violations:
        alerts.append(f"> [!WARNING]\n> Deterministic guard triggered: {'; '.join(violations)}")
    alert_block = ("\n\n".join(alerts) + "\n\n") if alerts else ""
    item_lines = [
        f"- `{it.file_id}` — `{sanitized_map[it.file_id].clean_path or it.name}` (`{it.mime_type}`)"
        for it in items
    ]
    op_lines = [
        f"- `{op.action.value}` (target=`{op.file_id or op.knowledge_file}`, "
        f"parent=`{op.target_parent_id}`, name=`{op.new_name}`)"
        for op in proposed_ops
    ] or ["- Proposal to be confirmed with the user before `plan-executor` execution."]
    return (
        f"# Google Drive Governance Gate (`alfred-drive`)\n\n"
        f"{alert_block}"
        f"## 1. Human Escalation Reason\n- {reason}\n\n"
        f"## 2. Affected Google Drive Files (0 Mutations Executed)\n"
        + "\n".join(item_lines)
        + "\n\n## 3. Proposed Actions After Approval (`Proceed`)\n"
        + "\n".join(op_lines)
        + "\n"
    )
