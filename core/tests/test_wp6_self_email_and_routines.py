"""WP6 TDD suite for self-sent Gmail capture, RoutineService systemd timers, and agent tools."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from collections.abc import Callable
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from alfred.controllers.cli_controller import CliController
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    CalendarEventItem,
    DriveMetadataIndex,
    EmailMessage,
    InboxOutputType,
    NounouContract,
    ScheduledWorkday,
)
from alfred.domain.routine_models import RoutinesConfig
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.evolve_worktree_service import EvolveWorktreeService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.nounou_service import NounouService
from alfred.services.pajemploi_calculator import PajemploiCalculator
from alfred.services.routine_service import RoutineService


def _raw_gmail_msg(
    msg_id: str,
    thread_id: str,
    sender: str,
    to_addr: str,
    subject: str,
    labels: list[str],
    body_b64: str = "Qm9uam91cg==",
) -> dict[str, object]:
    """Build a synthetic Gmail users.messages.get full payload dictionary."""
    return {
        "id": msg_id,
        "threadId": thread_id,
        "snippet": subject,
        "labelIds": labels,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": to_addr},
                {"name": "Subject", "value": subject},
            ],
            "mimeType": "text/plain",
            "body": {"data": body_b64},
        },
    }


def _sample_contract() -> NounouContract:
    """Build a valid synthetic NounouContract for routine scheduling tests."""
    return NounouContract(
        contract_id="c_routine",
        employee_name="Assistante Exemple",
        child_name="Enfant Exemple",
        calendar_id="cal_nounou_1",
        hourly_rate_net=4.5,
        base_monthly_net_salary=630.0,
        base_monthly_hours=140,
        base_monthly_days=14,
        maintenance_allowance_daily=3.8,
        meal_allowance_daily=4.5,
        vacation_notice_days=30,
        scheduled_workdays=[
            ScheduledWorkday(weekday="MONDAY", start_time="08:30", end_time="18:30", hours=10.0)
        ],
    )


def _build_routine_fixture(
    tmp_path: Path,
    gmail_client: MagicMock | None = None,
    drive_client: MagicMock | None = None,
    cal_client: MagicMock | None = None,
    systemctl_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[RoutineService, CliController, StateStore]:
    """Assemble RoutineService, CliController, and StateStore over mocked Google clients."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths).model_copy(
        update={"routines": RoutinesConfig(spawn_agy=False, cognitive_agy=False)}
    )
    store = StateStore(paths)
    knowledge = KnowledgeService(paths, settings, store)
    drive = DriveService(paths, settings, store, drive_client=drive_client or MagicMock())
    gmail = GmailService(paths, settings, gmail_client=gmail_client or MagicMock())
    cal = CalendarService(paths, settings, calendar_client=cal_client or MagicMock())
    agy = AgySessionManager(paths, settings, store)
    inbox = InboxTriageService(store, gmail, knowledge, drive, agy)
    nounou = NounouService(store, cal, knowledge, settings)
    paj = PajemploiCalculator()
    evolve = EvolveWorktreeService(paths, settings, store)
    routines = RoutineService(
        paths, settings, store, inbox, drive, nounou, paj, cal, agy, systemctl_runner
    )
    cli = CliController(
        knowledge,
        drive,
        inbox,
        nounou,
        paj,
        cal,
        evolve,
        store,
        gmail_service=gmail,
        settings=settings,
        agy_manager=agy,
        routine_service=routines,
    )
    return routines, cli, store


def test_gmail_captures_self_sent_inbox_and_archived_self_sent(tmp_path: Path) -> None:
    """Verify self-sent emails (SENT+INBOX) are captured even if archived before fetch."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    client = MagicMock()
    users = client.users.return_value
    users.history.return_value.list.return_value.execute.return_value = {
        "history": [
            {
                "messagesAdded": [
                    {"message": {"id": "m_self_in", "labelIds": ["UNREAD", "SENT", "INBOX"]}},
                    {"message": {"id": "m_self_arch", "labelIds": ["UNREAD", "SENT", "INBOX"]}},
                    {"message": {"id": "m_out_only", "labelIds": ["SENT"]}},
                    {"message": {"id": "m_draft", "labelIds": ["DRAFT", "INBOX"]}},
                ]
            }
        ],
        "historyId": "500",
    }
    payload_by_id = {
        "m_self_in": _raw_gmail_msg(
            "m_self_in",
            "th1",
            "owner@example.com",
            "owner@example.com",
            "Note perso",
            ["UNREAD", "SENT", "INBOX"],
        ),
        "m_self_arch": _raw_gmail_msg(
            "m_self_arch",
            "th2",
            "alias@example.org",
            "alias@example.org, spouse@example.com",
            "Sorties week-end",
            ["UNREAD", "SENT"],
        ),
        "m_out_only": _raw_gmail_msg(
            "m_out_only", "th3", "owner@example.com", "third@example.net", "Envoi", ["SENT"]
        ),
    }
    users.messages.return_value.get.side_effect = lambda **kw: MagicMock(
        execute=lambda i=str(kw["id"]): payload_by_id[i]
    )
    svc = GmailService(paths=paths, settings=settings, gmail_client=client)
    msgs, latest_id = svc.list_new_messages_since("450")
    assert latest_id == "500"
    assert [m.message_id for m in msgs] == ["m_self_in", "m_self_arch"]


def test_gmail_authenticated_addresses_search_thread_and_restore_inbox(tmp_path: Path) -> None:
    """Verify get_authenticated_addresses, search_messages, get_thread_messages, and restore."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    client = MagicMock()
    users = client.users.return_value
    users.getProfile.return_value.execute.return_value = {
        "emailAddress": "Owner@Example.com",
        "historyId": "10",
    }
    users.settings.return_value.sendAs.return_value.list.return_value.execute.return_value = {
        "sendAs": [{"sendAsEmail": "Alias@Example.org"}, {"sendAsEmail": "invalid"}]
    }
    raw_m1 = _raw_gmail_msg(
        "m_s1", "th_s1", "a@example.com", "owner@example.com", "Sujet 1", ["INBOX"]
    )
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m_s1"}]
    }
    users.messages.return_value.get.return_value.execute.return_value = raw_m1
    users.threads.return_value.get.return_value.execute.return_value = {
        "id": "th_s1",
        "messages": [raw_m1],
    }

    svc = GmailService(paths=paths, settings=settings, gmail_client=client)
    addrs = svc.get_authenticated_addresses()
    assert addrs == frozenset({"owner@example.com", "alias@example.org"})
    assert svc.get_authenticated_addresses() == addrs
    assert users.getProfile.call_count == 1
    assert svc.search_messages("subject:Sujet", max_results=5)[0].message_id == "m_s1"
    assert svc.get_thread_messages("th_s1")[0].thread_id == "th_s1"
    svc.restore_inbox_label("m_s1")
    users.messages.return_value.modify.assert_called_with(
        userId="me", id="m_s1", body={"addLabelIds": ["INBOX"]}
    )


def test_inbox_triage_self_memo_and_family_cc_never_draft_to_self(tmp_path: Path) -> None:
    """Verify self-sent memos and family-copied self-emails never create self-reply drafts."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    client = MagicMock()
    users = client.users.return_value
    users.getProfile.return_value.execute.return_value = {"emailAddress": "owner@example.com"}
    users.settings.return_value.sendAs.return_value.list.return_value.execute.return_value = {
        "sendAs": [{"sendAsEmail": "alias@example.org"}]
    }
    gmail = GmailService(paths, settings, gmail_client=client)
    knowledge = KnowledgeService(paths, settings, store)
    drive = DriveService(paths, settings, store, drive_client=MagicMock())
    agy = AgySessionManager(paths, settings, store)
    triage = InboxTriageService(store, gmail, knowledge, drive, agy)

    memo = EmailMessage(
        message_id="m_memo",
        thread_id="th_memo",
        sender="Owner <alias@example.org>",
        recipient="Owner <owner@example.com>",
        subject="Penser au vaccin",
        body_text="Prendre RDV pediatre la semaine prochaine.",
    )
    dec_memo = triage.process_message(memo)
    assert dec_memo.output_type == InboxOutputType.USER_ACTION_REQUIRED
    assert dec_memo.starred is True and dec_memo.draft_id is None

    family = EmailMessage(
        message_id="m_fam",
        thread_id="th_fam",
        sender="Owner <alias@example.org>",
        recipient="Owner <alias@example.org>, Spouse <spouse@example.com>",
        subject="Idees sorties en famille ce week-end",
        body_text="Voici trois idees d'activites pour samedi et dimanche.",
    )
    dec_fam = triage.process_message(family)
    assert dec_fam.output_type == InboxOutputType.USER_ACTION_REQUIRED
    assert dec_fam.starred is True and dec_fam.draft_id is None
    users.drafts.return_value.create.assert_not_called()

    silent_triage = InboxTriageService(
        store,
        gmail,
        knowledge,
        drive,
        agy,
        cognitive_fn=lambda _m, _d: (InboxOutputType.SILENT_NO_ACTION, "FYI", None),
    )
    dec_fyi = silent_triage.process_message(family.model_copy(update={"message_id": "m_fyi"}))
    assert dec_fyi.output_type == InboxOutputType.SILENT_NO_ACTION and dec_fyi.starred is False


def test_agy_workspace_cwd_and_ephemeral_classifier_purge(tmp_path: Path) -> None:
    """Verify workspace cwd resolution and SQLite/brain cleanup after classify_email_with_agy."""
    paths = AlfredPaths.from_env(tmp_path)
    inbox_ws = paths.workspaces_dir / "alfred-inbox"
    inbox_ws.mkdir(parents=True)
    agy_data = tmp_path / "agy_data"
    (agy_data / "conversations").mkdir(parents=True)
    (agy_data / "brain" / "conv_tmp_1").mkdir(parents=True)
    (agy_data / "conversations" / "conv_tmp_1.db").write_text("db", encoding="utf-8")
    summary_db = agy_data / "conversation_summaries.db"
    conn = sqlite3.connect(summary_db)
    conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO conversation_summaries VALUES ('conv_tmp_1')")
    conn.commit()
    conn.close()

    captured: list[list[str]] = []

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Return classifier envelope with ephemeral conversation_id."""
        captured.append(cmd)
        envelope = {
            "conversation_id": "conv_tmp_1",
            "result": {"output_type": "SILENT_NO_ACTION", "reason": "Newsletter"},
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(envelope), "")

    agy = AgySessionManager(
        paths,
        AlfredSettings.load(paths),
        StateStore(paths),
        runner_fn=_runner,
        agy_locator_fn=lambda: "/bin/agy",
        antigravity_data_dir=agy_data,
    )
    assert agy.resolve_workspace_cwd("alfred-inbox") == inbox_ws
    msg = EmailMessage(
        message_id="m1", thread_id="t1", sender="owner@example.com", subject="Hi", body_text="?"
    )
    out, _, _ = agy.classify_email_with_agy(
        msg, [], user_addresses=frozenset({"owner@example.com"})
    )
    assert out == InboxOutputType.SILENT_NO_ACTION
    assert "--disable-slash-commands" in captured[0] and "owner@example.com" in captured[0][6]
    assert not (agy_data / "conversations" / "conv_tmp_1.db").exists()
    assert not (agy_data / "brain" / "conv_tmp_1").exists()


def test_routine_service_periodic_seed_and_pajemploi_idempotence(tmp_path: Path) -> None:
    """Verify periodic routines skip without contract and run seed/Pajemploi once per period."""
    drive_client = MagicMock()
    drive_client.changes.return_value.list.return_value.execute.return_value = {
        "changes": [],
        "newStartPageToken": "tok_next",
    }
    cal_client = MagicMock()
    cal_client.events.return_value.list.return_value.execute.return_value = {"items": []}
    routines, _, store = _build_routine_fixture(
        tmp_path, drive_client=drive_client, cal_client=cal_client
    )

    uninit_tick = routines.run_routines("periodic", date(2026, 11, 20))
    uninit = {j.job_name: j.status for j in uninit_tick.jobs}
    assert uninit["drive_delta_sync"] == "SKIPPED_UNINITIALIZED"
    assert uninit["nounou_monthly_seed"] == "SKIPPED_NO_CONTRACT"

    store.drive_index.save(DriveMetadataIndex(start_page_token="tok_init"))
    store.nounou_contract.save(_sample_contract())
    s1 = {j.job_name: j.status for j in routines.run_routines("periodic", date(2026, 11, 20)).jobs}
    s2 = {j.job_name: j.status for j in routines.run_routines("periodic", date(2026, 11, 20)).jobs}
    assert s1["nounou_monthly_seed"] == "OK" and s2["nounou_monthly_seed"] == "ALREADY_COMPLETED"

    p1 = {j.job_name: j.status for j in routines.run_routines("periodic", date(2026, 2, 25)).jobs}
    p2 = {j.job_name: j.status for j in routines.run_routines("periodic", date(2026, 2, 25)).jobs}
    assert p1["pajemploi_monthly_close"] == "OK"
    assert p2["pajemploi_monthly_close"] == "ALREADY_COMPLETED"


def test_routine_service_vacation_notice_and_lock_contention(tmp_path: Path) -> None:
    """Verify upcoming vacation event notice Gate idempotence and flock contention skip."""
    cal_client = MagicMock()
    vac_ev = CalendarEventItem(
        event_id="ev_vac_99",
        summary="Vacances Nounou Hiver",
        start_iso="2026-12-05",
        end_iso="2026-12-12",
    )
    cal_client.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": vac_ev.event_id,
                "summary": vac_ev.summary,
                "start": {"date": vac_ev.start_iso},
                "end": {"date": vac_ev.end_iso},
            }
        ]
    }
    routines, _, store = _build_routine_fixture(tmp_path, cal_client=cal_client)
    store.nounou_contract.save(_sample_contract())

    v1 = {j.job_name: j.status for j in routines.run_routines("periodic", date(2026, 11, 15)).jobs}
    v2 = {j.job_name: j.status for j in routines.run_routines("periodic", date(2026, 11, 16)).jobs}
    assert v1["nounou_vacation_notice"] == "OK"
    assert v2["nounou_vacation_notice"] == "ALREADY_COMPLETED"

    with store.acquire_routine_lock("alfred_inbox") as held:
        assert held is True
        assert routines.run_routines(scope="inbox").locked_skip is True


def test_routine_systemd_install_and_cli_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify systemd timer installation and CLI routine/Gmail read-only commands."""
    gmail_client = MagicMock()
    users = gmail_client.users.return_value
    users.getProfile.return_value.execute.return_value = {"historyId": "99"}
    raw_msg = _raw_gmail_msg(
        "m_cli", "th_cli", "a@example.com", "owner@example.com", "Test CLI", ["INBOX"]
    )
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": "m_cli"}]
    }
    users.messages.return_value.get.return_value.execute.return_value = raw_msg
    users.threads.return_value.get.return_value.execute.return_value = {
        "id": "th_cli",
        "messages": [raw_msg],
    }
    sys_cmds: list[list[str]] = []
    routines, cli, _ = _build_routine_fixture(
        tmp_path,
        gmail_client=gmail_client,
        systemctl_runner=lambda cmd: (
            sys_cmds.append(cmd) or subprocess.CompletedProcess(cmd, 0, "active\n", "")
        ),
    )

    unit_dir = tmp_path / "systemd_user"
    written = routines.install_systemd_timers(systemd_user_dir=unit_dir, enable_now=True)
    assert len(written) == 4 and any("daemon-reload" in c for c in sys_cmds)
    assert "Persistent=true" in (unit_dir / "alfred-inbox.timer").read_text(encoding="utf-8")

    assert cli.run(["run-routines", "--only", "inbox"]) == 0
    capsys.readouterr()
    assert cli.run(["run-routines", "--only", "periodic", "--today", "2026-11-11"]) == 0
    capsys.readouterr()
    assert cli.run(["install-routines", "--systemd-dir", str(unit_dir), "--no-enable"]) == 0
    capsys.readouterr()
    assert cli.run(["status-routines", "--systemd-dir", str(unit_dir)]) == 0
    assert json.loads(capsys.readouterr().out)["systemd_timers_active"]["alfred-inbox.timer"]
    assert cli.run(["search-gmail", "--query", "subject:Test", "--max-results", "3"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["message_id"] == "m_cli"
    assert cli.run(["read-thread", "--thread-id", "th_cli"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["thread_id"] == "th_cli"


def test_agent_manifests_declare_explicit_tools_and_gmail_cli() -> None:
    """Verify all 5 .agents/agents/*.md manifests declare tools and Gmail CLI docs."""
    repo_root = Path(__file__).resolve().parents[2]
    agents_dir = repo_root / ".agents" / "agents"
    required_tools_by_agent = {
        "research-drive.md": {"read_file", "run_command"},
        "research-gmail-cal.md": {"read_file", "run_command"},
        "research-web.md": {"search_web", "read_file"},
        "plan-synthesizer.md": {"read_file", "write_file", "edit_file", "run_command"},
        "plan-executor.md": {"read_file", "write_file", "edit_file", "run_command"},
    }
    for file_name, expected_subset in required_tools_by_agent.items():
        raw = (agents_dir / file_name).read_text(encoding="utf-8")
        parts = raw.split("---", 2)
        assert len(parts) == 3, f"Invalid frontmatter in {file_name}"
        meta = yaml.safe_load(parts[1])
        assert isinstance(meta, dict)
        tools = meta.get("tools")
        assert isinstance(tools, list) and expected_subset.issubset(set(tools)), file_name
        if file_name == "plan-synthesizer.md":
            assert meta.get("mainAgent") is True

    gmail_cal_body = (agents_dir / "research-gmail-cal.md").read_text(encoding="utf-8")
    assert "search-gmail" in gmail_cal_body and "read-thread" in gmail_cal_body
