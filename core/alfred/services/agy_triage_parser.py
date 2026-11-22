"""Pure cognitive triage JSON schema, nonce-spotlighted prompt builder, and CLI JSON parsers."""

from __future__ import annotations

import json
from pathlib import Path

from alfred.domain.calendar_models import (
    CalendarActionIntent,
    CalendarSlotProposal,
    CalendarTriageAction,
)
from alfred.domain.models import AlfredPaths, EmailMessage, InboxOutputType, KnowledgeDocument
from alfred.services.email_security_service import wrap_untrusted_email_block
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
    doc_blocks = [
        f"### {d.file_name} ({d.frontmatter.title})\n{d.frontmatter.summary}\n{d.body}"
        for d in docs
    ]
    ref_context = "\n\n".join(doc_blocks) if doc_blocks else "(Aucune fiche correspondante)"
    identity_line = (
        f"Adresses de l'utilisateur propriétaire (ne jamais créer de DRAFT_REPLY vers soi) : "
        f"{', '.join(sorted(user_addresses))}\n"
        if user_addresses
        else ""
    )
    marker = "\n\n[Pièce jointe:"
    if marker in msg.body_text:
        email_part, att_part = msg.body_text.split(marker, 1)
        clipped_body = f"{email_part[:2400]}{marker}{att_part[:2400]}"
    else:
        clipped_body = msg.body_text[:4000]
    spotlighted, _ = wrap_untrusted_email_block(msg, clipped_body)
    if paths is not None:
        loader = PromptTemplateLoader(paths)
        custom_rules = loader.load_custom_inbox_rules() or "(Aucune règle personnalisée)"
        return loader.render(
            "inbox_triage.md",
            {
                "knowledge_frontmatters": ref_context,
                "custom_inbox_rules": custom_rules,
                "calendar_snapshot": calendar_context or "(Aucun événement futur)",
                "spotlighted_email": f"{identity_line}{spotlighted}",
            },
        )
    cal_block = (
        f"\nAgenda Principal (Tous les Événements Futurs — Sans Horizon) :\n{calendar_context}\n"
        if calendar_context
        else ""
    )
    return (
        "Mode classifieur JSON (pas d'artefact implementation_plan.md, zéro appel d'outil). "
        "Classe cet e-mail entrant dans l'une des 3 sorties exclusives d'Alfred V8.0 "
        "(SILENT_NO_ACTION, DRAFT_REPLY, USER_ACTION_REQUIRED). "
        "Si c'est une confirmation datée d'achat de billet, réservation, hôtel, transport, "
        "spectacle ou RDV absent de l'agenda, renseigne calendar_action.intent='CONFIRM_EVENT' "
        "avec attendees=[] et send_invites=false pour les marchands/billetteries (durée +1h par "
        "défaut si l'heure de fin manque).\n\n"
        f"{identity_line}{spotlighted}\n{cal_block}\nFiches de référence Tier-2:\n{ref_context}"
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
