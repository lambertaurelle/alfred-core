"""WP2 TDD test suite for alfred-inbox new email routine with 3 exclusive outputs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from alfred.domain.models import AlfredPaths, AlfredSettings, EmailMessage, InboxOutputType
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService


def _build_inbox_stack(
    tmp_path: Path,
) -> tuple[InboxTriageService, StateStore, MagicMock, list[str]]:
    """Wire InboxTriageService with mocked Gmail/Drive clients and local knowledge templates."""
    paths = AlfredPaths.from_env(tmp_path)
    paths.templates_knowledge_dir.mkdir(parents=True, exist_ok=True)
    for tpl in Path("/root/Alfred/core/templates/knowledge").glob("*.md"):
        (paths.templates_knowledge_dir / tpl.name).write_text(
            tpl.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    gmail_client = MagicMock()
    drive_client = MagicMock()
    archived_ids: list[str] = []

    knowledge = KnowledgeService(paths=paths, settings=settings, state_store=store)
    knowledge.bootstrap_templates_if_empty()
    drive = DriveService(
        paths=paths, settings=settings, state_store=store, drive_client=drive_client
    )
    gmail = GmailService(paths=paths, settings=settings, gmail_client=gmail_client)
    agy = AgySessionManager(
        paths=paths,
        settings=settings,
        state_store=store,
        archive_hook=archived_ids.append,
    )
    triage = InboxTriageService(
        state_store=store,
        gmail_service=gmail,
        knowledge_service=knowledge,
        drive_service=drive,
        agy_manager=agy,
    )
    return triage, store, gmail_client, archived_ids


def test_first_run_bookmarks_current_history_id_without_touching_past(tmp_path: Path) -> None:
    """Verify first poll records current Gmail historyId and processes 0 historical emails."""
    triage, store, gmail_client, _ = _build_inbox_stack(tmp_path)
    gmail_client.users.return_value.getProfile.return_value.execute.return_value = {
        "historyId": "88000"
    }

    decisions = triage.poll_new_messages()
    assert decisions == []
    assert store.bookmarks.load().gmail_history_id == "88000"
    gmail_client.users.return_value.history.assert_not_called()

    # Second poll processes newly arrived message once and skips duplicates on third poll
    users = gmail_client.users.return_value
    users.history.return_value.list.return_value.execute.return_value = {
        "history": [{"messagesAdded": [{"message": {"id": "m_2", "labelIds": ["INBOX"]}}]}],
        "historyId": "88010",
    }
    users.messages.return_value.get.return_value.execute.return_value = {
        "id": "m_2",
        "threadId": "th_2",
        "snippet": "Newsletter",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "no-reply@news.example.com"},
                {"name": "Subject", "value": "Newsletter hebdo"},
            ],
            "mimeType": "text/plain",
            "body": {},
        },
    }
    second = triage.poll_new_messages()
    assert len(second) == 1
    assert second[0].output_type == InboxOutputType.SILENT_NO_ACTION
    assert triage.poll_new_messages() == []


def test_silent_no_action_for_passive_portal_and_newsletter(tmp_path: Path) -> None:
    """Verify Generali passive portal notices and newsletters trigger 0 Gmail edits + archive."""
    triage, store, gmail_client, archived_ids = _build_inbox_stack(tmp_path)

    generali_msg = EmailMessage(
        message_id="msg_generali_1",
        thread_id="th_gen",
        sender="ne-pas-repondre@portail.example.com",
        subject="Vous avez de nouveaux documents réglementaires sur votre espace Generali",
        body_text=(
            "Bonjour, un nouveau document réglementaire est disponible sur votre espace client."
        ),
    )
    decision = triage.process_message(generali_msg)
    assert decision.output_type == InboxOutputType.SILENT_NO_ACTION
    assert decision.starred is False
    assert decision.draft_id is None
    assert decision.session_archived is True
    assert decision.session_id in archived_ids
    gmail_client.users.return_value.drafts.assert_not_called()
    gmail_client.users.return_value.messages.assert_not_called()

    invoice_msg = EmailMessage(
        message_id="msg_sub_invoice",
        thread_id="th_inv",
        sender="facturation@telecom.example.com",
        subject="Votre facture mensuelle d'abonnement est disponible",
        body_text="Montant prélevé automatiquement sans anomalie conformément à votre contrat.",
        headers={"list-unsubscribe": "<mailto:unsub@example.com>"},
    )
    inv_decision = triage.process_message(invoice_msg)
    assert inv_decision.output_type == InboxOutputType.SILENT_NO_ACTION
    assert inv_decision.session_archived is True
    assert store.has_processed_item("gmail", "msg_generali_1")


def test_draft_reply_creates_gmail_draft_and_active_plan_gate(tmp_path: Path) -> None:
    """Verify conversational email creates Gmail draft and keeps Antigravity session active."""
    triage, store, gmail_client, archived_ids = _build_inbox_stack(tmp_path)
    gmail_client.users.return_value.drafts.return_value.create.return_value.execute.return_value = {
        "id": "draft_42"
    }

    human_msg = EmailMessage(
        message_id="msg_human_1",
        thread_id="th_human_1",
        sender="ecole@example.com",
        subject="Question sur la réunion parents",
        body_text="Bonjour, pouvez-vous confirmer votre présence jeudi à 18h00 ? Merci.",
    )
    decision = triage.process_message(human_msg)
    assert decision.output_type == InboxOutputType.DRAFT_REPLY
    assert decision.draft_id == "draft_42"
    assert decision.draft_body is not None
    assert decision.starred is False
    assert decision.session_archived is False
    assert archived_ids == []
    assert decision.implementation_plan_markdown is not None
    assert "draft_42" in decision.implementation_plan_markdown
    assert decision.session_id is not None
    session = store.conversations.load().sessions[decision.session_id]
    assert session.status == "ACTIVE"
    assert Path(session.plan_path).exists()


def test_user_action_required_stars_email_and_builds_research_brief(tmp_path: Path) -> None:
    """Verify action-required email is STARRED in Gmail and produces a thorough research brief."""
    triage, store, gmail_client, archived_ids = _build_inbox_stack(tmp_path)

    action_msg = EmailMessage(
        message_id="msg_action_1",
        thread_id="th_action_1",
        sender="urssaf-pajemploi@example.com",
        subject="Action requise : déclaration mensuelle Pajemploi à valider avant échéance",
        body_text="Merci de compléter votre démarche administrative et vérifier vos pièces.",
    )
    decision = triage.process_message(action_msg)
    assert decision.output_type == InboxOutputType.USER_ACTION_REQUIRED
    assert decision.starred is True
    assert decision.draft_id is None
    assert decision.session_archived is False
    assert archived_ids == []
    gmail_client.users.return_value.messages.return_value.modify.assert_called_once()
    assert decision.implementation_plan_markdown is not None
    assert "Défrichage" in decision.implementation_plan_markdown
    assert len(decision.research_brief) >= 1
    assert decision.session_id is not None
    assert store.conversations.load().sessions[decision.session_id].status == "ACTIVE"

    # Urgent anomaly from a no-reply address MUST still route to USER_ACTION_REQUIRED
    urgent_noreply = EmailMessage(
        message_id="msg_anomaly_1",
        thread_id="th_anom_1",
        sender="no-reply@assurance.example.com",
        subject="Impayé : rejet de prélèvement sur votre échéance",
        body_text="Merci de régulariser votre moyen de paiement.",
    )
    anom_decision = triage.process_message(urgent_noreply)
    assert anom_decision.output_type == InboxOutputType.USER_ACTION_REQUIRED
    assert anom_decision.starred is True


def test_expired_history_id_recovery_and_reply_to_threading(tmp_path: Path) -> None:
    """Verify poll-inbox resets expired historyId (404) cleanly and honors Reply-To headers."""
    from alfred.domain.models import DriveEntry, DriveMetadataIndex

    triage, store, gmail_client, _ = _build_inbox_stack(tmp_path)
    store.bookmarks.update(lambda b: b.model_copy(update={"gmail_history_id": "10"}))

    class _Fake404Error(Exception):
        """Simulated HTTP 404 error."""

        status_code = 404

    users = gmail_client.users.return_value
    users.history.return_value.list.return_value.execute.side_effect = _Fake404Error
    users.getProfile.return_value.execute.return_value = {"historyId": "99999"}

    assert triage.poll_new_messages() == []
    assert store.bookmarks.load().gmail_history_id == "99999"

    # Populate Drive index so USER_ACTION_REQUIRED brief includes accented Drive file matches
    store.drive_index.save(
        DriveMetadataIndex(
            start_page_token="t1",
            entries={
                "d1": DriveEntry(
                    file_id="d1",
                    name="Déclaration_Pajemploi_2026.pdf",
                    mime_type="application/pdf",
                    path="/Admin/Déclaration_Pajemploi_2026.pdf",
                )
            },
        )
    )
    act = triage.process_message(
        EmailMessage(
            message_id="m_drv",
            thread_id="th_drv",
            sender="portail@example.com",
            subject="Action requise : declaration Pajemploi",
            body_text="Document a signer.",
        )
    )
    assert any("d1" in item for item in act.research_brief)

    users.drafts.return_value.create.return_value.execute.return_value = {"id": "dr_rt"}
    reply_dec = triage.process_message(
        EmailMessage(
            message_id="m_rt",
            thread_id="th_rt",
            sender="portail@ecole.example.com",
            subject="Sortie scolaire",
            body_text="Pouvez-vous accompagner la classe ?",
            headers={"reply-to": "directeur@ecole.example.com", "message-id": "<id1@ecole>"},
        )
    )
    assert reply_dec.output_type == InboxOutputType.DRAFT_REPLY
    assert "directeur@ecole.example.com" in (reply_dec.implementation_plan_markdown or "")
