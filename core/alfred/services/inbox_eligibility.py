"""Deterministic 0-token pre-filtering and thread coalescence for alfred-inbox."""

from __future__ import annotations

import re
from email.utils import getaddresses, parseaddr
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from alfred.domain.feature_models import InboxFilterConfig
from alfred.domain.models import EmailMessage
from alfred.services.inbox_booking_detector import has_dated_booking_signal

CALENDAR_SUBJECT_RE = re.compile(
    r"^\s*(?:(?:re|fwd|tr)\s*:\s*)*"
    r"(?:invitation|updated\s+invitation|canceled\s+event|cancelled\s+event|"
    r"accepted|declined|tentative|accept[eé]|refus[eé]|peut-[eê]tre|"
    r"annul[eé]|mise\s+[aà]\s+jour\s+de\s+l['’]invitation)\s*:",
    re.IGNORECASE,
)
CALENDAR_MIMES: frozenset[str] = frozenset({"text/calendar", "application/ics"})
SKIPPED_GMAIL_CATEGORIES: frozenset[str] = frozenset(
    {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}
)
BULK_PRECEDENCE_VALUES: frozenset[str] = frozenset({"bulk", "list", "junk", "auto_reply"})
DOCUMENT_ATTACHMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf", ".docx", ".xlsx", ".doc", ".xls", ".csv", ".tsv",
        ".odt", ".ods", ".rtf", ".txt", ".xml", ".json", ".html", ".htm", ".md",
    }
)


def _extract_valid_emails(header: str) -> list[str]:
    """Extract normalized email addresses from an RFC 2822 header via getaddresses."""
    valid: list[str] = []
    for _, addr in getaddresses([header]):
        cleaned = addr.strip().lower()
        if not cleaned or any(ch.isspace() or ch == "," for ch in cleaned):
            continue
        local, sep, domain = cleaned.partition("@")
        if sep == "@" and local and domain and not domain.startswith(".") and not domain.endswith(
            "."
        ):
            valid.append(cleaned)
    return valid


def _matches_denylist_addrs(addrs: list[str], config: InboxFilterConfig) -> bool:
    """Return True if any address in addrs matches configured denylist senders or domains."""
    if not addrs:
        return False
    deny_senders = {
        (parseaddr(s)[1] or s).strip().lower() for s in config.denylist_senders if s.strip()
    }
    deny_domains = {
        d.strip().lower().lstrip("@.") for d in config.denylist_domains if d.strip().lstrip("@.")
    }
    for addr in addrs:
        if addr in deny_senders:
            return True
        domain = addr.partition("@")[2]
        if any(domain == d or domain.endswith(f".{d}") for d in deny_domains):
            return True
    return False


def has_eligible_document_attachment(msg: EmailMessage) -> bool:
    """Return True when msg lists at least one attachment with a document extension.

    Args:
        msg: Normalized incoming EmailMessage.

    Returns:
        True when any filename suffix matches DOCUMENT_ATTACHMENT_EXTENSIONS.
    """
    return any(
        Path(fn.strip()).suffix.lower() in DOCUMENT_ATTACHMENT_EXTENSIONS
        for fn in msg.attachment_filenames
        if fn.strip()
    )


def _matches_allowlist_addrs(
    sender: str,
    raw_senders: list[str],
    raw_domains: list[str],
    config: InboxFilterConfig,
) -> bool:
    """Return True when all extracted sender addresses match allowlists without denylist hits."""
    addrs = _extract_valid_emails(sender)
    if not addrs or _matches_denylist_addrs(addrs, config):
        return False
    allow_senders = {
        (parseaddr(s)[1] or s).strip().lower() for s in raw_senders if s.strip()
    }
    allow_domains = [dom for d in raw_domains if (dom := d.strip().lower().lstrip("@."))]
    for addr in addrs:
        if addr in allow_senders:
            continue
        domain = addr.partition("@")[2]
        if not any(domain == d or domain.endswith(f".{d}") for d in allow_domains):
            return False
    return True


def is_allowlisted_priority_sender(sender: str, config: InboxFilterConfig) -> bool:
    """Return True when all extracted sender addresses match priority allowlists and no denylist.

    Args:
        sender: Raw RFC 2822 sender header or bare email address.
        config: InboxFilterConfig containing priority allowlist and denylist rules.

    Returns:
        True when extracted addresses are non-empty and all match priority allowlist rules.
    """
    return _matches_allowlist_addrs(
        sender, config.allowlist_senders, config.allowlist_domains, config
    )


def is_allowlisted_automated_sender(sender: str, config: InboxFilterConfig) -> bool:
    """Return True when all extracted sender addresses are allowlisted and not denylisted.

    Args:
        sender: Raw RFC 2822 sender header or bare email address.
        config: InboxFilterConfig containing automated allowlist and denylist rules.

    Returns:
        True when extracted addresses are non-empty and all match allowlist without denylist hits.
    """
    return _matches_allowlist_addrs(
        sender, config.allowlist_automated_senders, config.allowlist_automated_domains, config
    )


def has_allowlisted_automated_attachment(msg: EmailMessage, config: InboxFilterConfig) -> bool:
    """Return True when msg is from an allowlisted automated sender with a document attachment.

    Args:
        msg: Normalized incoming EmailMessage.
        config: InboxFilterConfig containing automated allowlist and denylist rules.

    Returns:
        True when sender is allowlisted and at least one document attachment is present.
    """
    return is_allowlisted_automated_sender(msg.sender, config) and has_eligible_document_attachment(
        msg
    )


class EligibilityDecision(BaseModel):
    """Result of evaluating deterministic 0-token inbox eligibility rules on an email."""

    model_config = ConfigDict(frozen=True)

    eligible: bool
    reason: str


class InboxEligibilityPolicy:
    """Apply 6 zero-token deterministic pre-filters before waking any LLM on new emails."""

    def __init__(self, config: InboxFilterConfig) -> None:
        """Initialize the inbox eligibility policy.

        Args:
            config: InboxFilterConfig specifying calendar bypass, category skip, and denylists.
        """
        self._config = config

    def evaluate(
        self,
        msg: EmailMessage,
        owner_addresses: frozenset[str] = frozenset(),
    ) -> EligibilityDecision:
        """Evaluate whether msg should enter cognitive triage or be bypassed at 0 tokens.

        Args:
            msg: Normalized incoming EmailMessage.
            owner_addresses: Authenticated user email addresses exempt from bulk/category skip.

        Returns:
            EligibilityDecision indicating eligible=True or deterministic skip reason.
        """
        labels = set(msg.label_ids)
        if labels and "INBOX" not in labels:
            return EligibilityDecision(eligible=False, reason="NOT_IN_INBOX")

        if self._config.bypass_calendar_invites and self._is_calendar_invite_or_rsvp(msg):
            return EligibilityDecision(eligible=False, reason="BYPASS_CALENDAR_INVITE")

        sender_addrs = _extract_valid_emails(msg.sender)
        owner_set = {a.lower() for a in owner_addresses}
        is_owner = bool(sender_addrs) and all(addr in owner_set for addr in sender_addrs)

        if self._is_denylisted(msg.sender):
            return EligibilityDecision(eligible=False, reason="SKIP_DENYLIST")

        exempt_signal = (
            is_allowlisted_priority_sender(msg.sender, self._config)
            or has_dated_booking_signal(msg)
            or has_allowlisted_automated_attachment(msg, self._config)
        )
        if (
            self._config.skip_promotions_social_forums
            and not is_owner
            and not SKIPPED_GMAIL_CATEGORIES.isdisjoint(labels)
            and not exempt_signal
        ):
            return EligibilityDecision(eligible=False, reason="SKIP_GMAIL_CATEGORY")

        if not is_owner and self._is_automated_bulk_message(msg) and not exempt_signal:
            return EligibilityDecision(eligible=False, reason="SKIP_BULK_AUTOMATED")

        return EligibilityDecision(eligible=True, reason="ELIGIBLE")

    def coalesce_by_thread(
        self,
        messages: list[EmailMessage],
    ) -> tuple[list[EmailMessage], list[EmailMessage]]:
        """Keep only the latest message per Gmail threadId within a single polling batch.

        Args:
            messages: Ordered list of candidate incoming EmailMessage items.

        Returns:
            Tuple of (latest_messages_to_evaluate, superseded_earlier_messages).
        """
        last_idx_by_thread: dict[str, int] = {}
        for idx, msg in enumerate(messages):
            key = msg.thread_id.strip() or msg.message_id
            last_idx_by_thread[key] = idx

        keep_indices = set(last_idx_by_thread.values())
        selected = [m for idx, m in enumerate(messages) if idx in keep_indices]
        superseded = [m for idx, m in enumerate(messages) if idx not in keep_indices]
        return selected, superseded

    def _is_calendar_invite_or_rsvp(self, msg: EmailMessage) -> bool:
        """Detect .ics attachments, text/calendar parts, or Google Calendar notifications."""
        if any(mt.lower() in CALENDAR_MIMES for mt in msg.mime_types):
            return True
        if any(fn.lower().endswith(".ics") for fn in msg.attachment_filenames):
            return True
        if "calendar-notification@google.com" in msg.sender.lower():
            return True
        if any(k.strip().lower().startswith("x-google-calendar-") for k in msg.headers):
            return True
        return bool(CALENDAR_SUBJECT_RE.match(msg.subject))

    def _is_denylisted(self, sender_norm: str) -> bool:
        """Return True if any address in sender_norm matches configured inbox denylists."""
        return _matches_denylist_addrs(_extract_valid_emails(sender_norm), self._config)

    def _is_automated_bulk_message(self, msg: EmailMessage) -> bool:
        """Detect mailing-list and automated bulk headers (RFC 3834 / List-Unsubscribe)."""
        lower_hdrs = {k.strip().lower(): v.strip() for k, v in msg.headers.items()}
        precedence = lower_hdrs.get("precedence", "").lower()
        if precedence in BULK_PRECEDENCE_VALUES:
            return True
        auto_sub = lower_hdrs.get("auto-submitted", "").lower()
        if auto_sub and auto_sub != "no":
            return True
        return bool(lower_hdrs.get("list-unsubscribe") or lower_hdrs.get("list-id"))
