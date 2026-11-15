"""Round 2 TDD tests for subagent CLI subcommands, live agy edge cases, and V8 CLI docs."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from alfred.controllers.cli_controller import CliController
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    DriveEntry,
    DriveMetadataIndex,
    EmailMessage,
    InboxOutputType,
)
from alfred.main import build_controller
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService


def _make_cli_stack(
    tmp_path: Path,
    settings: AlfredSettings | None = None,
    drive_client: MagicMock | None = None,
    gmail_client: MagicMock | None = None,
    cal_client: MagicMock | None = None,
    agy_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[CliController, StateStore]:
    """Assemble a mocked CliController and StateStore for Round 2 CLI tests."""
    paths = AlfredPaths.from_env(tmp_path)
    if settings is not None:
        paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
        paths.settings_path.write_text(
            f'google:\n  primary_calendar_id: "{settings.primary_calendar_id}"\n',
            encoding="utf-8",
        )
    ctrl = build_controller(str(tmp_path))
    cfg = settings or AlfredSettings.load(paths)
    store = StateStore(paths)
    knowledge = KnowledgeService(paths, cfg, store)
    drive = DriveService(paths, cfg, store, drive_client=drive_client or MagicMock())
    gmail = GmailService(paths, cfg, gmail_client=gmail_client or MagicMock())
    cal = CalendarService(paths, cfg, calendar_client=cal_client or MagicMock(), state_store=store)
    agy = AgySessionManager(
        paths,
        cfg,
        store,
        runner_fn=agy_runner,
        agy_locator_fn=(lambda: "agy") if agy_runner is not None else None,
    )
    inbox = InboxTriageService(store, gmail, knowledge, drive, agy, settings=cfg)
    ctrl._knowledge = knowledge
    ctrl._drive = drive
    ctrl._gmail = gmail
    ctrl._calendar = cal
    ctrl._agy = agy
    ctrl._inbox = inbox
    return ctrl, store


def test_cli_load_and_upsert_knowledge_and_drive_subcommands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify load-knowledge, upsert-knowledge, search-drive, sync-drive, and read-drive."""
    drive_client = MagicMock()
    drive_client.changes.return_value.list.return_value.execute.return_value = {
        "changes": [],
        "newStartPageToken": "tok_2",
    }
    drive_client.files.return_value.get.return_value.execute.return_value = {
        "id": "f_1",
        "name": "Contrat.pdf",
        "mimeType": "text/plain",
        "modifiedTime": "2026-05-01T10:00:00Z",
    }
    drive_client.files.return_value.get_media.return_value.execute.return_value = b"Texte contrat"
    cli, store = _make_cli_stack(tmp_path, drive_client=drive_client)
    entry = DriveEntry(
        file_id="f_1", name="Contrat.pdf", mime_type="text/plain", path="/Famille/Contrat.pdf"
    )
    store.drive_index.save(DriveMetadataIndex(start_page_token="tok_0", entries={"f_1": entry}))

    body_path = tmp_path / "body.md"
    body_path.write_text("# Synthèse\nDétail contrat.\n", encoding="utf-8")
    ups_argv = [
        "upsert-knowledge",
        "--file",
        "05_test.md",
        "--id",
        "test_doc",
        "--title",
        "Titre",
        "--domain",
        "famille",
        "--summary",
        "Résumé",
        "--keyword",
        "contrat",
        "--drive-source-id",
        "f_1",
        "--body-file",
        str(body_path),
    ]
    assert cli.run(ups_argv) == 0
    assert json.loads(capsys.readouterr().out)["frontmatter"]["id"] == "test_doc"
    assert cli.run(["load-knowledge", "--doc", "test_doc"]) == 0
    assert json.loads(capsys.readouterr().out)["file_name"] == "05_test.md"
    assert cli.run(["search-drive", "--query", "contrat"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["file_id"] == "f_1"
    assert cli.run(["sync-drive"]) == 0
    synced = json.loads(capsys.readouterr().out)
    assert synced["start_page_token"] == "tok_2" and "folder_count" in synced
    assert cli.run(["read-drive", "--file-id", "f_1"]) == 0
    assert "Texte contrat" in capsys.readouterr().out


def test_cli_list_events_resolves_primary_summary_and_explicit_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify list-events resolves primary, calendar summary, and explicit cal_id."""
    cal_client = MagicMock()
    cal_client.calendarList.return_value.list.return_value.execute.return_value = {
        "items": [{"id": "fam_cal_42", "summary": "Famille"}]
    }
    cal_client.events.return_value.list.return_value.execute.return_value = {
        "items": [
            {
                "id": "ev_1",
                "summary": "Réunion Famille",
                "start": {"dateTime": "2026-05-04T08:30:00+02:00"},
                "end": {"dateTime": "2026-05-04T18:30:00+02:00"},
            }
        ]
    }
    settings = AlfredSettings(primary_calendar_id="my_primary_id")
    cli, _ = _make_cli_stack(tmp_path, settings=settings, cal_client=cal_client)
    rng = ["--from", "2026-05-01T00:00:00Z", "--to", "2026-05-31T23:59:59Z"]

    assert cli.run(["list-events", *rng]) == 0
    assert json.loads(capsys.readouterr().out)[0]["event_id"] == "ev_1"
    assert cli.run(["list-events", "--calendar", "Famille", *rng]) == 0
    capsys.readouterr()
    assert cli.run(["list-events", "--calendar", "explicit_cal_99", *rng]) == 0
    capsys.readouterr()

    called_ids = [
        c.kwargs["calendarId"] for c in cal_client.events.return_value.list.call_args_list
    ]
    assert called_ids == ["my_primary_id", "fam_cal_42", "explicit_cal_99"]


def test_cli_poll_inbox_spawn_agy_and_cognitive_agy_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify alfred poll-inbox --spawn-agy --cognitive-agy wires live agy CLI features."""
    gmail_client = MagicMock()
    users = gmail_client.users.return_value
    users.history.return_value.list.return_value.execute.return_value = {
        "history": [{"messagesAdded": [{"message": {"id": "m9", "labelIds": ["INBOX"]}}]}],
        "historyId": "11",
    }
    users.messages.return_value.get.return_value.execute.return_value = {
        "id": "m9",
        "threadId": "th9",
        "snippet": "Bonjour",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "contact@example.com"},
                {"name": "Subject", "value": "Question"},
            ],
            "mimeType": "text/plain",
            "body": {},
        },
    }
    users.drafts.return_value.create.return_value.execute.return_value = {"id": "dr9"}

    def _runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Simulate cognitive classification and live session conversation ID."""
        if "--print" in cmd:
            out = json.dumps({"output_type": "DRAFT_REPLY", "reason": "agy", "draft_body": "OK"})
            return subprocess.CompletedProcess(cmd, 0, out, "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"conversation_id": "conv_77"}), "")

    cli, store = _make_cli_stack(tmp_path, gmail_client=gmail_client, agy_runner=_runner)
    store.bookmarks.update(lambda b: b.model_copy(update={"gmail_history_id": "10"}))

    assert cli.run(["poll-inbox", "--spawn-agy", "--cognitive-agy"]) == 0
    decisions = json.loads(capsys.readouterr().out)
    assert decisions[0]["session_id"] == "conv_77"
    assert decisions[0]["draft_body"] == "OK"


def test_agy_edge_cases_envelopes_and_unsafe_conversation_id(tmp_path: Path) -> None:
    """Verify AgySessionManager unwraps nested JSON envelopes and blocks traversal IDs."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    msg = EmailMessage(
        message_id="m1", thread_id="t1", sender="a@example.com", subject="Hi", body_text="?"
    )

    agy_trav = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=lambda c: subprocess.CompletedProcess(
            c, 0, json.dumps({"result": {"conversationId": "../escape"}}), ""
        ),
        agy_locator_fn=lambda: "agy",
    )
    rec = agy_trav.create_session("alfred-inbox", "m1", "# Plan\n", spawn_live=True)
    assert rec.session_id.startswith("agy_alfred_inbox_")

    wrapped_stdout = "INFO starting\n{not_json\n" + json.dumps(
        {
            "result": json.dumps(
                {
                    "output_type": "USER_ACTION_REQUIRED",
                    "reason": "Signature",
                    "draft_body": None,
                }
            )
        }
    )
    agy_wrap = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=lambda c: subprocess.CompletedProcess(c, 0, wrapped_stdout, ""),
        agy_locator_fn=lambda: "agy",
    )
    out_type, reason, draft = agy_wrap.classify_email_with_agy(msg, [])
    assert out_type == InboxOutputType.USER_ACTION_REQUIRED and reason == "Signature"
    assert draft is None


def test_agy_error_and_malformed_json_fallbacks(tmp_path: Path) -> None:
    """Verify OSError, empty stdout, unknown output_type, and missing binary fallbacks."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    msg = EmailMessage(
        message_id="m1", thread_id="t1", sender="a@example.com", subject="Hi", body_text="?"
    )

    def _raise_os(_c: list[str]) -> subprocess.CompletedProcess[str]:
        """Raise OSError to simulate broken binary execution."""
        raise OSError("exec format error")

    agy_os = AgySessionManager(
        paths, settings, store, runner_fn=_raise_os, agy_locator_fn=lambda: "agy"
    )
    assert agy_os.classify_email_with_agy(msg, [])[0] == InboxOutputType.DRAFT_REPLY

    agy_empty = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=lambda c: subprocess.CompletedProcess(c, 0, "", ""),
        agy_locator_fn=lambda: "agy",
    )
    assert agy_empty.classify_email_with_agy(msg, [])[0] == InboxOutputType.DRAFT_REPLY

    agy_bad_enum = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=lambda c: subprocess.CompletedProcess(
            c, 0, json.dumps({"output_type": "BOGUS", "reason": "?"}), ""
        ),
        agy_locator_fn=lambda: "agy",
    )
    assert agy_bad_enum.classify_email_with_agy(msg, [])[0] == InboxOutputType.DRAFT_REPLY

    agy_unavail = AgySessionManager(paths, settings, store, agy_locator_fn=lambda: None)
    assert agy_unavail.classify_email_with_agy(msg, [])[0] == InboxOutputType.DRAFT_REPLY


def test_subagent_manifests_and_readme_document_v8_cli_commands() -> None:
    """Verify subagent manifests and README.md document V8 CLI commands without Nounou."""
    root = Path("/root/Alfred")
    r_drive = (root / ".agents/agents/research-drive.md").read_text(encoding="utf-8")
    for expected in (
        "scan-knowledge",
        "load-knowledge --doc",
        "search-drive --query",
        "sync-drive",
        "read-drive --file-id",
    ):
        assert expected in r_drive

    r_cal = (root / ".agents/agents/research-gmail-cal.md").read_text(encoding="utf-8")
    assert "list-events" in r_cal
    assert "--calendar nounou" not in r_cal

    p_exec = (root / ".agents/agents/plan-executor.md").read_text(encoding="utf-8")
    assert "upsert-knowledge --file" in p_exec
    assert "send-draft --draft-id" in p_exec

    tpl_names = {p.name for p in (root / "core/templates/knowledge").glob("*.md")}
    assert tpl_names <= {
        "00_drive_taxonomy_and_filing_rules.md",
        "01_profil_et_contacts_cles.md",
    }
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "alfred" in readme.lower()


def test_knowledge_empty_doc_guard_stem_and_stdin_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify load-knowledge rejects empty/dot args cleanly, resolves stems/id, and reads '-'."""
    cli, _ = _make_cli_stack(tmp_path)
    for invalid_doc in ("", "   ", ".", ".."):
        with pytest.raises(FileNotFoundError):
            cli.run(["load-knowledge", "--doc", invalid_doc])

    monkeypatch.setattr("sys.stdin", io.StringIO("# Fiscalité\nBarème validé.\n"))
    ups_argv = [
        "upsert-knowledge",
        "--file",
        "02_fiscalite_foyer.md",
        "--id",
        "fiscalite_foyer",
        "--title",
        "Fiscalité du Foyer",
        "--domain",
        "finance",
        "--summary",
        "Synthèse fiscale",
        "--body-file",
        "-",
    ]
    assert cli.run(ups_argv) == 0
    capsys.readouterr()
    assert cli.run(["load-knowledge", "--doc", "02_fiscalite_foyer"]) == 0
    assert json.loads(capsys.readouterr().out)["file_name"] == "02_fiscalite_foyer.md"
    assert cli.run(["load-knowledge", "--doc", "fiscalite_foyer"]) == 0
    assert json.loads(capsys.readouterr().out)["frontmatter"]["id"] == "fiscalite_foyer"


def test_list_events_normalizes_iso_dates_and_rejects_inverted_range(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify list-events expands plain ISO dates/naive datetimes and rejects inverted ranges."""
    cal_client = MagicMock()
    cal_client.events.return_value.list.return_value.execute.return_value = {"items": []}
    cli, _ = _make_cli_stack(tmp_path, cal_client=cal_client)

    date_argv = [
        "list-events",
        "--calendar",
        "primary",
        "--from",
        "2026-05-01",
        "--to",
        "2026-05-31",
    ]
    assert cli.run(date_argv) == 0
    capsys.readouterr()
    call_kw = cal_client.events.return_value.list.call_args.kwargs
    assert call_kw["calendarId"] == "primary"
    assert call_kw["timeMin"].startswith("2026-05-01T00:00:00+")
    assert call_kw["timeMax"].startswith("2026-05-31T23:59:59+")

    naive_argv = ["list-events", "--from", "2026-05-02T08:30:00", "--to", "2026-05-02T18:00:00"]
    assert cli.run(naive_argv) == 0
    capsys.readouterr()
    naive_kw = cal_client.events.return_value.list.call_args.kwargs
    assert naive_kw["timeMin"].startswith("2026-05-02T08:30:00+")

    with pytest.raises(ValueError, match="precedes"):
        cli.run(["list-events", "--from", "2026-05-10", "--to", "2026-05-01"])


def test_agy_multiline_json_error_status_and_binary_validation(tmp_path: Path) -> None:
    """Verify multi-line JSON parsing, ERROR status rejection, and executable binary checks."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    msg = EmailMessage(message_id="m1", thread_id="t1", sender="x@example.com", subject="S")

    pretty_stdout = (
        "WARN: refreshing session\n"
        + json.dumps(
            {
                "conversation_id": "conv_pretty_1",
                "status": "SUCCESS",
                "items": [{"nested": 1}],
                "structured_output": {
                    "output_type": "SILENT_NO_ACTION",
                    "reason": "Passive",
                    "draft_body": None,
                },
            },
            indent=2,
        )
        + "\n trailing note"
    )
    agy_ok = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=lambda c: subprocess.CompletedProcess(c, 0, pretty_stdout, ""),
        agy_locator_fn=lambda: "agy",
    )
    live_ok = agy_ok.create_session("alfred-inbox", "m1", "# Plan\n", spawn_live=True)
    assert live_ok.session_id == "conv_pretty_1"
    assert agy_ok.classify_email_with_agy(msg, [])[0] == InboxOutputType.SILENT_NO_ACTION

    err_json = json.dumps(
        {"conversation_id": "conv_err", "status": "ERROR", "output_type": "SILENT_NO_ACTION"}
    )
    agy_err = AgySessionManager(
        paths,
        settings,
        store,
        runner_fn=lambda c: subprocess.CompletedProcess(c, 0, err_json, ""),
        agy_locator_fn=lambda: "agy",
    )
    live_err = agy_err.create_session("alfred-inbox", "m2", "# Plan\n", spawn_live=True)
    assert live_err.session_id.startswith("agy_")
    assert agy_err.classify_email_with_agy(msg, [])[0] == InboxOutputType.DRAFT_REPLY

    non_exec = tmp_path / "fake_agy"
    non_exec.write_text("#!/bin/sh\n", encoding="utf-8")
    non_exec.chmod(0o644)
    cfg_non_exec = settings.model_copy(update={"agy_binary": str(non_exec)})
    assert AgySessionManager(paths, cfg_non_exec, store)._locate_agy_binary() is None
    cfg_custom = settings.model_copy(update={"agy_binary": "missing_custom_agy_bin"})
    assert AgySessionManager(paths, cfg_custom, store)._locate_agy_binary() is None
