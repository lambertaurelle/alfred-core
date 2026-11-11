"""WP5 TDD test suite for distinct Antigravity workspaces provisioning and dynamic routing."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from alfred.controllers.cli_controller import CliController
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    NounouContract,
    ScheduledWorkday,
)
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
from alfred.services.workspace_service import WorkspaceService


def _seed_legacy_projects_and_db(projects_dir: Path, conv_db: Path) -> None:
    """Create legacy empty/busy project manifests and conversation_summaries.db fixture."""
    projects_dir.mkdir(parents=True, exist_ok=True)
    for pid, name in (
        ("legacy-empty", "Alfred"),
        ("default-cli-project", "CLI Project"),
        ("keep-busy", "Alfred"),
    ):
        (projects_dir / f"{pid}.json").write_text(
            json.dumps({"id": pid, "name": name, "projectResources": {}}),
            encoding="utf-8",
        )
    conn = sqlite3.connect(conv_db)
    conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, project_id TEXT)")
    conn.execute("INSERT INTO conversation_summaries VALUES ('c1', 'keep-busy')")
    conn.commit()
    conn.close()


def _build_ws_cli(
    tmp_path: Path,
    spawned_cmds: list[list[str]],
) -> tuple[CliController, StateStore]:
    """Build a wired CliController with WorkspaceService and fake agy runner."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)

    def _agy_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Record spawned agy commands and return synthetic conversation ID."""
        spawned_cmds.append(cmd)
        out = json.dumps({"conversation_id": f"conv_{len(spawned_cmds)}"})
        return subprocess.CompletedProcess(cmd, 0, out, "")

    agy = AgySessionManager(
        paths, settings, store, runner_fn=_agy_runner, agy_locator_fn=lambda: "agy"
    )
    ws_svc = WorkspaceService(paths, settings, agy, projects_dir=tmp_path / "projects")
    cal_mock = MagicMock()
    cal_mock.events.return_value.list.return_value.execute.return_value = {"items": []}
    cal = CalendarService(paths, settings, calendar_client=cal_mock)
    knowledge = KnowledgeService(paths, settings, store)
    drive = DriveService(paths, settings, store, drive_client=MagicMock())
    gmail = GmailService(paths, settings, gmail_client=MagicMock())
    inbox = InboxTriageService(store, gmail, knowledge, drive, agy)
    nounou = NounouService(store, cal, knowledge, settings)
    evolve = EvolveWorktreeService(paths, settings, store)
    cli = CliController(
        knowledge,
        drive,
        inbox,
        nounou,
        PajemploiCalculator(),
        cal,
        evolve,
        store,
        gmail_service=gmail,
        settings=settings,
        workspace_service=ws_svc,
        agy_manager=agy,
    )
    return cli, store


def test_settings_load_workspaces_and_resolve_project(tmp_path: Path) -> None:
    """Verify AlfredSettings loads antigravity.workspaces YAML and resolves aliases."""
    paths = AlfredPaths.from_env(tmp_path)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path.write_text(
        "antigravity:\n"
        "  binary_path: agy\n"
        "  workspaces:\n"
        "    life: custom-life\n"
        "    inbox: custom-inbox\n"
        "    calendar: alfred-calendar\n"
        "    evolve: alfred-evolve\n",
        encoding="utf-8",
    )
    settings = AlfredSettings.load(paths)
    assert settings.resolve_workspace_project("inbox") == "custom-inbox"
    assert settings.resolve_workspace_project("alfred-inbox") == "custom-inbox"
    assert settings.resolve_workspace_project("life") == "custom-life"
    assert settings.resolve_workspace_project("alfred-life") == "custom-life"
    assert settings.resolve_workspace_project("calendar") == "alfred-calendar"
    assert settings.resolve_workspace_project("   ") == "custom-life"
    assert settings.resolve_workspace_project("explicit-proj") == "explicit-proj"


def test_workspace_service_provisions_idempotently_and_prunes_empty_legacy(
    tmp_path: Path,
) -> None:
    """Verify WorkspaceService links .agents, provisions 4 projects, and prunes safely."""
    paths = AlfredPaths.from_env(tmp_path)
    (paths.root_dir / ".agents" / "agents").mkdir(parents=True)
    for role in ("life", "inbox", "calendar", "evolve"):
        ws_dir = paths.workspaces_dir / f"alfred-{role}"
        ws_dir.mkdir(parents=True)
        (ws_dir / "AGENTS.md").write_text(f"# Workspace alfred-{role}\n", encoding="utf-8")

    projects_dir = tmp_path / "projects"
    conv_db = tmp_path / "conversation_summaries.db"
    _seed_legacy_projects_and_db(projects_dir, conv_db)
    settings = AlfredSettings.load(paths)
    agy = AgySessionManager(
        paths, settings, StateStore(paths), agy_locator_fn=lambda: "/usr/local/bin/agy"
    )
    counter = 0

    def _runner(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        """Simulate agy --new-project agents and agy --project <name> agents."""
        nonlocal counter
        if "--new-project" in cmd:
            counter += 1
            pid = f"uuid-{counter}"
            payload = {
                "id": pid,
                "name": cwd.name,
                "projectResources": {"resources": [{"folderUri": cwd.as_uri()}]},
            }
            (projects_dir / f"{pid}.json").write_text(json.dumps(payload), encoding="utf-8")
        out = "Available agents:\nplan-executor\nplan-synthesizer\nresearch-drive\nresearch-web\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    svc = WorkspaceService(paths, settings, agy, projects_dir, conv_db, _runner)
    first = svc.init_workspaces(prune_legacy=True)
    assert [s.project_name for s in first] == [
        "alfred-life",
        "alfred-inbox",
        "alfred-calendar",
        "alfred-evolve",
    ]
    assert all(s.created and len(s.agents_discovered) == 4 for s in first)
    assert (Path(first[0].workspace_dir) / ".agents").resolve() == (
        paths.root_dir / ".agents"
    ).resolve()
    assert not (projects_dir / "legacy-empty.json").exists()
    assert not (projects_dir / "default-cli-project.json").exists()
    assert (projects_dir / "keep-busy.json").exists()

    second = svc.init_workspaces(prune_legacy=False)
    assert all(not s.created for s in second) and counter == 4


def test_workspace_service_offline_fallback_without_agy_binary(tmp_path: Path) -> None:
    """Verify WorkspaceService writes native project JSON cleanly when agy binary is absent."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths).model_copy(
        update={
            "workspaces": {
                "life": "mon-life",
                "inbox": "alfred-inbox",
                "calendar": "alfred-calendar",
                "evolve": "alfred-evolve",
            }
        }
    )
    store = StateStore(paths)
    agy = AgySessionManager(paths, settings, store, agy_locator_fn=lambda: None)
    projects_dir = tmp_path / "offline_projects"
    svc = WorkspaceService(paths, settings, agy, projects_dir=projects_dir)
    statuses = svc.init_workspaces(prune_legacy=False)
    assert statuses[0].project_name == "mon-life" and statuses[0].created is True
    saved = json.loads((projects_dir / f"{statuses[0].project_id}.json").read_text("utf-8"))
    assert saved["name"] == "mon-life"


def test_cli_init_workspaces_and_calendar_evolve_spawn_agy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify CLI init-workspaces and --spawn-agy routing to alfred-calendar and alfred-evolve."""
    spawned_cmds: list[list[str]] = []
    cli, store = _build_ws_cli(tmp_path, spawned_cmds)

    assert cli.run(["init-workspaces", "--prune-legacy"]) == 0
    assert len(json.loads(capsys.readouterr().out)) == 4

    store.nounou_contract.save(
        NounouContract(
            contract_id="c1",
            employee_name="Nounou",
            child_name="Enfant",
            calendar_id="cal_1",
            hourly_rate_net=4.5,
            base_monthly_net_salary=630.0,
            base_monthly_hours=140,
            base_monthly_days=14,
            maintenance_allowance_daily=3.8,
            meal_allowance_daily=4.5,
            scheduled_workdays=[
                ScheduledWorkday(
                    weekday="MONDAY", start_time="08:30", end_time="17:30", hours=9.0
                )
            ],
        )
    )
    assert cli.run(["seed-nounou", "--date", "2026-05-20", "--spawn-agy"]) == 0
    capsys.readouterr()
    vac_argv = ["check-vacation", "--start", "2026-08-01", "--end", "2026-08-10", "--spawn-agy"]
    assert cli.run(vac_argv) == 0
    capsys.readouterr()
    assert cli.run(["close-pajemploi", "--year", "2026", "--month", "5", "--spawn-agy"]) == 0
    capsys.readouterr()
    ev_argv = ["evolve", "--id", "f1", "--title", "WS", "--description", "Spec", "--spawn-agy"]
    assert cli.run(ev_argv) == 0
    capsys.readouterr()

    projects_targeted = [cmd[cmd.index("--project") + 1] for cmd in spawned_cmds]
    assert projects_targeted == [
        "alfred-calendar",
        "alfred-calendar",
        "alfred-calendar",
        "alfred-evolve",
    ]
