"""WP4 TDD test suite for manifests, EvolveWorktreeService Dual-Repo PRs, CLI, and main.py."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from alfred.controllers.cli_controller import CliController
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.main import main
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


def test_wp4_subagent_and_workspace_manifests_exist() -> None:
    """Verify all 5 subagent manifests and 4 workspace AGENTS.md files exist and follow V7.1."""
    repo_root = Path("/root/Alfred")
    expected_agents = [
        "research-drive.md",
        "research-gmail-cal.md",
        "research-web.md",
        "plan-synthesizer.md",
        "plan-executor.md",
    ]
    for agent_file in expected_agents:
        path = repo_root / ".agents" / "agents" / agent_file
        assert path.exists(), f"Missing agent manifest: {path}"
        assert len(path.read_text(encoding="utf-8")) > 100

    expected_workspaces = ["alfred-life", "alfred-inbox", "alfred-calendar", "alfred-evolve"]
    for ws in expected_workspaces:
        ws_path = repo_root / "core" / "workspaces" / ws / "AGENTS.md"
        assert ws_path.exists(), f"Missing workspace AGENTS.md: {ws_path}"
        content = ws_path.read_text(encoding="utf-8")
        assert "implementation_plan.md" in content


def _init_dual_git_fixture(tmp_path: Path) -> AlfredPaths:
    """Create a realistic Dual-Git repository pair (alfred-core + alfred-instance) in tmp_path."""
    paths = AlfredPaths.from_env(tmp_path)
    (paths.core_dir / "scripts").mkdir(parents=True)
    for name in ("check-upstream-leak.sh", "check_code_architecture.py"):
        src = Path("/root/Alfred/core/scripts") / name
        dst = paths.core_dir / "scripts" / name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        dst.chmod(0o755)

    (paths.root_dir / ".gitignore").write_text("instance/\nworktrees/\n", encoding="utf-8")
    (paths.root_dir / "README.md").write_text("# Alfred Core\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=paths.root_dir, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Alfred Maintainer"], cwd=paths.root_dir, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "alfred@example.com"], cwd=paths.root_dir, check=True
    )
    subprocess.run(["git", "add", "."], cwd=paths.root_dir, check=True)
    subprocess.run(["git", "commit", "-m", "chore: init core"], cwd=paths.root_dir, check=True)

    (paths.instance_dir / "extensions").mkdir(parents=True)
    (paths.instance_dir / "secrets").mkdir(parents=True)
    (paths.instance_dir / "data").mkdir(parents=True)
    (paths.instance_dir / ".gitignore").write_text("secrets/\ndata/\n*.lock\n", encoding="utf-8")
    (paths.instance_dir / "extensions" / "__init__.py").write_text(
        '"""Extensions."""\n', encoding="utf-8"
    )
    (paths.instance_dir / "secrets" / "token.json").write_text("SECRET", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=paths.instance_dir, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Alfred Instance"], cwd=paths.instance_dir, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "instance@example.com"], cwd=paths.instance_dir, check=True
    )
    subprocess.run(["git", "add", "."], cwd=paths.instance_dir, check=True)
    subprocess.run(
        ["git", "commit", "-m", "chore: init instance"], cwd=paths.instance_dir, check=True
    )
    return paths


def test_evolve_worktree_service_dual_repo_prs_and_leak_block(tmp_path: Path) -> None:
    """Verify isolated git worktree creation, leak/syntax rejection, and Dual-Repo PRs."""
    paths = _init_dual_git_fixture(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    tdd_runs: list[Path] = []
    evolve = EvolveWorktreeService(paths, settings, store, tdd_gate_fn=tdd_runs.append)

    with pytest.raises(ValueError, match="Invalid proposal_id"):
        evolve.prepare_evolution_plan("../bad", "Bad", "Path traversal")

    record, plan_md = evolve.prepare_evolution_plan("feat_01", "Add parser", "Receipt summary.")
    assert record.status == "PENDING_GATE" and "alfred-core" in plan_md
    core_wt, inst_wt = evolve.create_isolated_worktrees("feat_01")

    bad_ip = ".".join(["192", "168", "1", "50"])
    (core_wt / "leaked.txt").write_text(f"Server {bad_ip}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Leak"):
        evolve.execute_and_open_dual_prs("feat_01", "feat: x", "body", core_wt, inst_wt)
    (core_wt / "leaked.txt").unlink()

    (inst_wt / "extensions" / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="architecture failed"):
        evolve.execute_and_open_dual_prs("feat_01", "feat: x", "body", core_wt, inst_wt)
    (inst_wt / "extensions" / "broken.py").unlink()

    (core_wt / "README.md").write_text("# Alfred Core\n\nGeneric feature.\n", encoding="utf-8")
    (inst_wt / "extensions" / "custom_rule.py").write_text(
        '"""Personal rule extension."""\n', encoding="utf-8"
    )
    prs = evolve.execute_and_open_dual_prs("feat_01", "feat: parser", "TDD", core_wt, inst_wt)
    assert len(tdd_runs) >= 1 and {pr.repo_name for pr in prs} == {"alfred-core", "alfred-instance"}
    inst_pr = next(pr for pr in prs if pr.repo_name == "alfred-instance")
    assert "extensions/custom_rule.py" in inst_pr.changed_files
    assert all("secrets" not in f and "data" not in f for f in inst_pr.changed_files)
    evolve.cleanup_worktrees("feat_01")


def _sample_contract_json(tmp_path: Path) -> Path:
    """Write a compact valid NounouContract JSON file for CLI testing."""
    contract_file = tmp_path / "contract.json"
    contract_file.write_text(
        json.dumps(
            {
                "contract_id": "c1",
                "employee_name": "Nounou Exemple",
                "child_name": "Enfant",
                "hourly_rate_net": 4.5,
                "base_monthly_net_salary": 630.0,
                "base_monthly_hours": 140,
                "base_monthly_days": 14,
                "maintenance_allowance_daily": 3.8,
                "meal_allowance_daily": 4.5,
                "scheduled_workdays": [
                    {"weekday": "MONDAY", "start_time": "08:30", "end_time": "18:30", "hours": 10.0}
                ],
            }
        ),
        encoding="utf-8",
    )
    return contract_file


def _build_test_cli(paths: AlfredPaths) -> tuple[CliController, EvolveWorktreeService]:
    """Construct a fully mocked CliController and EvolveWorktreeService for CLI tests."""
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    drive_client = MagicMock()
    drive_client.files.return_value.list.return_value.execute.return_value = {"files": []}
    drive_client.changes.return_value.getStartPageToken.return_value.execute.return_value = {
        "startPageToken": "t1"
    }
    gmail_client = MagicMock()
    users = gmail_client.users.return_value
    users.getProfile.return_value.execute.return_value = {"historyId": "101"}
    users.drafts.return_value.update.return_value.execute.return_value = {"id": "dr_1"}
    users.drafts.return_value.send.return_value.execute.return_value = {"id": "sent_1"}
    cal_client = MagicMock()
    cal_client.calendarList.return_value.list.return_value.execute.return_value = {"items": []}
    cal_client.calendars.return_value.insert.return_value.execute.return_value = {"id": "cal_1"}
    cal_client.events.return_value.list.return_value.execute.return_value = {"items": []}
    oauth_mock = MagicMock()
    oauth_mock.bootstrap_interactive_token.return_value = paths.secrets_dir / "token.json"

    knowledge = KnowledgeService(paths, settings, store)
    drive = DriveService(paths, settings, store, drive_client=drive_client)
    gmail = GmailService(paths, settings, gmail_client=gmail_client)
    cal = CalendarService(paths, settings, calendar_client=cal_client)
    agy = AgySessionManager(paths, settings, store)
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
        oauth_service=oauth_mock,
    )
    return cli, evolve


def test_cli_controller_and_main_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify CLI controller Stage-1/2/3 commands and main() < 80-line entrypoint."""
    paths = _init_dual_git_fixture(tmp_path)
    paths.templates_knowledge_dir.mkdir(parents=True, exist_ok=True)
    for tpl in Path("/root/Alfred/core/templates/knowledge").glob("*.md"):
        (paths.templates_knowledge_dir / tpl.name).write_text(
            tpl.read_text(encoding="utf-8"), encoding="utf-8"
        )
    cli, evolve = _build_test_cli(paths)
    contract_file = _sample_contract_json(tmp_path)

    assert cli.run(["auth-google", "--no-browser"]) == 0
    assert cli.run(["scan-knowledge", "--query", "nounou"]) == 0
    assert cli.run(["init-drive"]) == 0
    assert cli.run(["init-drive", "--execute"]) == 0
    assert cli.run(["poll-inbox"]) == 0
    assert (
        cli.run(
            ["send-draft", "--draft-id", "dr_1", "--to", "a@example.com", "--body", "Revised"]
        )
        == 0
    )
    assert cli.run(["init-nounou", "--contract-json", str(contract_file)]) == 0
    assert cli.run(["init-nounou", "--contract-json", str(contract_file), "--execute"]) == 0
    assert cli.run(["seed-nounou", "--date", "2026-04-20"]) == 0
    assert cli.run(["check-vacation", "--start", "2026-08-01", "--end", "2026-08-15"]) == 0
    assert cli.run(["close-pajemploi"]) == 0
    ev_args = ["evolve", "--id", "ev1", "--title", "T", "--description", "D"]
    assert cli.run(ev_args) == 0
    assert cli.run([*ev_args, "--execute"]) == 0
    evolve.cleanup_worktrees("ev1")

    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))
    assert main(["scan-knowledge"]) == 0


def test_evolve_custom_pr_publisher_and_worktree_reset(tmp_path: Path) -> None:
    """Verify pr_publisher_fn OPEN_REMOTE status and existing worktree reset."""
    paths = _init_dual_git_fixture(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    published: list[str] = []

    def _publish(_wt: Path, br: str, _base: str, _t: str, _b: str) -> str:
        """Record branch publication and return synthetic PR URL."""
        published.append(br)
        return f"https://example.test/pr/{len(published)}"

    evolve = EvolveWorktreeService(
        paths,
        settings,
        store,
        tdd_gate_fn=lambda _p: None,
        pr_publisher_fn=_publish,
    )
    evolve.prepare_evolution_plan("rem1", "Remote PR", "Body")
    evolve.create_isolated_worktrees("rem1")
    c_wt, i_wt = evolve.create_isolated_worktrees("rem1")
    (c_wt / "README.md").write_text("# Updated\n", encoding="utf-8")
    prs = evolve.execute_and_open_dual_prs("rem1", "feat: remote", "body", c_wt, i_wt)
    assert all(p.status == "OPEN_REMOTE" for p in prs)
    assert len(published) == 2
    evolve.cleanup_worktrees("rem1")
