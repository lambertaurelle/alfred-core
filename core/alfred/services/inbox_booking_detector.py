"""Generic FR/EN dated booking confirmation and automated correspondent detectors."""

from __future__ import annotations

import re
from email.utils import parseaddr

from alfred.domain.models import EmailMessage, fold_accents

PASSIVE_PORTAL_PATTERNS: tuple[str, ...] = (
    r"nouveaux?\s+documents?.*espace",
    r"document\s+reglementaire.*disponible",
    r"facture.*abonnement.*disponible",
    r"espace\s+generali",
    r"newsletter",
    r"se\s+desabonner",
)

_AUTOMATED_EXACT_LOCALS: frozenset[str] = frozenset(
    {
        "noreply",
        "no-reply",
        "no_reply",
        "donotreply",
        "do-not-reply",
        "do_not_reply",
        "ne-pas-repondre",
        "ne_pas_repondre",
        "nepasrepondre",
        "mailer-daemon",
        "postmaster",
        "feedback",
        "notification",
        "notifications",
        "bounce",
        "bounces",
    }
)
_AUTOMATED_LOCAL_RE = re.compile(
    r"(?:^|[._+-])(?:no[-_]?reply|do[-_]?not[-_]?reply|"
    r"ne[-_]?pas[-_]?repondre|mailer[-_]?daemon|bounces?)(?:$|[._+-])"
)

_SUBJECT_DISQUALIFY_RE = re.compile(
    r"\b(?:offre\s+flash|code\s+promo\w*|bon\s+de\s+reduction|vente\s+privee|"
    r"-\s*\d+\s*%|newsletter|inscription\s+(?:a\s+(?:la|notre)\s+)?newsletter|"
    r"recu\s+(?:de|pour\s+votre)\s+paiement|payment\s+receipt|avis\s+de\s+paiement|"
    r"code\s+de\s+verification|verification\s+code|"
    r"facture\s+mensuelle)\b"
)
_PARCEL_DISQUALIFY_RE = re.compile(
    r"\b(?:colis\s+en\s+preparation|suivi\s+de\s+livraison|livraison\s+standard|"
    r"votre\s+colis\s+sera\s+prepare)\b"
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
    r"\bcheck[- ]?in\b.{0,40}\bcheck[- ]?out\b"
    r")"
)
_PURCHASE_CONFIRM_RE = re.compile(r"\bconfirmation\s+d['’ ]achat\b")

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
    """Return True when email_addr belongs to an automated/no-reply/bounce address.

    Args:
        email_addr: Raw RFC 2822 address header or bare email string.
    """
    _, bare = parseaddr(email_addr)
    cleaned = (bare or email_addr).strip().lower()
    if "@" not in cleaned:
        return False
    local = cleaned.partition("@")[0]
    if local in _AUTOMATED_EXACT_LOCALS:
        return True
    return bool(_AUTOMATED_LOCAL_RE.search(local))


def has_dated_booking_signal(msg: EmailMessage) -> bool:
    """Return True only when msg confirms a dated ticket, booking, stay, or appointment.

    Args:
        msg: Normalized EmailMessage to inspect.
    """
    email_body = msg.body_text.split("\n\n[Pièce jointe:", 1)[0]
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


def is_passive_or_automated_email(msg: EmailMessage, combined_folded: str) -> bool:
    """Return True for passive portal notices, newsletters, or non-booking automated senders.

    Args:
        msg: Normalized EmailMessage to inspect.
        combined_folded: Pre-folded subject + snippet + body text for pattern checks.
    """
    if has_dated_booking_signal(msg):
        return False
    if any(re.search(pat, combined_folded) for pat in PASSIVE_PORTAL_PATTERNS):
        return True
    lower_hdrs = {k.strip().lower(): v.strip().lower() for k, v in msg.headers.items()}
    has_unsub = "list-unsubscribe" in lower_hdrs
    auto_sub = bool(lower_hdrs.get("auto-submitted")) and lower_hdrs["auto-submitted"] != "no"
    return has_unsub or auto_sub or is_automated_correspondent_address(msg.sender)
