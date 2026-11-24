"""End-to-end CLI tests (Graceful Skip + V8 subcommands) and repository manifest invariants."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from alfred.domain.calendar_models import CalendarListEntry, FreeBusyInterval
from alfred.domain.models import (
    ActiveSessionRecord,
    AlfredPaths,
    CalendarEventItem,
    DriveEntry,
    DriveMetadataIndex,
    GoogleSyncBookmarks,
)
from alfred.domain.tasks_models import ContactSummary, GoogleTaskItem, GoogleTaskListItem
from alfred.repositories.state_store import StateStore

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_main(argv: list[str]) -> int:
    """Import alfred.main.main lazily and execute with explicit CLI argv."""
    main_fn: Any = importlib.import_module("alfred.main").main
    return int(main_fn(argv))


class FakeCalendarService:
    """In-memory test double for CalendarService returning typed V8 domain entities."""

    def __init__(self) -> None:
        """Initialize empty call log for calendar CLI subcommands."""
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def list_calendars(self, *args: object, **kwargs: object) -> list[CalendarListEntry]:
        """Return a deterministic primary Google Calendar entry."""
        self.calls.append(("list_calendars", args, dict(kwargs)))
        return [
            CalendarListEntry(
                calendar_id="primary",
                summary="Agenda Principal",
                primary=True,
                access_role="owner",
                time_zone="Europe/Paris",
            )
        ]

    def query_freebusy(self, *args: object, **kwargs: object) -> list[FreeBusyInterval]:
        """Return a deterministic busy interval inside the queried window."""
        self.calls.append(("query_freebusy", args, dict(kwargs)))
        return [
            FreeBusyInterval(
                calendar_id="primary",
                start_iso="2026-11-16T09:00:00Z",
                end_iso="2026-11-16T10:00:00Z",
            )
        ]

    def update_event(self, *args: object, **kwargs: object) -> CalendarEventItem:
        """Return an updated CalendarEventItem reflecting CLI flags."""
        self.calls.append(("update_event", args, dict(kwargs)))
        return CalendarEventItem(
            event_id="ev1",
            summary="Updated",
            start_iso="2026-11-16T14:00:00Z",
            end_iso="2026-11-16T15:00:00Z",
        )


class FakeTasksService:
    """In-memory test double for TasksService returning typed Google Tasks entities."""

    def __init__(self) -> None:
        """Initialize empty call log for Google Tasks CLI subcommands."""
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def list_tasklists(self, *args: object, **kwargs: object) -> list[GoogleTaskListItem]:
        """Return a deterministic default Google TaskList descriptor."""
        self.calls.append(("list_tasklists", args, dict(kwargs)))
        return [
            GoogleTaskListItem(
                tasklist_id="@default",
                title="Tâches Foyer",
                updated="2026-11-15T18:00:00Z",
            )
        ]

    def list_tasks(self, *args: object, **kwargs: object) -> list[GoogleTaskItem]:
        """Return a deterministic pending GoogleTaskItem."""
        self.calls.append(("list_tasks", args, dict(kwargs)))
        return [
            GoogleTaskItem(
                task_id="t1",
                tasklist_id="@default",
                title="Vérifier taxe foncière",
                status="needsAction",
            )
        ]

    def create_task(self, *args: object, **kwargs: object) -> GoogleTaskItem:
        """Record task creation and return a newly created GoogleTaskItem."""
        self.calls.append(("create_task", args, dict(kwargs)))
        return GoogleTaskItem(
            task_id="t-created-1",
            tasklist_id="@default",
            title="T",
            status="needsAction",
        )

    def complete_task(self, *args: object, **kwargs: object) -> GoogleTaskItem:
        """Record task completion and return the completed GoogleTaskItem."""
        self.calls.append(("complete_task", args, dict(kwargs)))
        return GoogleTaskItem(
            task_id="t1",
            tasklist_id="@default",
            title="T",
            status="completed",
            completed_at="2026-11-15T19:00:00Z",
        )


class FakeContactsService:
    """In-memory test double for ContactsService returning typed Google People summaries."""

    def __init__(self) -> None:
        """Initialize empty call log for Google Contacts CLI subcommands."""
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def search_contacts(self, *args: object, **kwargs: object) -> list[ContactSummary]:
        """Record query and return a deterministic fictional ContactSummary."""
        self.calls.append(("search_contacts", args, dict(kwargs)))
        return [
            ContactSummary(
                resource_name="people/c101",
                display_name="Alice Martin",
                emails=["alice@example.com"],
                phones=["+33600000001"],
                organization="Cabinet Exemple",
                etag="etag-c101",
            )
        ]


def _write_settings(root_dir: Path, *, enabled: bool, agy_bin: str = "agy") -> AlfredPaths:
    """Write an isolated instance/config/settings.yaml with uniform feature toggle state."""
    paths = AlfredPaths(root_dir=root_dir)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "antigravity": {"binary_path": agy_bin, "sync_daemon_on_changes": False},
        "features": {
            "drive_knowledge_sync": enabled,
            "inbox_auto_triage": enabled,
            "gmail_drafts_and_send": enabled,
            "calendar_management": enabled,
            "tasks_management": enabled,
            "contacts_lookup": enabled,
            "google_docs_suite": enabled,
            "signature_delegation": enabled,
            "mcp_gemini_bridge": enabled,
        },
        "mcp": {"public_url": "https://alfred.domaine-perso.example"},
    }
    paths.settings_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return paths


def _create_fake_agy_binary(root_dir: Path) -> tuple[Path, Path]:
    """Create an executable local shell stub simulating the agy CLI binary."""
    bin_dir = root_dir / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_file = root_dir / "agy_invocations.log"
    agy_script = bin_dir / "fake_agy.sh"
    script_text = (
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log_file}"\n'
        'echo \'{"session_id": "sess-delegated-001"}\'\n'
    )
    agy_script.write_text(script_text, encoding="utf-8")
    agy_script.chmod(0o755)
    return agy_script, log_file


@pytest.mark.parametrize(
    "argv",
    [
        ["poll-inbox"],
        ["list-tasks"],
        ["create-task", "--title", "T"],
        ["search-contacts", "--query", "Q"],
        ["prepare-signature", "--pdf", "/tmp/x.pdf"],
        ["serve-mcp"],
        ["delegate", "--to", "tasks", "--title", "T", "--brief", "B"],
    ],
)
def test_cli_graceful_skip_when_features_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    """Verify disabled feature flags exit 0 and print SKIPPED_DISABLED on stdout."""
    _write_settings(tmp_path, enabled=False)
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))

    exit_code = _run_main(argv)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "SKIPPED_DISABLED" in captured.out


def test_cli_onboarding_oauth_guide_and_discover_drive_subcommands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify V8 status-onboarding --json, oauth-guide, and discover-drive --json subcommands."""
    paths = _write_settings(tmp_path, enabled=True)
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))

    store = StateStore(paths)
    store.bookmarks.save(GoogleSyncBookmarks(drive_page_token="tok-101"))
    store.drive_index.save(
        DriveMetadataIndex(
            start_page_token="tok-101",
            last_synced_at="2026-11-15T18:00:00Z",
            folder_count=1,
            file_count=1,
            top_level_folders=["01_Administration"],
            entries={
                "fld-1": DriveEntry(
                    file_id="fld-1",
                    name="01_Administration",
                    mime_type="application/vnd.google-apps.folder",
                    path="/01_Administration",
                    is_folder=True,
                )
            },
        )
    )

    assert _run_main(["status-onboarding", "--json"]) == 0
    onboard_json: Any = json.loads(capsys.readouterr().out)
    assert isinstance(onboard_json, dict)

    assert _run_main(["oauth-guide"]) == 0
    guide_out = capsys.readouterr().out
    assert "console.cloud.google.com" in guide_out

    assert _run_main(["discover-drive", "--json"]) == 0
    drive_out = capsys.readouterr().out
    assert isinstance(json.loads(drive_out), dict)
    assert "01_Administration" in drive_out


def test_cli_repair_titles_delegate_and_setup_mcp_subcommands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify V8 repair-session-titles, delegate --to evolve, and setup-mcp subcommands."""
    agy_script, agy_log = _create_fake_agy_binary(tmp_path)
    paths = _write_settings(tmp_path, enabled=True, agy_bin=str(agy_script))
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))

    store = StateStore(paths)
    seeded = ActiveSessionRecord(
        session_id="sess-1", workspace="alfred-life", title="Revue Patrimoine"
    )
    store.conversations.save(
        store.conversations.load().model_copy(update={"sessions": {"sess-1": seeded}})
    )

    assert _run_main(["repair-session-titles"]) == 0
    assert capsys.readouterr().out.strip()

    assert _run_main(["delegate", "--to", "evolve", "--title", "E", "--brief", "B"]) == 0
    del_out = capsys.readouterr().out
    logged = agy_log.read_text(encoding="utf-8") if agy_log.exists() else ""
    assert "alfred-evolve" in del_out or "alfred-evolve" in logged

    assert _run_main(["setup-mcp", "--pin", "246810"]) == 0
    _ = capsys.readouterr()
    mcp_secret = paths.secrets_dir / "mcp_oauth.json"
    assert mcp_secret.is_file()
    assert "246810" not in mcp_secret.read_text(encoding="utf-8")


def test_cli_google_calendar_tasks_and_contacts_subcommands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify list-calendars, query-freebusy, update-event, tasks, and search-contacts via CLI."""
    paths = _write_settings(tmp_path, enabled=True)
    fake_cal = FakeCalendarService()
    fake_tasks = FakeTasksService()
    fake_contacts = FakeContactsService()
    cli_mod = importlib.import_module("alfred.controllers.cli_controller")
    factory: Any = cli_mod.build_cli_controller
    controller: Any = factory(
        paths=paths,
        calendar_service=fake_cal,
        tasks_service=fake_tasks,
        contacts_service=fake_contacts,
    )

    assert controller.run(["list-calendars"]) == 0
    assert "Agenda Principal" in capsys.readouterr().out

    fb_cmd = ["query-freebusy", "--from", "2026-11-16T08:00:00Z", "--to", "2026-11-16T18:00:00Z"]
    assert controller.run(fb_cmd) == 0
    assert "2026-11-16T09:00:00Z" in capsys.readouterr().out

    upd_cmd = ["update-event", "--calendar", "primary", "--event-id", "ev1", "--summary", "Updated"]
    assert controller.run(upd_cmd) == 0
    assert "Updated" in capsys.readouterr().out

    assert controller.run(["list-tasklists"]) == 0
    assert "Tâches Foyer" in capsys.readouterr().out

    assert controller.run(["list-tasks"]) == 0
    assert "Vérifier taxe foncière" in capsys.readouterr().out

    assert controller.run(["create-task", "--title", "T"]) == 0
    assert capsys.readouterr().out.strip()

    assert controller.run(["complete-task", "--task-id", "t1"]) == 0
    assert "t1" in capsys.readouterr().out

    assert controller.run(["search-contacts", "--query", "Alice"]) == 0
    contacts_out = capsys.readouterr().out
    assert "Alice Martin" in contacts_out or "alice@example.com" in contacts_out


def test_hooks_json_defines_pre_tool_use_security_hook() -> None:
    """Verify .agents/hooks.json is valid JSON and wires PreToolUse to security_pre_tool_hook.py."""
    hooks_path = REPO_ROOT / ".agents" / "hooks.json"
    assert hooks_path.is_file()
    parsed: Any = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    hooks_map: Any = parsed.get("hooks", parsed)
    assert isinstance(hooks_map, dict)
    assert "PreToolUse" in hooks_map
    assert isinstance(hooks_map["PreToolUse"], list) and hooks_map["PreToolUse"]
    assert "security_pre_tool_hook.py" in json.dumps(hooks_map["PreToolUse"])


def test_subagent_manifests_exist_with_yaml_frontmatter() -> None:
    """Verify TDD triad and study-orchestrator agent manifests exist with valid YAML frontmatter."""
    expected_agents = (
        "tdd-red.md",
        "tdd-green.md",
        "tdd-verifier.md",
        "study-orchestrator.md",
    )
    for filename in expected_agents:
        agent_path = REPO_ROOT / ".agents" / "agents" / filename
        assert agent_path.is_file(), f"Missing agent manifest: {agent_path}"
        raw = agent_path.read_text(encoding="utf-8")
        assert raw.startswith("---\n"), f"{filename} must start with YAML frontmatter"
        parts = raw.split("\n---\n", maxsplit=1)
        assert len(parts) == 2, f"{filename} must close YAML frontmatter"
        frontmatter: Any = yaml.safe_load(parts[0][4:])
        assert isinstance(frontmatter, dict) and frontmatter


def test_six_workspace_agents_md_exist_and_purge_legacy_terms() -> None:
    """Verify all 7 specialist workspace AGENTS.md exist and contain zero legacy childcare terms."""
    workspaces = (
        "alfred-onboard",
        "alfred-life",
        "alfred-inbox",
        "alfred-calendar",
        "alfred-tasks",
        "alfred-drive",
        "alfred-evolve",
    )
    forbidden_terms = ("nou" + "nou", "pajem" + "ploi")
    for slug in workspaces:
        manifest = REPO_ROOT / "core" / "workspaces" / slug / "AGENTS.md"
        assert manifest.is_file(), f"Missing workspace manifest: {manifest}"
        lowered = manifest.read_text(encoding="utf-8").lower()
        for term in forbidden_terms:
            assert term not in lowered, f"Forbidden term in {manifest}"


def test_evolve_and_root_agents_md_specify_tdd_triad_and_hydrogen_executor() -> None:
    """Verify root AGENTS.md and alfred-evolve/AGENTS.md codify TDD triad & Hydrogen executor."""
    root_md = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    evolve_md = (REPO_ROOT / "core" / "workspaces" / "alfred-evolve" / "AGENTS.md").read_text(
        encoding="utf-8"
    )
    for tdd_agent in ("tdd-red", "tdd-green", "tdd-verifier"):
        assert tdd_agent in evolve_md
        assert tdd_agent in root_md
    assert "plan-executor" in root_md
    assert "hydrogen" in root_md.lower()


def test_bootstrap_script_and_concise_readme_invariants() -> None:
    """Verify executable scripts/bootstrap.sh and <=220L README.md with OAuth, Tunnel, and TLS."""
    bootstrap = REPO_ROOT / "scripts" / "bootstrap.sh"
    assert bootstrap.is_file()
    assert os.access(bootstrap, os.X_OK)
    assert bool(bootstrap.stat().st_mode & 0o111)

    readme = REPO_ROOT / "README.md"
    assert readme.is_file()
    text = readme.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert 1 <= len(lines) <= 220
    assert "|" in text
    for scope_token in ("drive", "gmail.modify", "calendar", "tasks", "contacts"):
        assert scope_token in text
    lower = text.lower()
    assert "cloudflared" in lower
    assert "tls" in lower
