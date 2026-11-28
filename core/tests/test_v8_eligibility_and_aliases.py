"""V8 TDD tests for InboxEligibilityPolicy, sendAs aliases, CalendarService, Agy, and Drive map."""

from __future__ import annotations

import base64
import email
import sqlite3
from email.header import decode_header, make_header
from email.message import EmailMessage as MimeEmailMessage
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from alfred.domain.calendar_models import CalendarSlotProposal
from alfred.domain.feature_models import IdentityConfig, InboxFilterConfig
from alfred.domain.models import (
    ActiveConversationsState,
    ActiveSessionRecord,
    AlfredPaths,
    AlfredSettings,
    CalendarEventItem,
    DriveEntry,
    DriveMetadataIndex,
    EmailMessage,
)
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_eligibility import InboxEligibilityPolicy


def _verdict_code(verdict: object) -> str:
    """Extract canonical eligibility code from a string, StrEnum, or decision object."""
    if isinstance(verdict, str):
        return verdict
    for attr in ("reason", "code", "status", "outcome"):
        val = getattr(verdict, attr, None)
        if isinstance(val, str) and val:
            return val
    return str(verdict)


def _is_eligible(verdict: object) -> bool:
    """Return True when an eligibility verdict permits normal inbox triage."""
    elig_attr = getattr(verdict, "eligible", None)
    return elig_attr if isinstance(elig_attr, bool) else _verdict_code(verdict) == "ELIGIBLE"


def _decode_raw_mime_from(raw_b64: str) -> str:
    """Decode an RFC 2822 base64url raw Gmail payload and return its decoded From header."""
    padded = raw_b64 + "=" * (-len(raw_b64) % 4)
    raw_from = email.message_from_bytes(base64.urlsafe_b64decode(padded)).get("From", "")
    return str(make_header(decode_header(raw_from))) if raw_from else ""


class _FakeCalendarApi:
    """In-memory Google Calendar v3 fake supporting calendarList, events, and freebusy."""

    def __init__(self) -> None:
        """Initialize calendar list, event store, and freebusy responses."""
        self.insert_calls: list[dict[str, Any]] = []
        self.update_calls: list[dict[str, Any]] = []
        self.events_store: dict[str, dict[str, Any]] = {}

    def calendarList(self) -> Any:  # noqa: N802
        """Return fake calendarList resource."""
        items = [
            {"id": "primary@example.com", "summary": "Agenda Principal", "primary": True},
            {"id": "famille_cal_42@group.calendar.google.com", "summary": "Famille"},
        ]
        mock_res = MagicMock()
        mock_res.list.return_value.execute.return_value = {"items": items}
        return mock_res

    def _insert_event(self, **kwargs: Any) -> Any:
        """Store inserted event and return executable mock."""
        ev_id = f"ev_{len(self.insert_calls) + 1}"
        stored = {"id": ev_id, **dict(kwargs["body"])}
        self.events_store[ev_id] = stored
        self.insert_calls.append(kwargs)
        return MagicMock(execute=lambda: stored)

    def _update_event(self, **kwargs: Any) -> Any:
        """Store updated or patched event and return executable mock."""
        ev_id = str(kwargs["eventId"])
        merged = {**self.events_store.get(ev_id, {}), "id": ev_id, **dict(kwargs["body"])}
        self.events_store[ev_id] = merged
        self.update_calls.append(kwargs)
        return MagicMock(execute=lambda: merged)

    def events(self) -> Any:
        """Return fake events resource recording inserts, updates, patches, lists, and deletes."""
        res = MagicMock()
        res.insert.side_effect = self._insert_event
        res.update.side_effect = self._update_event
        res.patch.side_effect = self._update_event
        res.get.side_effect = lambda **kw: MagicMock(
            execute=lambda: self.events_store.get(str(kw["eventId"]), {"id": str(kw["eventId"])})
        )
        res.list.side_effect = lambda **_kw: MagicMock(
            execute=lambda: {"items": list(self.events_store.values())}
        )
        res.delete.side_effect = lambda **kw: MagicMock(
            execute=lambda: self.events_store.pop(str(kw["eventId"]), {})
        )
        return res

    def _query_freebusy(self, **_kwargs: Any) -> Any:
        """Return two synthetic busy intervals across primary and Famille calendars."""
        payload = {
            "calendars": {
                "primary@example.com": {
                    "busy": [{"start": "2026-11-20T09:00:00Z", "end": "2026-11-20T10:00:00Z"}]
                },
                "famille_cal_42@group.calendar.google.com": {
                    "busy": [{"start": "2026-11-20T14:00:00Z", "end": "2026-11-20T15:30:00Z"}]
                },
            }
        }
        return MagicMock(execute=lambda: payload)

    def freebusy(self) -> Any:
        """Return fake freebusy resource."""
        res = MagicMock()
        res.query.side_effect = self._query_freebusy
        return res


def test_inbox_eligibility_bypasses_calendar_invites_and_non_inbox() -> None:
    """InboxEligibilityPolicy bypasses .ics, text/calendar, Google notifications, and non-INBOX."""
    policy = InboxEligibilityPolicy(InboxFilterConfig())
    base = EmailMessage(
        message_id="m_base", thread_id="th_base",
        sender="contact@example.com", subject="Point projet", label_ids=["INBOX"],
    )
    assert _is_eligible(policy.evaluate(base))
    for upd in (
        {"attachment_filenames": ["invite.ics"]},
        {"mime_types": ["text/calendar"]},
        {"sender": "calendar-notification@google.com"},
    ):
        assert _verdict_code(policy.evaluate(base.model_copy(update=upd))) == (
            "BYPASS_CALENDAR_INVITE"
        )
    for prefix in (
        "Invitation: Point", "Accepté: Point", "Accepted: Sync",
        "Declined: Sync", "Updated invitation: Point", "Canceled event: Point",
    ):
        assert _verdict_code(policy.evaluate(base.model_copy(update={"subject": prefix}))) == (
            "BYPASS_CALENDAR_INVITE"
        )
    assert _verdict_code(
        policy.evaluate(base.model_copy(update={"label_ids": ["CATEGORY_UPDATES"]}))
    ) == "NOT_IN_INBOX"


def test_inbox_eligibility_categories_bulk_denylist_and_coalesce() -> None:
    """Policy skips promos/social/forums/bulk/denylist, exempts owners, and coalesces threads."""
    cfg = InboxFilterConfig(
        denylist_senders=["spammer@example.com"], denylist_domains=["bruit.example.com"]
    )
    policy = InboxEligibilityPolicy(cfg)
    owners = frozenset({"owner@domaine-perso.example"})

    for cat in ("CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"):
        ext_cat = EmailMessage(
            message_id=f"m_{cat}", thread_id="t_cat",
            sender="boutique@example.com", subject="Offre", label_ids=["INBOX", cat],
        )
        assert _verdict_code(policy.evaluate(ext_cat, owner_addresses=owners)) == (
            "SKIP_GMAIL_CATEGORY"
        )
        own_cat = ext_cat.model_copy(update={"sender": "Owner <owner@domaine-perso.example>"})
        assert _is_eligible(policy.evaluate(own_cat, owner_addresses=owners))

    bulk_ext = EmailMessage(
        message_id="m_bulk", thread_id="t_bulk", sender="digest@example.com",
        subject="Digest", label_ids=["INBOX"], headers={"Precedence": "bulk"},
    )
    assert _verdict_code(policy.evaluate(bulk_ext, owner_addresses=owners)) == (
        "SKIP_BULK_AUTOMATED"
    )
    own_bulk = bulk_ext.model_copy(update={"sender": "owner@domaine-perso.example"})
    assert _is_eligible(policy.evaluate(own_bulk, owner_addresses=owners))

    deny_s = bulk_ext.model_copy(update={"headers": {}, "sender": "Spam <spammer@example.com>"})
    deny_d = bulk_ext.model_copy(update={"headers": {}, "sender": "info@bruit.example.com"})
    assert _verdict_code(policy.evaluate(deny_s)) == "SKIP_DENYLIST"
    assert _verdict_code(policy.evaluate(deny_d)) == "SKIP_DENYLIST"

    msgs = [
        EmailMessage(
            message_id=mid, thread_id=tid, sender="a@example.com", subject=mid, label_ids=["INBOX"]
        )
        for mid, tid in (("m1", "th_1"), ("m2", "th_2"), ("m3", "th_1"))
    ]
    raw_coal: object = policy.coalesce_by_thread(msgs)
    kept = raw_coal[0] if isinstance(raw_coal, tuple) else raw_coal
    assert isinstance(kept, list)
    assert {m.message_id for m in kept} == {"m2", "m3"}


def test_gmail_service_list_send_as_and_create_draft_from_header(tmp_path: Path) -> None:
    """GmailService lists sendAs aliases and injects From header on create_draft."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings(
        identity=IdentityConfig(
            default_send_as_email="contact@domaine-perso.example",
            default_send_as_display_name="Alex Bureau",
        )
    )
    client = MagicMock()
    users = client.users.return_value
    users.settings.return_value.sendAs.return_value.list.return_value.execute.return_value = {
        "sendAs": [
            {"sendAsEmail": "primary@example.com", "displayName": "Alex", "isPrimary": True},
            {"sendAsEmail": "contact@domaine-perso.example", "displayName": "Alex Bureau"},
        ]
    }
    users.drafts.return_value.create.return_value.execute.return_value = {"id": "dr_alias_1"}

    svc = GmailService(paths=paths, settings=settings, gmail_client=client)
    identities = svc.list_send_as_identities()
    assert len(identities) == 2 and "contact@domaine-perso.example" in str(identities)

    assert svc.create_draft("th_alias_1", "dest@example.com", "Devis", "Bonjour") == "dr_alias_1"
    create_from = _decode_raw_mime_from(
        users.drafts.return_value.create.call_args.kwargs["body"]["message"]["raw"]
    )
    assert "contact@domaine-perso.example" in create_from and "Alex Bureau" in create_from


def test_gmail_service_send_draft_enforces_from_header(tmp_path: Path) -> None:
    """GmailService enforces default_send_as_email + display_name From header on send_draft."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings(
        identity=IdentityConfig(
            default_send_as_email="contact@domaine-perso.example",
            default_send_as_display_name="Alex Bureau",
        )
    )
    client = MagicMock()
    users = client.users.return_value
    orig_mime = MimeEmailMessage()
    orig_mime["From"], orig_mime["To"], orig_mime["Subject"] = (
        "primary@example.com",
        "dest@example.com",
        "Devis",
    )
    orig_mime.set_content("Bonjour")
    orig_raw = base64.urlsafe_b64encode(orig_mime.as_bytes()).decode("ascii")
    users.drafts.return_value.get.return_value.execute.return_value = {
        "id": "dr_alias_1",
        "message": {
            "id": "m_dr_1",
            "threadId": "th_alias_1",
            "raw": orig_raw,
            "payload": {
                "headers": [
                    {"name": "From", "value": "primary@example.com"},
                    {"name": "To", "value": "dest@example.com"},
                    {"name": "Subject", "value": "Devis"},
                ],
                "mimeType": "text/plain",
                "body": {"data": base64.urlsafe_b64encode(b"Bonjour").decode("ascii")},
            },
        },
    }
    users.drafts.return_value.update.return_value.execute.return_value = {"id": "dr_alias_1"}
    users.drafts.return_value.send.return_value.execute.return_value = {"id": "sent_alias_1"}

    svc = GmailService(paths=paths, settings=settings, gmail_client=client)
    assert svc.send_draft("dr_alias_1") == "sent_alias_1"
    upd_call = users.drafts.return_value.update.call_args
    send_call = users.drafts.return_value.send.call_args
    raw_sent = (
        upd_call.kwargs["body"]["message"]["raw"]
        if upd_call is not None
        else send_call.kwargs.get("body", {}).get("message", {}).get("raw", "")
    )
    assert raw_sent
    send_from = _decode_raw_mime_from(raw_sent)
    assert "contact@domaine-perso.example" in send_from and "Alex Bureau" in send_from


def _build_sanitized_cal_service(
    tmp_path: Path,
) -> tuple[CalendarService, _FakeCalendarApi, list[str], frozenset[str]]:
    """Build CalendarService with self/blocked identity rules and candidate attendee fixture."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings(
        primary_calendar_id="primary@example.com",
        identity=IdentityConfig(
            default_send_as_email="alias@domaine-perso.example",
            calendar_organizer_email="primary@example.com",
            never_invite_emails=["never@domaine-perso.example"],
        ),
    )
    fake_api = _FakeCalendarApi()
    svc = CalendarService(paths, settings, calendar_client=fake_api, state_store=StateStore(paths))
    svc.set_hold_allowlist_entry("partenaire@example.com")
    svc.set_hold_allowlist_entry("never@domaine-perso.example")
    raw_guests = [
        "alias@domaine-perso.example",
        "primary@example.com",
        "never@domaine-perso.example",
        "auth-owner@domaine-perso.example",
        "partenaire@example.com",
        "externe@example.com",
    ]
    return svc, fake_api, raw_guests, frozenset({"auth-owner@domaine-perso.example"})


def test_calendar_service_strips_self_aliases_on_insert_and_update_rrule(tmp_path: Path) -> None:
    """CalendarService strips self/blocked emails and preserves RRULE on insert and update."""
    svc, fake_api, raw_guests, owners = _build_sanitized_cal_service(tmp_path)
    assert svc.sanitize_guest_attendees(raw_guests, owner_addresses=owners) == [
        "partenaire@example.com",
        "externe@example.com",
    ]

    rrule = ["RRULE:FREQ=WEEKLY;COUNT=3"]
    ev_id = svc.insert_event(
        "primary@example.com",
        CalendarEventItem(
            summary="Cours",
            start_iso="2026-11-20T10:00:00+01:00",
            end_iso="2026-11-20T11:00:00+01:00",
            attendees=raw_guests,
            recurrence=rrule,
        ),
        owner_addresses=owners,
    )
    ins_body = fake_api.insert_calls[-1]["body"]
    assert ins_body["recurrence"] == rrule
    assert {a["email"] for a in ins_body["attendees"]} == {
        "partenaire@example.com",
        "externe@example.com",
    }

    svc.update_event(
        "primary@example.com",
        ev_id,
        summary="Cours Maj",
        start_iso="2026-11-20T10:30:00+01:00",
        end_iso="2026-11-20T11:30:00+01:00",
        attendees=raw_guests,
        recurrence=rrule,
        owner_addresses=owners,
    )
    assert fake_api.update_calls[-1]["body"]["recurrence"] == rrule
    assert {a["email"] for a in fake_api.update_calls[-1]["body"]["attendees"]} == {
        "partenaire@example.com",
        "externe@example.com",
    }


def test_calendar_service_strips_self_aliases_on_hold_confirm_and_freebusy(tmp_path: Path) -> None:
    """CalendarService strips self/blocked emails on HOLD/confirm and resolves FreeBusy."""
    svc, fake_api, raw_guests, owners = _build_sanitized_cal_service(tmp_path)
    slot = CalendarSlotProposal(
        summary="Point Notaire",
        start_iso="2026-11-22T14:00:00+01:00",
        end_iso="2026-11-22T15:00:00+01:00",
        attendees=raw_guests,
    )
    svc.create_hold_event("primary@example.com", slot, thread_id="th_n", owner_addresses=owners)
    assert {a["email"] for a in fake_api.insert_calls[-1]["body"]["attendees"]} == {
        "partenaire@example.com"
    }
    svc.confirm_event("primary@example.com", slot, thread_id="th_n", owner_addresses=owners)
    assert {a["email"] for a in fake_api.update_calls[-1]["body"]["attendees"]} == {
        "partenaire@example.com",
        "externe@example.com",
    }

    cals = svc.list_calendars()
    assert len(cals) == 2 and cals[0].primary is True
    assert svc.resolve_calendar_selector("Famille") == "famille_cal_42@group.calendar.google.com"
    assert svc.resolve_calendar_selector("primary") == "primary@example.com"

    cal_ids = ["primary@example.com", "famille_cal_42@group.calendar.google.com"]
    busy = svc.query_freebusy("2026-11-20T00:00:00Z", "2026-11-20T23:59:59Z", cal_ids)
    assert len(busy) == 2


def test_agy_model_cli_flags_and_repair_session_titles(tmp_path: Path) -> None:
    """AgySessionManager omits --effort on Carbon/Neon orchestrators and repairs titles."""
    paths = AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)
    agy_dir = tmp_path / "agy_data"
    (agy_dir / "conversations").mkdir(parents=True, exist_ok=True)
    conv_id = "abababab-1212-3434-5656-cdcdcdcdcdcd"
    (agy_dir / "conversations" / f"{conv_id}.db").write_bytes(b"sqlite")

    conn = sqlite3.connect(agy_dir / "conversation_summaries.db")
    try:
        conn.execute(
            "CREATE TABLE conversation_summaries ("
            "conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', "
            "project_id TEXT NOT NULL DEFAULT '')"
        )
        conn.execute("INSERT INTO conversation_summaries VALUES (?, '', 'p1')", (conv_id,))
        conn.commit()
    finally:
        conn.close()

    agy = AgySessionManager(paths, AlfredSettings(), store, antigravity_data_dir=agy_dir)
    for role in ("onboard", "life", "evolve", "alfred-onboard", "alfred-life", "alfred-evolve"):
        flags = agy.build_model_cli_flags(role)
        assert "--model" in flags and "gemini-pro-orchestrator" in flags and "--effort" not in flags

    for role in ("inbox", "calendar", "tasks", "alfred-inbox", "alfred-calendar", "alfred-tasks"):
        assert agy.build_model_cli_flags(role) == [
            "--model",
            "gemini-pro-orchestrator-neon",
        ]

    assert agy.build_model_cli_flags("alfred-inbox", trace=True) == [
        "--model",
        "gemini-pro-orchestrator-neon",
    ]

    plan_file = tmp_path / "plan.md"
    plan_file.write_text("# Renouvellement Assurance Habitation\n", encoding="utf-8")
    rec = ActiveSessionRecord(
        session_id=conv_id,
        workspace="alfred-inbox",
        status="ACTIVE",
        title="[INBOX] Renouvellement Assurance Habitation",
        outcome="USER_ACTION_REQUIRED: Assurance",
        plan_path=str(plan_file),
    )
    store.conversations.save(ActiveConversationsState(sessions={conv_id: rec}))
    assert agy.repair_session_titles()
    pbtxt = (agy_dir / "annotations" / f"{conv_id}.pbtxt").read_text(encoding="utf-8")
    assert "Renouvellement Assurance Habitation" in pbtxt and "archived:false" in pbtxt


def test_drive_build_family_office_discovery_map_skips_referentiel(tmp_path: Path) -> None:
    """build_family_office_discovery_map clusters domains and skips _ALFRED_REFERENTIEL_."""
    paths = AlfredPaths.from_env(tmp_path)
    store = StateStore(paths)
    entries = {
        "f_ref_dir": DriveEntry(
            file_id="f_ref_dir", name="_ALFRED_REFERENTIEL_",
            mime_type="application/vnd.google-apps.folder",
            path="/_ALFRED_REFERENTIEL_", is_folder=True,
        ),
        "f_ref_md": DriveEntry(
            file_id="f_ref_md", name="00_drive_taxonomy_and_filing_rules.md",
            mime_type="text/markdown", parents=["f_ref_dir"],
            path="/_ALFRED_REFERENTIEL_/00_drive_taxonomy_and_filing_rules.md",
        ),
        "f_fisc": DriveEntry(
            file_id="f_fisc", name="Avis_Imposition_2025.pdf",
            mime_type="application/pdf", path="/Fiscalite/Avis_Imposition_2025.pdf",
        ),
        "f_immo": DriveEntry(
            file_id="f_immo", name="Acte_Propriete_Maison.pdf",
            mime_type="application/pdf", path="/Immobilier/Acte_Propriete_Maison.pdf",
        ),
        "f_sante": DriveEntry(
            file_id="f_sante", name="Contrat_Mutuelle_Famille.pdf",
            mime_type="application/pdf", path="/Sante/Contrat_Mutuelle_Famille.pdf",
        ),
    }
    store.drive_index.save(
        DriveMetadataIndex(
            start_page_token="tok_1", folder_count=1, file_count=4,
            top_level_folders=["_ALFRED_REFERENTIEL_", "Fiscalite", "Immobilier", "Sante"],
            entries=entries,
        )
    )
    svc = DriveService(paths, AlfredSettings(), store, drive_client=MagicMock())
    serialized = str(svc.build_family_office_discovery_map())
    assert "_ALFRED_REFERENTIEL_" not in serialized
    assert "00_drive_taxonomy_and_filing_rules.md" not in serialized
    assert "Avis_Imposition_2025.pdf" in serialized
    assert "Acte_Propriete_Maison.pdf" in serialized
    assert "Contrat_Mutuelle_Famille.pdf" in serialized
