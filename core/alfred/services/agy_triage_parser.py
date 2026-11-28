"""Pure cognitive triage JSON schema, nonce-spotlighted prompt builder, and CLI JSON parsers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from alfred.domain.attachment_models import AttachmentFilingTarget, CognitiveTriageDirectives
from alfred.domain.calendar_models import (
    CalendarActionIntent,
    CalendarSlotProposal,
    CalendarTriageAction,
)
from alfred.domain.models import AlfredPaths, EmailMessage, InboxOutputType, KnowledgeDocument
from alfred.services.attachment_extractor import sanitize_attachment_filename, split_attachment_body
from alfred.services.email_security_service import INVISIBLE_UNICODE_RE, wrap_untrusted_email_block
from alfred.services.prompt_loader import PromptTemplateLoader

ERROR_ENVELOPE_STATUSES = frozenset(
    {"ERROR", "FAILED", "DEADLINE_EXCEEDED", "CANCELLED", "TIMEOUT"}
)

COGNITIVE_TRIAGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "output_type": {
            "type": "string",
            "enum": [
                InboxOutputType.SILENT_NO_ACTION.value,
                InboxOutputType.DRAFT_REPLY.value,
                InboxOutputType.USER_ACTION_REQUIRED.value,
            ],
        },
        "reason": {"type": "string"},
        "draft_body": {"type": "string"},
        "needs_orchestrator_study": {"type": "boolean"},
        "keep_in_inbox": {"type": "boolean"},
        "research_brief": {"type": "array", "items": {"type": "string"}},
        "attachments_to_save": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "parent_id": {"type": "string"},
                    "target_name": {"type": "string"},
                },
                "required": ["filename"],
                "additionalProperties": False,
            },
        },
        "calendar_action": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [
                        CalendarActionIntent.NONE.value,
                        CalendarActionIntent.CREATE_HOLD.value,
                        CalendarActionIntent.CONFIRM_EVENT.value,
                    ],
                },
                "slots": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "start_iso": {"type": "string"},
                            "end_iso": {"type": "string"},
                            "attendees": {"type": "array", "items": {"type": "string"}},
                            "send_invites": {"type": "boolean"},
                            "location": {"type": "string"},
                            "description": {"type": "string"},
                        },
                        "required": ["summary", "start_iso", "end_iso"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["intent", "slots"],
            "additionalProperties": False,
        },
    },
    "required": ["output_type", "reason"],
    "additionalProperties": False,
}


def _prepare_triage_prompt_inputs(
    msg: EmailMessage,
    docs: list[KnowledgeDocument],
    user_addresses: frozenset[str],
) -> tuple[str, str, str]:
    """Format Tier-2 knowledge context, owner identity line, and spotlighted email."""
    doc_blocks = [
        f"### {d.file_name} ({d.frontmatter.title})\n{d.frontmatter.summary}\n{d.body}"
        for d in docs
    ]
    ref_context = "\n\n".join(doc_blocks) if doc_blocks else "(No matching reference document)"
    identity_line = (
        f"Owner user addresses (never generate a DRAFT_REPLY to yourself): "
        f"{', '.join(sorted(user_addresses))}\n"
        if user_addresses
        else ""
    )
    email_part, att_marker, att_part = split_attachment_body(msg.body_text)
    if att_marker:
        sep = "\n\n" if email_part else ""
        clipped_body = f"{email_part[:2400]}{sep}{att_marker}{att_part[:2400]}"
    else:
        clipped_body = msg.body_text[:4000]
    spotlighted, _ = wrap_untrusted_email_block(msg, clipped_body)
    return ref_context, identity_line, spotlighted


def build_cognitive_triage_prompt(
    msg: EmailMessage,
    docs: list[KnowledgeDocument],
    user_addresses: frozenset[str] = frozenset(),
    calendar_context: str = "",
    paths: AlfredPaths | None = None,
) -> str:
    """Build a nonce-spotlighted cognitive triage prompt for agy --print (Hydrogen).

    Args:
        msg: Sanitized incoming Gmail message.
        docs: Loaded Tier-2 reference KnowledgeDocument list.
        user_addresses: Optional authenticated owner email addresses.
        calendar_context: Optional full-horizon primary calendar and [HOLD] snapshot.
        paths: Optional AlfredPaths enabling Markdown prompt template & custom rule loading.
    """
    ref_context, identity_line, spotlighted = _prepare_triage_prompt_inputs(
        msg, docs, user_addresses
    )
    if paths is not None:
        loader = PromptTemplateLoader(paths)
        custom_rules = loader.load_custom_inbox_rules() or "(No custom rules)"
        return loader.render(
            "inbox_triage.md",
            {
                "knowledge_frontmatters": ref_context,
                "custom_inbox_rules": custom_rules,
                "calendar_snapshot": calendar_context or "(No upcoming events)",
                "spotlighted_email": f"{identity_line}{spotlighted}",
            },
        )
    cal_block = (
        f"\nPrimary Calendar (All Future Events — Unlimited Horizon):\n{calendar_context}\n"
        if calendar_context
        else ""
    )
    return (
        "JSON classifier mode (no implementation_plan.md artifact, zero tool calls). "
        "Classify this incoming email into one of the 3 exclusive Alfred V8.0 outputs "
        "(SILENT_NO_ACTION, DRAFT_REPLY, USER_ACTION_REQUIRED). "
        "If it is a dated ticket purchase, booking, hotel, transport, show, or appointment "
        "confirmation missing from the calendar, populate calendar_action.intent='CONFIRM_EVENT' "
        "with attendees=[] and send_invites=false for merchants/ticketing (default +1h duration "
        "when end time is missing).\n\n"
        f"{identity_line}{spotlighted}\n{cal_block}\nTier-2 Reference Docs:\n{ref_context}"
    )


def is_error_envelope(payload: dict[str, object]) -> bool:
    """Return True when an agy JSON envelope reports an execution error status.

    Args:
        payload: Parsed JSON dictionary from agy stdout.
    """
    status = payload.get("status")
    return isinstance(status, str) and status.strip().upper() in ERROR_ENVELOPE_STATUSES


def parse_json_object(raw_stdout: str) -> dict[str, object] | None:
    """Parse a single- or multi-line JSON object from stdout, tolerating log lines.

    Args:
        raw_stdout: Raw stdout string emitted by agy CLI.
    """
    stripped = raw_stdout.strip()
    if not stripped:
        return None
    try:
        direct = json.loads(stripped)
        if isinstance(direct, dict):
            return {str(k): v for k, v in direct.items()}
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    offsets = [
        0,
        *(i + 1 for i, ch in enumerate(stripped) if ch == "\n" and i + 1 < len(stripped)),
    ]
    for start in reversed(offsets):
        sub = stripped[start:].lstrip(" \t")
        if not sub.startswith("{"):
            continue
        try:
            parsed, end_idx = decoder.raw_decode(sub)
        except json.JSONDecodeError:
            continue
        rem = sub[end_idx:].lstrip(" \t")
        if (
            isinstance(parsed, dict)
            and (not rem or rem.startswith("\n"))
            and not rem.lstrip().startswith(("}", "]", ","))
        ):
            return {str(k): v for k, v in parsed.items()}
    return None


def extract_conversation_id(payload: dict[str, object]) -> str | None:
    """Extract a path-safe conversation ID from a non-error agy JSON response.

    Args:
        payload: Parsed JSON dictionary from agy CLI.
    """
    if is_error_envelope(payload):
        return None
    containers: list[dict[str, object]] = [payload]
    for wrap_key in ("result", "data"):
        nested = payload.get(wrap_key)
        if isinstance(nested, dict):
            nested_dict = {str(k): v for k, v in nested.items()}
            if not is_error_envelope(nested_dict):
                containers.append(nested_dict)
    for obj in containers:
        for key in ("conversation_id", "conversationId", "session_id", "sessionId", "id"):
            val = obj.get(key)
            if isinstance(val, str):
                cleaned = val.strip()
                if (
                    cleaned
                    and not any(bad in cleaned for bad in ("/", "\\", "..", "\x00"))
                    and Path(cleaned).name == cleaned
                    and cleaned not in {".", ".."}
                ):
                    return cleaned
    return None


def unwrap_triage_payload(payload: dict[str, object]) -> dict[str, object]:
    """Unwrap nested structured output envelopes returned by CLI JSON formatters.

    Args:
        payload: Outer JSON response dictionary.
    """
    if "output_type" in payload:
        return payload
    for key in ("structured_output", "result", "data", "output", "response"):
        nested = payload.get(key)
        if isinstance(nested, dict) and "output_type" in nested:
            return {str(k): v for k, v in nested.items()}
        if isinstance(nested, str):
            parsed = parse_json_object(nested)
            if parsed is not None and "output_type" in parsed:
                return parsed
    return payload


def parse_calendar_action(unwrapped: dict[str, object]) -> CalendarTriageAction:
    """Parse an optional calendar_action block from a cognitive triage payload.

    Args:
        unwrapped: Unwrapped triage dictionary potentially containing 'calendar_action'.
    """
    raw = unwrapped.get("calendar_action")
    if not isinstance(raw, dict):
        return CalendarTriageAction()
    try:
        intent = CalendarActionIntent(str(raw.get("intent", "NONE")))
    except ValueError:
        return CalendarTriageAction()
    raw_slots = raw.get("slots")
    slots: list[CalendarSlotProposal] = []
    if isinstance(raw_slots, list):
        for item in raw_slots:
            if not isinstance(item, dict):
                continue
            summary = str(item.get("summary") or "").strip()
            start_iso = str(item.get("start_iso") or "").strip()
            end_iso = str(item.get("end_iso") or "").strip()
            if not (summary and start_iso and end_iso):
                continue
            raw_att = item.get("attendees")
            att = (
                [str(a).strip().lower() for a in raw_att if str(a).strip()]
                if isinstance(raw_att, list)
                else []
            )
            slots.append(
                CalendarSlotProposal(
                    summary=summary,
                    start_iso=start_iso,
                    end_iso=end_iso,
                    attendees=att,
                    send_invites=bool(item.get("send_invites", True)),
                    location=str(item.get("location") or "").strip(),
                    description=str(item.get("description") or "").strip(),
                )
            )
    return CalendarTriageAction(intent=intent, slots=slots) if slots else CalendarTriageAction()


def _strip_control_chars(raw: str) -> str:
    """Remove C0, C1, and CR/LF/NUL control characters and strip surrounding whitespace."""
    return "".join(ch for ch in raw if ord(ch) >= 32 and not (0x7F <= ord(ch) <= 0x9F)).strip()


def _sanitize_directive_target_name(raw_name: str) -> str:
    """Sanitize optional Drive target_name, returning empty string for pure traversal."""
    no_ctrl = _strip_control_chars(raw_name)
    no_inv = INVISIBLE_UNICODE_RE.sub("", no_ctrl).replace("`", "")
    slashed = re.sub(r"[\uff0f\u2215\\]", "/", no_inv).strip()
    segments = [seg.strip() for seg in slashed.split("/") if seg.strip()]
    if not segments or segments[-1] in {".", ".."}:
        return ""
    return sanitize_attachment_filename(raw_name)


def _resolve_directive_filename(
    raw_fn: str,
    available_filenames: Sequence[str],
    canonical_map: dict[str, str],
) -> str | None:
    """Validate and resolve a directive attachment filename against known or safe names."""
    if available_filenames:
        return canonical_map.get(raw_fn.lower())
    bad_tokens = ("..", "/", "\\", "\uff0f", "\u2215", "\x00", "\n", "\r", "`")
    if (
        sanitize_attachment_filename(raw_fn) != raw_fn
        or any(tok in raw_fn for tok in bad_tokens)
        or INVISIBLE_UNICODE_RE.search(raw_fn) is not None
    ):
        return None
    return raw_fn


def parse_triage_directives(
    unwrapped: Mapping[str, object],
    available_filenames: Sequence[str] = (),
) -> CognitiveTriageDirectives:
    """Validate and sanitize optional triage directives from an Agy cognitive output.

    Args:
        unwrapped: Unwrapped triage dictionary returned by agy CLI.
        available_filenames: Canonical non-inline attachment filenames present on the email.

    Returns:
        Validated CognitiveTriageDirectives value object.
    """
    raw_brief = unwrapped.get("research_brief")
    brief = (
        [item.strip() for item in raw_brief if isinstance(item, str) and item.strip()][:8]
        if isinstance(raw_brief, list)
        else []
    )
    raw_atts = unwrapped.get("attachments_to_save")
    att_specified = isinstance(raw_atts, list)
    canonical_map = {fn.strip().lower(): fn.strip() for fn in available_filenames if fn.strip()}
    targets: list[AttachmentFilingTarget] = []
    seen: set[str] = set()
    if isinstance(raw_atts, list):
        for item in raw_atts:
            if not isinstance(item, Mapping):
                continue
            raw_fn = str(item.get("filename") or "").strip()
            if not raw_fn:
                continue
            canon_fn = _resolve_directive_filename(raw_fn, available_filenames, canonical_map)
            if canon_fn is None:
                continue
            key = canon_fn.lower()
            if key in seen:
                continue
            seen.add(key)
            raw_pid = _strip_control_chars(str(item.get("parent_id") or ""))
            parent_id = raw_pid.replace("`", "").strip()
            raw_target = str(item.get("target_name") or "").strip()
            target_name = _sanitize_directive_target_name(raw_target) if raw_target else ""
            targets.append(
                AttachmentFilingTarget(
                    filename=canon_fn, parent_id=parent_id, target_name=target_name
                )
            )
    return CognitiveTriageDirectives(
        keep_in_inbox=bool(unwrapped.get("keep_in_inbox", False)),
        needs_orchestrator_study=bool(unwrapped.get("needs_orchestrator_study", False)),
        research_brief=brief,
        attachments_specified=att_specified,
        attachments_to_save=targets,
    )
