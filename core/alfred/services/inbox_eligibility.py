"""Deterministic 0-token pre-filtering and thread coalescence for alfred-inbox."""

from __future__ import annotations

import re
from email.utils import parseaddr

from pydantic import BaseModel, ConfigDict

from alfred.domain.feature_models import InboxFilterConfig
from alfred.domain.models import EmailMessage

CALENDAR_SUBJECT_RE = re.compile(
    r"^\s*(?:(?:re|fwd|tr)\s*:\s*)*"
    r"(?:invitation|updated\s+invitation|canceled\s+event|cancelled\s+event|"
    r"accepted|declined|tentative|accept[eé]|refus[eé]|peut-[eê]tre|"
    r"annul[eé]|mise\s+[aà]\s+jour\s+de\s+l'invitation)\s*:",
    re.IGNORECASE,
)
CALENDAR_MIMES: frozenset[str] = frozenset({"text/calendar", "application/ics"})
SKIPPED_GMAIL_CATEGORIES: frozenset[str] = frozenset(
    {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}
)
BULK_PRECEDENCE_VALUES: frozenset[str] = frozenset({"bulk", "list", "junk", "auto_reply"})


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

        _, bare_sender = parseaddr(msg.sender)
        sender_norm = bare_sender.strip().lower()
        is_owner = sender_norm in {a.lower() for a in owner_addresses}

        if self._is_denylisted(sender_norm):
            return EligibilityDecision(eligible=False, reason="SKIP_DENYLIST")

        if (
            self._config.skip_promotions_social_forums
            and not is_owner
            and not SKIPPED_GMAIL_CATEGORIES.isdisjoint(labels)
        ):
            return EligibilityDecision(eligible=False, reason="SKIP_GMAIL_CATEGORY")

        if not is_owner and self._is_automated_bulk_message(msg):
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
        """Return True if sender email or domain matches configured inbox denylists."""
        if not sender_norm:
            return False
        if sender_norm in {s.strip().lower() for s in self._config.denylist_senders}:
            return True
        domain = sender_norm.partition("@")[2]
        domains_deny = {d.strip().lower().lstrip("@") for d in self._config.denylist_domains}
        return bool(domain and domain in domains_deny)

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
