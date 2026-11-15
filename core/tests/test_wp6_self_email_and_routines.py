"""WP6 TDD suite for self-sent Gmail capture, V8 RoutineService timers/gates, and agent tools."""

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
from alfred.domain.feature_models import FeatureFlagsConfig
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    DriveMetadataIndex,
    EmailMessage,
    InboxOutputType,
)
from alfred.domain.routine_models import RoutinesConfig
from alfred.main import build_controller
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
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


def _build_routine_fixture(
    tmp_path: Path,
    settings_override: AlfredSettings | None = None,
    gmail_client: MagicMock | None = None,
    drive_client: MagicMock | None = None,
    systemctl_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[RoutineService, CliController, StateStore]:
    """Assemble V8 RoutineService, CliController, and StateStore over mocked Google clients."""
    paths = AlfredPaths.from_env(tmp_path)
    base_cfg = AlfredSettings.load(paths).model_copy(
        update={"routines": RoutinesConfig(spawn_agy=False, cognitive_agy=False)}
    )
    settings = settings_override or base_cfg
    ctrl = build_controller(str(tmp_path))
    store = StateStore(paths)
    knowledge = KnowledgeService(paths, settings, store)
    drive = DriveService(paths, settings, store, drive_client=drive_client or MagicMock())
    gmail = GmailService(paths, settings, gmail_client=gmail_client or MagicMock())
    agy = AgySessionManager(paths, settings, store)
    inbox = InboxTriageService(store, gmail, knowledge, drive, agy, settings=settings)
    routines = RoutineService(
        paths,
        settings,
        store,
        inbox,
        drive,
        agy,
        systemctl_runner=systemctl_runner,
    )
    ctrl._knowledge = knowledge
    ctrl._drive = drive
    ctrl._gmail = gmail
    ctrl._agy = agy
    ctrl._inbox = inbox
    ctrl._routines = routines
    return routines, ctrl, store


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
            "alias@domaine-perso.example",
            "alias@domaine-perso.example, spouse@example.com",
            "Sorties week-end",
            ["UNREAD", "SENT"],
        ),
        "m_out_only": _raw_gmail_msg(
            "m_out_only", "th3", "owner@example.com", "third@example.com", "Envoi", ["SENT"]
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
        "sendAs": [{"sendAsEmail": "Alias@domaine-perso.example"}, {"sendAsEmail": "invalid"}]
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
    assert addrs == frozenset({"owner@example.com", "alias@domaine-perso.example"})
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
        "sendAs": [{"sendAsEmail": "alias@domaine-perso.example"}]
    }
    gmail = GmailService(paths, settings, gmail_client=client)
    knowledge = KnowledgeService(paths, settings, store)
    drive = DriveService(paths, settings, store, drive_client=MagicMock())
    agy = AgySessionManager(paths, settings, store)
    triage = InboxTriageService(store, gmail, knowledge, drive, agy, settings=settings)

    memo = EmailMessage(
        message_id="m_memo",
        thread_id="th_memo",
        sender="Owner <alias@domaine-perso.example>",
        recipient="Owner <owner@example.com>",
        subject="Penser au vaccin",
        body_text="Prendre RDV pediatre la semaine prochaine.",
        label_ids=["INBOX"],
    )
    dec_memo = triage.process_message(memo)
    assert dec_memo.output_type == InboxOutputType.USER_ACTION_REQUIRED
    assert dec_memo.starred is True and dec_memo.draft_id is None

    family = EmailMessage(
        message_id="m_fam",
        thread_id="th_fam",
        sender="Owner <alias@domaine-perso.example>",
        recipient="Owner <alias@domaine-perso.example>, Spouse <spouse@example.com>",
        subject="Idees sorties en famille ce week-end",
        body_text="Voici trois idees d'activites pour samedi et dimanche.",
        label_ids=["INBOX"],
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
        settings=settings,
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
    prompt_idx = captured[0].index("--print") + 1
    assert "--disable-slash-commands" in captured[0]
    assert "owner@example.com" in captured[0][prompt_idx]
    assert not (agy_data / "conversations" / "conv_tmp_1.db").exists()
    assert not (agy_data / "brain" / "conv_tmp_1").exists()


def _write_sample_tax_extension(extensions_dir: Path) -> None:
    """Write a compliant sample AlfredExtension plugin into extensions_dir."""
    extensions_dir.mkdir(parents=True, exist_ok=True)
    (extensions_dir / "tax_reminder.py").write_text(
        '"""Test extension."""\n\n'
        "from alfred.domain.extension_protocol import ExtensionContext, ExtensionJobOutcome\n\n\n"
        "class TaxReminderExtension:\n"
        '    """Annual tax reminder."""\n\n'
        "    @property\n"
        "    def extension_id(self) -> str:\n"
        '        """Return ID."""\n'
        '        return "tax_reminder"\n\n'
        "    @property\n"
        "    def description(self) -> str:\n"
        '        """Return summary."""\n'
        '        return "Tax check"\n\n'
        "    def validate_config(self, raw_config: dict[str, object]) -> list[str]:\n"
        '        """Validate config.\n\n'
        "        Args:\n"
        "            raw_config: Raw config.\n"
        '        """\n'
        "        return []\n\n"
        "    def run_periodic(self, context: ExtensionContext) -> list[ExtensionJobOutcome]:\n"
        '        """Run periodic tick.\n\n'
        "        Args:\n"
        "            context: Execution context.\n"
        '        """\n'
        "        return [\n"
        "            ExtensionJobOutcome(\n"
        '                extension_id="tax_reminder",\n'
        '                job_name="tax_check",\n'
        '                status="OK",\n'
        '                summary=context.today_iso,\n'
        "            )\n"
        "        ]\n",
        encoding="utf-8",
    )


def test_routine_service_feature_gates_extensions_and_drive_backup(tmp_path: Path) -> None:
    """Verify V8 RoutineService feature gates, instance_extensions, and instance_drive_backup."""
    disabled_cfg = AlfredSettings(
        features=FeatureFlagsConfig(inbox_auto_triage=False, drive_knowledge_sync=False),
        routines=RoutinesConfig(spawn_agy=False, cognitive_agy=False),
    )
    dis_routines, _, _ = _build_routine_fixture(tmp_path / "dis", settings_override=disabled_cfg)
    dis_inbox = {j.job_name: j.status for j in dis_routines.run_routines("inbox").jobs}
    assert dis_inbox["inbox_poll"] == "SKIPPED_DISABLED"
    dis_per = {
        j.job_name: j.status for j in dis_routines.run_routines("periodic", date(2026, 11, 20)).jobs
    }
    assert dis_per["drive_delta_sync"] == "SKIPPED_DISABLED"
    assert dis_per["instance_drive_backup"] == "SKIPPED_DISABLED"

    drive_client = MagicMock()
    drive_client.changes.return_value.list.return_value.execute.return_value = {
        "changes": [],
        "newStartPageToken": "tok_next",
    }
    drive_client.files.return_value.list.return_value.execute.return_value = {"files": []}
    drive_client.files.return_value.create.return_value.execute.return_value = {"id": "fid_bak"}

    act_dir = tmp_path / "active"
    paths = AlfredPaths.from_env(act_dir)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path.write_text('project_name: "Alfred Test"\n', encoding="utf-8")
    _write_sample_tax_extension(paths.extensions_dir)

    routines, _, store = _build_routine_fixture(act_dir, drive_client=drive_client)
    uninit = {
        j.job_name: j.status for j in routines.run_routines("periodic", date(2026, 11, 20)).jobs
    }
    assert uninit["drive_delta_sync"] == "SKIPPED_UNINITIALIZED"
    assert "nounou_monthly_seed" not in uninit and "pajemploi_monthly_close" not in uninit

    store.drive_index.save(DriveMetadataIndex(start_page_token="tok_init"))
    tick = {
        j.job_name: j.status for j in routines.run_routines("periodic", date(2026, 11, 20)).jobs
    }
    assert tick["drive_delta_sync"] == "OK"
    assert tick["instance_extensions"] == "OK"
    assert tick["instance_drive_backup"] == "OK"
    assert "agy_ui_sync" in tick

    with store.acquire_routine_lock("alfred_inbox") as held:
        assert held is True
        assert routines.run_routines(scope="inbox").locked_skip is True


def test_routine_systemd_conditional_inbox_timer_and_cli_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify conditional alfred-inbox.timer installation and CLI routine/Gmail commands."""
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

    def _sys_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Record systemctl command and return active status."""
        sys_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "active\n", "")

    routines, cli, _ = _build_routine_fixture(
        tmp_path,
        gmail_client=gmail_client,
        systemctl_runner=_sys_runner,
    )

    unit_dir = tmp_path / "systemd_user"
    written = routines.install_systemd_timers(systemd_user_dir=unit_dir, enable_now=True)
    assert len(written) == 4 and any("daemon-reload" in c for c in sys_cmds)
    assert "Persistent=true" in (unit_dir / "alfred-inbox.timer").read_text(encoding="utf-8")

    no_inbox_cfg = AlfredSettings(features=FeatureFlagsConfig(inbox_auto_triage=False))
    no_inbox_routines, _, _ = _build_routine_fixture(
        tmp_path / "no_inbox", settings_override=no_inbox_cfg
    )
    unit_dir_no_inbox = tmp_path / "systemd_no_inbox"
    written_no_inbox = no_inbox_routines.install_systemd_timers(
        systemd_user_dir=unit_dir_no_inbox, enable_now=False
    )
    assert not (unit_dir_no_inbox / "alfred-inbox.timer").exists()
    assert (unit_dir_no_inbox / "alfred-periodic.timer").is_file()
    assert len(written_no_inbox) == 2

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
