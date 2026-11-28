"""Generic FR/EN dated booking confirmation and automated correspondent detectors."""

from __future__ import annotations

import re
from email.utils import getaddresses

from alfred.domain.feature_models import InboxFilterConfig
from alfred.domain.models import EmailMessage, fold_accents
from alfred.services.attachment_extractor import split_attachment_body

PASSIVE_PORTAL_PATTERNS: tuple[str, ...] = (
    r"nouveaux?\s+documents?.*espace",
    r"document\s+reglementaire.*disponible",
    r"facture.*abonnement.*disponible",
    r"espace\s+generali",
    r"newsletter",
    r"se\s+desabonner",
    r"new\s+documents?.*(?:portal|account)",
    r"regulatory\s+document.*available",
    r"monthly\s+(?:subscription\s+)?invoice.*available",
    r"unsubscribe",
)

_AUTOMATED_EXACT_LOCALS: frozenset[str] = frozenset(
    {
        "noreply",
        "no-reply",
        "no_reply",
        "no.reply",
        "donotreply",
        "do-not-reply",
        "do_not_reply",
        "do.not.reply",
        "ne-pas-repondre",
        "ne_pas_repondre",
        "nepasrepondre",
        "ne.pas.repondre",
        "mailer-daemon",
        "daemon",
        "postmaster",
        "feedback",
        "notification",
        "notifications",
        "alert",
        "alerts",
        "bounce",
        "bounces",
    }
)
_AUTOMATED_LOCAL_RE = re.compile(
    r"(?:^|[._+-])(?:"
    r"no[._-]?reply\d*|do[._-]?not[._-]?reply\d*|ne[._-]?pas[._-]?repondre\d*|"
    r"mailer[._-]?daemon|bounces?|notifications?|alerts?"
    r")(?:$|[._+-])"
)

_SUBJECT_DISQUALIFY_RE = re.compile(
    r"\b(?:offre\s+flash|flash\s+sale|code\s+promo\w*|promo\s+code|discount\s+code|"
    r"bon\s+de\s+reduction|vente\s+privee|private\s+sale|"
    r"-\s*\d+\s*%|newsletter|inscription\s+(?:a\s+(?:la|notre)\s+)?newsletter|"
    r"recu\s+(?:de|pour\s+votre)\s+paiement|payment\s+receipt|avis\s+de\s+paiement|"
    r"code\s+de\s+verification|verification\s+code|"
    r"facture\s+mensuelle|monthly\s+invoice)\b"
)
_PARCEL_DISQUALIFY_RE = re.compile(
    r"\b(?:colis\s+en\s+preparation|suivi\s+de\s+livraison|livraison\s+standard|"
    r"votre\s+colis\s+sera\s+prepare|package\s+tracking|"
    r"parcel\s+(?:is\s+)?out\s+for\s+delivery|out\s+for\s+delivery|shipment\s+tracking)\b"
)

_BOOKING_EVENT_CUE_RE = re.compile(
    r"\b(?:billet|billets|e-?billet|ticket|e-?ticket|reservation|sejour|hotel|"
    r"vol|train|spectacle|concert|exposition|musee|theatre|cinema|visite|atelier|"
    r"experience|rendez[- ]vous|rdv|seance|entree|place|places|embarquement|"
    r"boarding|check[- ]?in|gite|lodge)\b"
)

_BOOKING_CONFIRM_RE = re.compile(
    r"(?:"
    r"confirmation\s+de\s+(?:votre\s+)?(?:rendez[- ]vous|rdv|reservation|sejour|"
    r"billet|billets|vol|train|spectacle|visite|atelier)|"
    r"rappel\s+de\s+(?:votre\s+)?(?:rendez[- ]vous|rdv)|"
    r"(?:booking|reservation|appointment|ticket|stay|flight|train)\s+confirm(?:ation|ed)|"
    r"(?:reservation|rendez[- ]vous|rdv|billet|e-?billet|e-?ticket|sejour|entree|"
    r"place|places|vol|trajet|stay)\b.{0,50}\b(?:confirm(?:e|ee|es|ed)|valide(?:e|s)?)\b|"
    r"\b(?:confirm(?:e|ee|es|ed)|valide(?:e|s)?)\b.{0,30}\b"
    r"(?:reservation|rendez[- ]vous|rdv|billet|e-?billet|e-?ticket|sejour|stay)\b|"
    r"\be-?billet\b|\be-?ticket\b|\bboarding\s+pass\b|\bcarte\s+d['’ ]embarquement\b|"
    r"\bid\s+du\s+billet\b|\bnumero\s+de\s+(?:billet|reservation)\b|"
    r"\breservation\s*#[a-z0-9_-]+|\bbooking\s+(?:id|ref|reference)\b|"
    r"\bcheck[- ]?in\b.{0,40}\bcheck[- ]?out\b|"
    r"\bappointment\s+reminder\b"
    r")"
)
_PURCHASE_CONFIRM_RE = re.compile(
    r"(?:\bconfirmation\s+d['’ ]achat\b|\bpurchase\s+confirmation\b)"
)

_MONTH_WORDS = (
    r"(?:janvier|january|janv?\.?|"
    r"fevrier|february|fevr?\.?|feb\.?|"
    r"mars|march|mar\.?|"
    r"avril|april|avr\.?|apr\.?|"
    r"mai|may|"
    r"juin|june|jun\.?|"
    r"juillet|july|juil\.?|jul\.?|"
    r"aout|august|aug\.?|"
    r"septembre|september|sept?\.?|"
    r"octobre|october|oct\.?|"
    r"novembre|november|nov\.?|"
    r"decembre|december|dec\.?)"
)
_SINGLE_DATE = (
    rf"(?:20\d{{2}}-\d{{2}}-\d{{2}}|"
    rf"\d{{1,2}}[/-]\d{{1,2}}[/-](?:20)?\d{{2}}|"
    rf"\d{{1,2}}\s+{_MONTH_WORDS}(?:\s+20\d{{2}})?|"
    rf"{_MONTH_WORDS}\s+\d{{1,2}}(?:,?\s+20\d{{2}})?)"
)
_DATE_RE = re.compile(rf"\b{_SINGLE_DATE}(?!\w)")
_TIME_RE = re.compile(r"\b\d{1,2}\s*(?:h\s*\d{2}|:\d{2}(?:\s*[ap]\.?m\.?)?|h(?!\w))")
_STAY_OR_DEPART_RE = re.compile(
    rf"(?:\bcheck[- ]?in\b|\bcheck[- ]?out\b|\bdepart\b|\bdeparture\b|"
    rf"\bembarquement\b|\bboarding\b|"
    rf"\bdu\s+{_SINGLE_DATE}\s+au\s+{_SINGLE_DATE}(?!\w)|"
    rf"\bfrom\s+{_SINGLE_DATE}\s+to\s+{_SINGLE_DATE}(?!\w))"
)


def is_automated_correspondent_address(email_addr: str) -> bool:
    """Return True when any address in email_addr belongs to an automated/no-reply/bounce address.

    Args:
        email_addr: Raw RFC 2822 address header or comma-separated email list.
    """
    extracted = [addr.strip().lower() for _, addr in getaddresses([email_addr]) if "@" in addr]
    if not extracted and "@" in email_addr:
        extracted = [email_addr.strip().lower()]
    for cleaned in extracted:
        local = cleaned.partition("@")[0]
        if local in _AUTOMATED_EXACT_LOCALS or _AUTOMATED_LOCAL_RE.search(local):
            return True
    return False


def has_automated_bulk_headers_without_human_reply_to(msg: EmailMessage) -> bool:
    """Return True when bulk/auto headers exist without a distinct human Reply-To address.

    Args:
        msg: Normalized EmailMessage to inspect.
    """
    lower_hdrs = {k.strip().lower(): v.strip() for k, v in msg.headers.items()}
    precedence = lower_hdrs.get("precedence", "").lower()
    auto_sub = lower_hdrs.get("auto-submitted", "").lower()
    has_bulk = (
        precedence in {"bulk", "list", "junk", "auto_reply"}
        or (bool(auto_sub) and auto_sub != "no")
        or bool(lower_hdrs.get("list-unsubscribe") or lower_hdrs.get("list-id"))
    )
    if not has_bulk:
        return False
    rt_addrs = [
        addr.strip().lower()
        for _, addr in getaddresses([lower_hdrs.get("reply-to", "")])
        if "@" in addr and not any(ch.isspace() for ch in addr.strip())
    ]
    if not rt_addrs:
        return True
    sender_addrs = {addr.strip().lower() for _, addr in getaddresses([msg.sender]) if "@" in addr}
    if all(addr in sender_addrs for addr in rt_addrs):
        return True
    return any(is_automated_correspondent_address(addr) for addr in rt_addrs)


def has_dated_booking_signal(msg: EmailMessage) -> bool:
    """Return True only when msg confirms a dated ticket, booking, stay, or appointment.

    Args:
        msg: Normalized EmailMessage to inspect.
    """
    email_body, _, _ = split_attachment_body(msg.body_text)
    subj_folded = fold_accents(msg.subject)
    corpus = fold_accents(f"{msg.subject}\n{msg.snippet}\n{email_body}")
    if _SUBJECT_DISQUALIFY_RE.search(subj_folded) or _PARCEL_DISQUALIFY_RE.search(corpus):
        return False
    has_confirm = bool(_BOOKING_CONFIRM_RE.search(corpus)) or bool(
        _PURCHASE_CONFIRM_RE.search(corpus) and _BOOKING_EVENT_CUE_RE.search(corpus)
    )
    if not has_confirm or not _DATE_RE.search(corpus):
        return False
    return bool(_TIME_RE.search(corpus) or _STAY_OR_DEPART_RE.search(corpus))


def is_passive_or_automated_email(
    msg: EmailMessage,
    combined_folded: str,
    inbox_filters: InboxFilterConfig | None = None,
    *,
    has_doc_attachment_override: bool | None = None,
) -> bool:
    """Return True for passive portal notices, newsletters, or non-booking automated senders.

    Args:
        msg: Normalized EmailMessage to inspect.
        combined_folded: Pre-folded subject + snippet + body text for pattern checks.
        inbox_filters: Optional InboxFilterConfig enabling automated-sender allowlist exemption.
        has_doc_attachment_override: Optional override replacing attachment extension check.
    """
    if has_dated_booking_signal(msg):
        return False
    if inbox_filters is not None:
        from alfred.services.inbox_eligibility import (
            has_eligible_document_attachment,
            is_allowlisted_automated_sender,
            is_allowlisted_priority_sender,
        )

        if is_allowlisted_priority_sender(msg.sender, inbox_filters):
            return False
        if is_allowlisted_automated_sender(msg.sender, inbox_filters):
            has_doc = (
                has_doc_attachment_override
                if has_doc_attachment_override is not None
                else has_eligible_document_attachment(msg)
            )
            if has_doc:
                return False
    if any(re.search(pat, combined_folded) for pat in PASSIVE_PORTAL_PATTERNS):
        return True
    lower_hdrs = {k.strip().lower(): v.strip().lower() for k, v in msg.headers.items()}
    has_unsub = "list-unsubscribe" in lower_hdrs
    auto_sub = bool(lower_hdrs.get("auto-submitted")) and lower_hdrs["auto-submitted"] != "no"
    return has_unsub or auto_sub or is_automated_correspondent_address(msg.sender)
