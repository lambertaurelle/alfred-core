"""Swarm adversarial red-team tests for allowlist spoofing, PDF crashes, and Reply-To leaks."""

from __future__ import annotations

import io
from typing import Any

import pytest
from pypdf import PdfWriter

from alfred.domain.attachment_models import CognitiveTriageDirectives
from alfred.domain.calendar_models import (
    CalendarActionIntent,
    CalendarSlotProposal,
    CalendarTriageAction,
)
from alfred.domain.feature_models import InboxFilterConfig
from alfred.domain.models import EmailMessage, InboxOutputType
from alfred.services import attachment_extractor
from alfred.services.attachment_extractor import extract_attachment_text
from alfred.services.email_security_service import InboxCapabilityFirewall
from alfred.services.inbox_booking_detector import is_passive_or_automated_email
from alfred.services.inbox_calendar_bridge import (
    enrich_calendar_action_attendees,
    extract_correspondent_emails,
)
from alfred.services.inbox_eligibility import InboxEligibilityPolicy
from alfred.services.inbox_plan_renderer import finalize_cognitive_triage_decision


def _blank_pdf_bytes() -> bytes:
    """Create a valid 1-page blank PDF with zero extractable text."""
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def test_adversarial_multi_address_and_group_from_header_spoofing() -> None:
    """Reject multi-address From headers and RFC 2822 group syntax mixing evil addresses."""
    from alfred.services.inbox_eligibility import is_allowlisted_priority_sender

    cfg = InboxFilterConfig.from_dict(
        {
            "allowlist_senders": ["scheduler@vendor.example"],
            "allowlist_domains": ["vendor.example"],
        }
    )
    policy = InboxEligibilityPolicy(cfg)

    spoof_vectors = (
        "Trusted <scheduler@vendor.example>, Attacker <attacker@evil.example>",
        "Group: scheduler@vendor.example, attacker@evil.example;",
        "scheduler@vendor.example <attacker@evil.example>",
        "Empty Group: ;",
    )
    for raw_from in spoof_vectors:
        assert is_allowlisted_priority_sender(raw_from, cfg) is False
        forum_msg = EmailMessage(
            message_id="m_spoof_cat",
            thread_id="th_spoof",
            sender=raw_from,
            recipient="owner@example.com",
            subject="Urgent schedule",
            headers={"List-Unsubscribe": "<https://evil.example/unsub>"},
            label_ids=["INBOX", "CATEGORY_FORUMS"],
        )
        assert policy.evaluate(forum_msg).eligible is False
        assert policy.evaluate(forum_msg).reason == "SKIP_GMAIL_CATEGORY"

        bulk_msg = forum_msg.model_copy(update={"label_ids": ["INBOX"]})
        assert policy.evaluate(bulk_msg).eligible is False
        assert policy.evaluate(bulk_msg).reason == "SKIP_BULK_AUTOMATED"


def test_adversarial_denylist_strictly_overrides_priority_allowlist() -> None:
    """Ensure denylist_senders and denylist_domains always override priority allowlist_*."""
    from alfred.services.inbox_eligibility import is_allowlisted_priority_sender

    cfg_sender_deny = InboxFilterConfig.from_dict(
        {
            "allowlist_domains": ["vendor.example"],
            "denylist_senders": ["compromised@vendor.example"],
        }
    )
    msg_comp = EmailMessage(
        message_id="m_comp",
        thread_id="th_comp",
        sender="Vendor <compromised@vendor.example>",
        recipient="owner@example.com",
        subject="Notice",
        headers={"List-Unsubscribe": "<https://vendor.example/u>"},
        label_ids=["INBOX", "CATEGORY_FORUMS"],
    )
    assert is_allowlisted_priority_sender(msg_comp.sender, cfg_sender_deny) is False
    verdict_s = InboxEligibilityPolicy(cfg_sender_deny).evaluate(msg_comp)
    assert verdict_s.eligible is False and verdict_s.reason == "SKIP_DENYLIST"
    assert (
        is_passive_or_automated_email(msg_comp, "unsubscribe", inbox_filters=cfg_sender_deny)
        is True
    )

    cfg_domain_deny = InboxFilterConfig.from_dict(
        {
            "allowlist_senders": ["bot@spam.vendor.example"],
            "allowlist_domains": ["vendor.example"],
            "denylist_domains": ["spam.vendor.example"],
        }
    )
    msg_dom = msg_comp.model_copy(update={"sender": "Bot <bot@spam.vendor.example>"})
    assert is_allowlisted_priority_sender(msg_dom.sender, cfg_domain_deny) is False
    verdict_d = InboxEligibilityPolicy(cfg_domain_deny).evaluate(msg_dom)
    assert verdict_d.eligible is False and verdict_d.reason == "SKIP_DENYLIST"


def test_adversarial_lookalike_and_suffix_domain_spoofing() -> None:
    """Reject lookalike suffix/prefix domains attempting to match vendor.example."""
    from alfred.services.inbox_eligibility import is_allowlisted_priority_sender

    cfg = InboxFilterConfig.from_dict({"allowlist_domains": ["vendor.example"]})
    policy = InboxEligibilityPolicy(cfg)

    for spoof_addr in (
        "spoof@notvendor.example",
        "spoof@evil-vendor.example",
        "spoof@vendor.example.evil.org",
        "vendor.example@evil.org",
    ):
        assert is_allowlisted_priority_sender(spoof_addr, cfg) is False
        msg = EmailMessage(
            message_id="m_lookalike",
            thread_id="th_lookalike",
            sender=spoof_addr,
            recipient="owner@example.com",
            subject="Schedule",
            headers={"Precedence": "bulk"},
            label_ids=["INBOX"],
        )
        assert policy.evaluate(msg).reason == "SKIP_BULK_AUTOMATED"


def test_adversarial_corrupted_blank_and_crashing_pdf_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure corrupted bytes, blank pages, and crashing extractors never raise uncaught errors."""
    assert extract_attachment_text(
        b"%PDF-1.4\x00garbage\xff\xfe", "corrupt.pdf", "application/pdf"
    ) == ("corrupted_pdf", "", False)

    assert extract_attachment_text(
        _blank_pdf_bytes(), "blank.pdf", "application/pdf"
    ) == ("empty_or_scanned_pdf", "", False)

    with pytest.raises(ValueError):
        extract_attachment_text(b"%PDF-1.4", "neg.pdf", "application/pdf", max_chars=-1)

    class _CrashingPage:
        """Adversarial page stub raising RuntimeError on layout and ValueError on plain extract."""

        def extract_text(self, **kwargs: Any) -> str:
            """Raise distinct exceptions on layout and plain extraction calls."""
            if kwargs:
                raise RuntimeError("layout engine crash")
            raise ValueError("malformed content stream")

    class _CrashingReader:
        """Reader wrapper returning _CrashingPage."""

        def __init__(self, _stream: Any) -> None:
            """Expose single crashing page."""
            self.pages = [_CrashingPage()]

    monkeypatch.setattr(attachment_extractor, "PdfReader", _CrashingReader)
    assert extract_attachment_text(b"%PDF-crash", "crash.pdf", "application/pdf") == (
        "corrupted_pdf",
        "",
        False,
    )


def _assert_bulk_reply_to_vector_blocked(msg: EmailMessage) -> None:
    """Verify a bulk message yields zero fallback attendees and converts DRAFT_REPLY."""
    owners = frozenset({"owner@example.com"})
    firewall = InboxCapabilityFirewall()
    empty_slot = CalendarSlotProposal(
        summary="Household Activity",
        start_iso="2026-12-18T16:00:00+01:00",
        end_iso="2026-12-18T17:00:00+01:00",
        attendees=[],
        send_invites=True,
    )
    household_slot = empty_slot.model_copy(update={"attendees": ["alice@example.com"]})

    assert extract_correspondent_emails(msg, owners, frozenset()) == []

    act_empty = CalendarTriageAction(intent=CalendarActionIntent.CONFIRM_EVENT, slots=[empty_slot])
    res_empty = enrich_calendar_action_attendees(act_empty, msg, owners, frozenset(), [])
    assert res_empty.slots[0].attendees == []

    act_hh = CalendarTriageAction(
        intent=CalendarActionIntent.CONFIRM_EVENT, slots=[household_slot]
    )
    res_hh = enrich_calendar_action_attendees(act_hh, msg, owners, frozenset(), [])
    assert res_hh.slots[0].attendees == ["alice@example.com"]

    out_t, _, draft, _, _, _, _ = finalize_cognitive_triage_decision(
        msg=msg,
        out_type=InboxOutputType.DRAFT_REPLY,
        reason="LLM proposed draft",
        draft="Thanks!",
        cal_action=CalendarTriageAction(),
        conv_id="conv-adv",
        needs_orch=False,
        matched_pat=None,
        is_from_self=False,
        use_cognitive_agy=True,
        has_cognitive_fn=False,
        directives=CognitiveTriageDirectives(),
        last_needs_orch=False,
        pinned_recipient=firewall.pin_draft_recipient(msg),
    )
    assert out_t == InboxOutputType.USER_ACTION_REQUIRED
    assert draft is None


def test_adversarial_bulk_reply_to_self_echo_and_automated_reply_to_bypass() -> None:
    """Block Calendar leaks and DRAFT_REPLY on self-echo or noreply Reply-To bulk headers."""
    self_echo_msg = EmailMessage(
        message_id="m_echo",
        thread_id="th_echo",
        sender="Provider <contact@provider.example>",
        recipient="owner@example.com",
        subject="Activity booking",
        headers={
            "Reply-To": "Provider <contact@provider.example>",
            "List-Unsubscribe": "<mailto:u@provider.example>",
        },
    )
    noreply_rt_msg = EmailMessage(
        message_id="m_noreply_rt",
        thread_id="th_noreply_rt",
        sender="Provider <contact@provider.example>",
        recipient="owner@example.com",
        subject="Activity booking",
        headers={
            "Reply-To": "noreply@provider.example",
            "Precedence": "bulk",
        },
    )
    _assert_bulk_reply_to_vector_blocked(self_echo_msg)
    _assert_bulk_reply_to_vector_blocked(noreply_rt_msg)
