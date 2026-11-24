"""WP5 TDD test suite for the 7 canonical V8 Antigravity specialist workspaces."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from alfred.controllers.cli_controller import CliController
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.main import build_controller
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.evolve_worktree_service import EvolveWorktreeService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.workspace_service import WorkspaceService

CANONICAL_ROLES: tuple[str, ...] = (
    "onboard",
    "life",
    "inbox",
    "calendar",
    "tasks",
    "drive",
    "evolve",
)


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
    """Build a wired CliController with WorkspaceService over all 6 V8 roles."""
    paths = AlfredPaths.from_env(tmp_path)
    for role in CANONICAL_ROLES:
        ws_dir = paths.workspaces_dir / f"alfred-{role}"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "AGENTS.md").write_text(f"# Workspace alfred-{role}\n", encoding="utf-8")

    ctrl = build_controller(str(tmp_path))
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
    cal = CalendarService(paths, settings, calendar_client=cal_mock, state_store=store)
    knowledge = KnowledgeService(paths, settings, store)
    drive = DriveService(paths, settings, store, drive_client=MagicMock())
    gmail = GmailService(paths, settings, gmail_client=MagicMock())
    inbox = InboxTriageService(store, gmail, knowledge, drive, agy, settings=settings)
    evolve = EvolveWorktreeService(paths, settings, store)
    ctrl._knowledge = knowledge
    ctrl._drive = drive
    ctrl._gmail = gmail
    ctrl._calendar = cal
    ctrl._inbox = inbox
    ctrl._evolve = evolve
    ctrl._agy = agy
    ctrl._workspaces = ws_svc
    return ctrl, store


def test_settings_load_workspaces_and_resolve_six_projects(tmp_path: Path) -> None:
    """Verify AlfredSettings loads all 7 workspace roles and resolves aliases."""
    paths = AlfredPaths.from_env(tmp_path)
    paths.settings_path.parent.mkdir(parents=True, exist_ok=True)
    paths.settings_path.write_text(
        "antigravity:\n"
        "  binary_path: agy\n"
        "  workspaces:\n"
        "    onboard: custom-onboard\n"
        "    life: custom-life\n"
        "    inbox: custom-inbox\n"
        "    calendar: alfred-calendar\n"
        "    tasks: custom-tasks\n"
        "    drive: custom-drive\n"
        "    evolve: alfred-evolve\n",
        encoding="utf-8",
    )
    settings = AlfredSettings.load(paths)
    assert settings.resolve_workspace_project("onboard") == "custom-onboard"
    assert settings.resolve_workspace_project("alfred-onboard") == "custom-onboard"
    assert settings.resolve_workspace_project("inbox") == "custom-inbox"
    assert settings.resolve_workspace_project("alfred-inbox") == "custom-inbox"
    assert settings.resolve_workspace_project("life") == "custom-life"
    assert settings.resolve_workspace_project("alfred-life") == "custom-life"
    assert settings.resolve_workspace_project("calendar") == "alfred-calendar"
    assert settings.resolve_workspace_project("tasks") == "custom-tasks"
    assert settings.resolve_workspace_project("alfred-tasks") == "custom-tasks"
    assert settings.resolve_workspace_project("drive") == "custom-drive"
    assert settings.resolve_workspace_project("alfred-drive") == "custom-drive"
    assert settings.resolve_workspace_project("evolve") == "alfred-evolve"
    assert settings.resolve_workspace_project("   ") == "custom-life"
    assert settings.resolve_workspace_project("explicit-proj") == "explicit-proj"


def test_workspace_service_provisions_six_roles_and_prunes_empty_legacy(
    tmp_path: Path,
) -> None:
    """Verify WorkspaceService links .agents, provisions all 7 projects, and prunes safely."""
    paths = AlfredPaths.from_env(tmp_path)
    (paths.root_dir / ".agents" / "agents").mkdir(parents=True)
    for role in CANONICAL_ROLES:
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
    assert [s.role for s in first] == list(CANONICAL_ROLES)
    assert [s.project_name for s in first] == [f"alfred-{r}" for r in CANONICAL_ROLES]
    assert all(s.created and len(s.agents_discovered) == 4 for s in first)
    assert (Path(first[0].workspace_dir) / ".agents").resolve() == (
        paths.root_dir / ".agents"
    ).resolve()
    assert not (projects_dir / "legacy-empty.json").exists()
    assert not (projects_dir / "default-cli-project.json").exists()
    assert (projects_dir / "keep-busy.json").exists()

    second = svc.init_workspaces(prune_legacy=False)
    assert all(not s.created for s in second) and counter == 7


def test_workspace_service_offline_fallback_without_agy_binary(tmp_path: Path) -> None:
    """Verify WorkspaceService writes native project JSON cleanly for 7 roles offline."""
    paths = AlfredPaths.from_env(tmp_path)
    settings = AlfredSettings.load(paths).model_copy(
        update={
            "workspaces": {
                "onboard": "alfred-onboard",
                "life": "mon-life",
                "inbox": "alfred-inbox",
                "calendar": "alfred-calendar",
                "tasks": "alfred-tasks",
                "drive": "alfred-drive",
                "evolve": "alfred-evolve",
            }
        }
    )
    store = StateStore(paths)
    agy = AgySessionManager(paths, settings, store, agy_locator_fn=lambda: None)
    projects_dir = tmp_path / "offline_projects"
    svc = WorkspaceService(paths, settings, agy, projects_dir=projects_dir)
    statuses = svc.init_workspaces(prune_legacy=False)
    assert len(statuses) == 7
    life_status = next(s for s in statuses if s.role == "life")
    assert life_status.project_name == "mon-life" and life_status.created is True
    saved = json.loads((projects_dir / f"{life_status.project_id}.json").read_text("utf-8"))
    assert saved["name"] == "mon-life"


def test_cli_init_workspaces_and_evolve_spawn_agy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify CLI init-workspaces returns 7 specialist projects and evolve routes to evolve."""
    spawned_cmds: list[list[str]] = []
    cli, _ = _build_ws_cli(tmp_path, spawned_cmds)

    assert cli.run(["init-workspaces", "--prune-legacy"]) == 0
    provisioned = json.loads(capsys.readouterr().out)
    assert len(provisioned) == 7
    assert {item["role"] for item in provisioned} == set(CANONICAL_ROLES)

    ev_argv = ["evolve", "--id", "f1", "--title", "WS", "--description", "Spec", "--spawn-agy"]
    assert cli.run(ev_argv) == 0
    capsys.readouterr()

    projects_targeted = [cmd[cmd.index("--project") + 1] for cmd in spawned_cmds]
    assert projects_targeted == ["alfred-evolve"]
