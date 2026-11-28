"""Markdown Gate plan and silent-trace renderers for alfred-inbox triage outputs."""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence

from alfred.domain.attachment_models import AttachmentFilingTarget, CognitiveTriageDirectives
from alfred.domain.calendar_models import CalendarTriageAction
from alfred.domain.models import (
    EmailMessage,
    InboxOutputType,
    KnowledgeDocument,
    fold_accents,
)
from alfred.services.drive_service import DriveService
from alfred.services.email_security_service import INVISIBLE_UNICODE_RE
from alfred.services.inbox_booking_detector import (
    has_automated_bulk_headers_without_human_reply_to,
    is_automated_correspondent_address,
)


def _security_and_orchestrator_banners(
    needs_orchestrator_study: bool,
    suspicious_injection: bool,
) -> tuple[str, str]:
    """Return optional security caution banner and 2-speed Orchestrator study block."""
    sec = (
        "\n> [!CAUTION]\n"
        "> **Anti-Prompt-Injection Alert (`EmailSanitizer`)**: hidden HTML elements, invisible "
        "Unicode, or injection patterns detected and neutralized. Review carefully.\n"
        if suspicious_injection
        else ""
    )
    orch = (
        "\n## 2-Speed Pipeline (`Orchestrator Carbon` -> `Orchestrator (Neon)`)\n"
        "- After **`Proceed`**, invoke `study-orchestrator` (`Orchestrator Carbon`) "
        "for deep multi-source research (Drive Tier-3 / history), then apply "
        "approved actions with `plan-executor` (`Orchestrator (Neon)`).\n"
        if needs_orchestrator_study
        else ""
    )
    return sec, orch


def _filter_attachment_brief(
    attachment_brief: list[str] | None,
    selected_filenames: set[str] | None,
) -> list[str]:
    """Filter attachment preview bullets to selected filenames when directives specify them."""
    if not attachment_brief:
        return []
    if selected_filenames is None:
        return list(attachment_brief)
    if not selected_filenames:
        return []
    return [
        bullet
        for bullet in attachment_brief
        if any(f"`{fn}`" in bullet.lower() for fn in selected_filenames)
    ]


def build_action_research_brief(
    msg: EmailMessage,
    docs: list[KnowledgeDocument],
    drive_service: DriveService,
    stopwords: frozenset[str],
    attachment_brief: list[str] | None = None,
    cognitive_brief: Sequence[str] = (),
    selected_filenames: set[str] | None = None,
) -> list[str]:
    """Build deduplicated research brief bullets from attachment, cognitive, and Drive context.

    Args:
        msg: Incoming EmailMessage under evaluation.
        docs: Matched Tier-2 KnowledgeDocument list.
        drive_service: DriveService used for 0-token index lookup.
        stopwords: Folded words excluded from Drive index queries.
        attachment_brief: Optional preview bullets extracted from attachments.
        cognitive_brief: Optional concrete action steps emitted by cognitive triage.
        selected_filenames: Optional lowercase set of retained attachment filenames.

    Returns:
        Ordered list of research brief bullet strings.
    """
    brief_items: list[str] = _filter_attachment_brief(attachment_brief, selected_filenames)
    seen_text = set(brief_items)
    for item in cognitive_brief:
        cleaned = item.strip()
        if cleaned and cleaned not in seen_text:
            seen_text.add(cleaned)
            brief_items.append(cleaned)
    for doc in docs:
        brief_items.append(f"Reference note `{doc.file_name}`: {doc.frontmatter.summary}")
    folded_words = re.findall(r"[a-z0-9]{4,}", fold_accents(msg.subject))
    tokens = [tok for tok in folded_words if tok not in stopwords][:4]
    seen_ids: set[str] = set()
    for tok in tokens:
        for hit in drive_service.search_index(tok)[:2]:
            if hit.file_id not in seen_ids:
                seen_ids.add(hit.file_id)
                brief_items.append(f"Google Drive file: `{hit.path}` (`{hit.file_id}`)")
    if not brief_items:
        brief_items.append("Review `01_profil_et_contacts_cles.md` recommended.")
    return brief_items


def _sanitize_inline_cmd_arg(value: str) -> str:
    """Sanitize a command argument for single-line backtick-safe shell quoting."""
    ws_norm = re.sub(r"[\r\n\t]+", " ", value)
    no_ctrl = "".join(
        ch
        for ch in ws_norm
        if ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F) and ch not in {"`", "\u2028", "\u2029"}
    )
    return " ".join(INVISIBLE_UNICODE_RE.sub("", no_ctrl).split())


def _render_attachment_filing_lines(
    msg: EmailMessage,
    targets: Sequence[AttachmentFilingTarget],
    target_doc_id: str,
    drive_watcher_enabled: bool,
) -> str:
    """Format Drive attachment archival CLI lines or alfred-drive delegation command."""
    if not targets:
        return ""
    if drive_watcher_enabled:
        parts: list[str] = []
        for target in targets:
            desc = _sanitize_inline_cmd_arg(target.filename)
            pid = _sanitize_inline_cmd_arg(target.parent_id)
            tname = _sanitize_inline_cmd_arg(target.target_name)
            if pid:
                desc += f" -> parent_id={pid}"
            if tname:
                desc += f" (name={tname})"
            parts.append(desc)
        title = f"Archive attachment(s): {_sanitize_inline_cmd_arg(msg.subject)}"
        brief = (
            f"Archive attachment(s) for message {msg.message_id}: {'; '.join(parts)}. "
            f"Update reference note {target_doc_id}."
        )
        cmd = (
            "alfred delegate --to drive --mode execute-approved "
            f"--source-workspace alfred-inbox --source-message-id {shlex.quote(msg.message_id)} "
            f"--title {shlex.quote(title)} --brief {shlex.quote(brief)}"
        )
        return f"- **Delegate attachment filing (`alfred-drive`)**: `{cmd}`\n"

    lines: list[str] = []
    for target in targets:
        fn = _sanitize_inline_cmd_arg(target.filename)
        pid = _sanitize_inline_cmd_arg(target.parent_id)
        tname = _sanitize_inline_cmd_arg(target.target_name)
        include_fn = len(msg.attachment_filenames) > 1 or len(targets) > 1 or bool(pid or tname)
        fn_flag = f" --filename {shlex.quote(fn)}" if include_fn else ""
        name_flag = f" --name {shlex.quote(tname)}" if tname else ""
        parent_tok = shlex.quote(pid) if pid else "<FOLDER_ID>"
        cmd = (
            f"alfred save-attachment-to-drive --message-id {shlex.quote(msg.message_id)}"
            f"{fn_flag}{name_flag} --parent-id {parent_tok}"
        )
        lines.append(f"- **Save attachment(s) to Drive**: `{cmd}`\n")
    return "".join(lines)


def render_draft_reply_plan(
    reply_to: str,
    reply_subject: str,
    draft_id: str,
    draft_body: str,
    cal_section: str,
    thread_id: str = "",
    needs_orchestrator_study: bool = False,
    suspicious_injection: bool = False,
    *,
    keep_in_inbox: bool = False,
) -> str:
    """Render the Blocking Gate implementation_plan.md for DRAFT_REPLY.

    Args:
        reply_to: Pinned recipient header for the draft reply.
        reply_subject: Subject line for the draft reply.
        draft_id: Created Gmail draft identifier.
        draft_body: Proposed plain-text body.
        cal_section: Optional rendered alfred-calendar [HOLD]/confirmation section.
        thread_id: Optional Gmail threadId archived by default on send-draft.
        needs_orchestrator_study: Whether post-Proceed triggers Orchestrator Carbon study.
        suspicious_injection: Whether EmailSanitizer flagged prompt-injection cues.
        keep_in_inbox: When True, append --keep-in-inbox to prevent Gmail archival.
    """
    sec_banner, orch_block = _security_and_orchestrator_banners(
        needs_orchestrator_study, suspicious_injection
    )
    th_line = f"- **Gmail Thread**: `{thread_id}`\n" if thread_id else ""
    th_flag = f" --thread-id {shlex.quote(thread_id)}" if thread_id else ""
    keep_flag = " --keep-in-inbox" if keep_in_inbox else ""
    send_cmd = f"alfred send-draft --draft-id {shlex.quote(draft_id)}{th_flag}{keep_flag}"
    exec_line = (
        f"- Send the draft and **keep the email in inbox (`--keep-in-inbox`)**: `{send_cmd}`.\n"
        if keep_in_inbox
        else (
            f"- Send the draft and **archive the email in Gmail by default**: `{send_cmd}` "
            "(add a `--keep-in-inbox` comment to keep it in inbox).\n"
        )
    )
    return (
        f"# Gmail Draft Reply (`DRAFT_REPLY`)\n"
        f"{sec_banner}\n"
        f"- **To (pinned)**: `{reply_to}`\n"
        f"- **Subject**: {reply_subject}\n"
        f"- **Gmail Draft ID**: `{draft_id}`\n"
        f"{th_line}\n"
        f"## Proposed Draft (Editable via Inline Comments)\n\n"
        f"```text\n{draft_body}\n```\n"
        f"{cal_section}"
        f"{orch_block}\n"
        f"## Post-Approval Execution (`Proceed`)\n"
        f"{exec_line}"
    )


def render_action_required_plan(
    msg: EmailMessage,
    reason: str,
    brief_items: list[str],
    target_doc_id: str,
    needs_orchestrator_study: bool = False,
    suspicious_injection: bool = False,
    cal_section: str = "",
    *,
    keep_in_inbox: bool = False,
    drive_watcher_enabled: bool = False,
    attachments_to_save: Sequence[AttachmentFilingTarget] | None = None,
    archive_by_default: bool = True,
) -> str:
    """Render the Blocking Gate implementation_plan.md for USER_ACTION_REQUIRED.

    Args:
        msg: Incoming starred EmailMessage.
        reason: Classification rationale.
        brief_items: Gathered Tier-2 and Google Drive context bullets.
        target_doc_id: Suggested Tier-2 reference document ID for post-action update.
        needs_orchestrator_study: Whether post-Proceed triggers Orchestrator Carbon study.
        suspicious_injection: Whether EmailSanitizer flagged prompt-injection cues.
        cal_section: Optional rendered alfred-calendar [HOLD]/confirmation section.
        keep_in_inbox: When True, omit archive-email and retain message in INBOX.
        drive_watcher_enabled: When True, delegate Drive archival to alfred-drive.
        attachments_to_save: Optional explicit attachment filing targets overriding all-PJ default.
        archive_by_default: Whether Gmail archival is emitted by default when keep_in_inbox=False.
    """
    sec_banner, orch_block = _security_and_orchestrator_banners(
        needs_orchestrator_study, suspicious_injection
    )
    bullets = "\n".join(f"- {item}" for item in brief_items)
    targets = (
        list(attachments_to_save)
        if attachments_to_save is not None
        else [AttachmentFilingTarget(filename=fn) for fn in msg.attachment_filenames]
    )
    att_line = _render_attachment_filing_lines(msg, targets, target_doc_id, drive_watcher_enabled)
    arch_target = (
        f"--thread-id {shlex.quote(msg.thread_id)}"
        if msg.thread_id
        else f"--message-id {shlex.quote(msg.message_id)}"
    )
    arch_line = (
        "- **Keep in Gmail inbox (`--keep-in-inbox`)**: do NOT archive this email in `INBOX`.\n"
        if keep_in_inbox or not archive_by_default
        else (
            f"- **Default Gmail archiving upon completion**: "
            f"`alfred archive-email {arch_target} --unstar` "
            "(specify `--keep-in-inbox` in a comment if needed).\n"
        )
    )
    return (
        f"# User Action Required (`USER_ACTION_REQUIRED`)\n"
        f"{sec_banner}\n"
        f"- **Starred email (`STARRED`)**: `{msg.message_id}` (Thread `{msg.thread_id}`)\n"
        f"- **Sender**: `{msg.sender}`\n"
        f"- **Recipients**: `{msg.recipient}`\n"
        f"- **Subject**: {msg.subject}\n"
        f"- **Reason**: {reason}\n\n"
        f"## Full Research Brief & Gathered Context\n"
        f"{bullets}\n"
        f"{cal_section}"
        f"{orch_block}\n"
        f"## Post-Approval Execution (`Proceed`)\n"
        f"- Target reference note: `{target_doc_id}`\n"
        f"{att_line}"
        f"{arch_line}"
    )


def render_silent_trace_markdown(msg: EmailMessage, reason: str) -> str:
    """Format a concise audit summary for an auto-archived SILENT_NO_ACTION conversation.

    Args:
        msg: Processed EmailMessage.
        reason: Classification rationale.
    """
    return (
        f"### Auto-Archived Trace `SILENT_NO_ACTION`\n"
        f"- **Message ID**: `{msg.message_id}` (Thread `{msg.thread_id}`)\n"
        f"- **From**: `{msg.sender}`\n"
        f"- **Subject**: {msg.subject}\n"
        f"- **Reason**: {reason}\n"
        f"- **Gmail Action**: Zero modifications (`0` drafts, `0` stars).\n"
    )


def finalize_cognitive_triage_decision(
    msg: EmailMessage,
    out_type: InboxOutputType,
    reason: str,
    draft: str | None,
    cal_action: CalendarTriageAction,
    conv_id: str | None,
    needs_orch: bool,
    matched_pat: str | None,
    is_from_self: bool,
    use_cognitive_agy: bool,
    has_cognitive_fn: bool,
    directives: CognitiveTriageDirectives,
    last_needs_orch: bool,
    pinned_recipient: str,
) -> tuple[
    InboxOutputType, str, str | None, CalendarTriageAction, str | None, bool,
    CognitiveTriageDirectives,
]:
    """Enforce post-cognitive fallback recovery, action promotion, and anti-noreply guards.

    Args:
        msg: Sanitized EmailMessage under triage.
        out_type: Output type returned by cognitive classifier or default.
        reason: Classification rationale string.
        draft: Optional draft reply text.
        cal_action: Parsed CalendarTriageAction.
        conv_id: Optional classifier conversation ID (None on fallback).
        needs_orch: Deterministic complex-study indicator.
        matched_pat: Matched ACTION_REQUIRED_PATTERNS regex or None.
        is_from_self: Whether sender belongs to authenticated owner.
        use_cognitive_agy: Whether agy CLI cognitive triage was requested.
        has_cognitive_fn: Whether an explicit cognitive_fn callback was injected.
        directives: Structured directives parsed from Agy output.
        last_needs_orch: AgySessionManager.last_needs_orchestrator_study flag.
        pinned_recipient: Firewall-pinned reply recipient header.

    Returns:
        Normalized 7-tuple for InboxTriageService dispatch.
    """
    eff_dir = (
        directives if use_cognitive_agy and not has_cognitive_fn else CognitiveTriageDirectives()
    )
    eff_orch = needs_orch or eff_dir.needs_orchestrator_study or last_needs_orch
    if conv_id is None and not has_cognitive_fn and matched_pat is not None:
        return (
            InboxOutputType.USER_ACTION_REQUIRED,
            f"Matched user-action signal: '{matched_pat}'",
            None, cal_action, None, eff_orch, eff_dir,
        )
    if (
        use_cognitive_agy
        and matched_pat is not None
        and out_type == InboxOutputType.SILENT_NO_ACTION
    ):
        out_type = InboxOutputType.USER_ACTION_REQUIRED
    if out_type == InboxOutputType.SILENT_NO_ACTION and eff_dir.attachments_to_save:
        out_type = InboxOutputType.USER_ACTION_REQUIRED
    if out_type == InboxOutputType.DRAFT_REPLY and (
        is_from_self
        or is_automated_correspondent_address(pinned_recipient)
        or has_automated_bulk_headers_without_human_reply_to(msg)
    ):
        rsn = (
            "Self-authored email requiring personal follow-up (no self-reply draft)"
            if is_from_self
            else "Automated/no-reply correspondent requires user action (no reply draft)"
        )
        return (
            InboxOutputType.USER_ACTION_REQUIRED, rsn, None, cal_action, conv_id,
            eff_orch, eff_dir,
        )
    return (out_type, reason, draft, cal_action, conv_id, eff_orch, eff_dir)
