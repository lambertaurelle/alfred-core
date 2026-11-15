"""WP4 TDD test suite for V8 manifests, Mono-Repo EvolveWorktreeService PR, CLI, and main.py."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from alfred.controllers.cli_controller import CliController
from alfred.domain.calendar_models import PullRequestArtifact
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.main import build_controller, main
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.evolve_worktree_service import EvolveWorktreeService
from alfred.services.gmail_service import GmailService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService


def _cleanup_proposal_worktree(evolve: EvolveWorktreeService, proposal_id: str) -> None:
    """Clean up an isolated Mono-Repo evolution worktree."""
    evolve.cleanup_worktree(proposal_id)


def test_wp4_subagent_and_workspace_manifests_exist() -> None:
    """Verify subagent manifests and all 6 canonical V8 workspace AGENTS.md files exist."""
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

    expected_workspaces = [
        "alfred-onboard",
        "alfred-life",
        "alfred-inbox",
        "alfred-calendar",
        "alfred-tasks",
        "alfred-evolve",
    ]
    for ws in expected_workspaces:
        ws_path = repo_root / "core" / "workspaces" / ws / "AGENTS.md"
        assert ws_path.exists(), f"Missing workspace AGENTS.md: {ws_path}"
        content = ws_path.read_text(encoding="utf-8")
        assert "implementation_plan.md" in content


def _init_mono_git_fixture(tmp_path: Path) -> AlfredPaths:
    """Create a realistic V8 Mono-Repo Git fixture in tmp_path with zero instance/.git."""
    paths = AlfredPaths.from_env(tmp_path)
    (paths.core_dir / "scripts").mkdir(parents=True)
    for name in ("check-upstream-leak.sh", "check_code_architecture.py"):
        src = Path("/root/Alfred/core/scripts") / name
        dst = paths.core_dir / "scripts" / name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        dst.chmod(0o755)

    (paths.root_dir / ".gitignore").write_text(
        "worktrees/\n"
        "*.lock\n"
        "instance/data/\n"
        "instance/secrets/\n"
        "instance/config/settings.yaml\n"
        "instance/extensions/*.py\n"
        "!instance/extensions/__init__.py\n"
        "!instance/extensions/*.example.py\n",
        encoding="utf-8",
    )
    (paths.root_dir / "README.md").write_text("# Alfred Mono-Repo\n", encoding="utf-8")
    (paths.instance_dir / "examples").mkdir(parents=True)
    (paths.instance_dir / "extensions").mkdir(parents=True)
    (paths.instance_dir / "secrets").mkdir(parents=True)
    (paths.instance_dir / "data").mkdir(parents=True)
    (paths.instance_dir / "examples" / "README.md").write_text("# Examples\n", encoding="utf-8")
    (paths.instance_dir / "extensions" / "__init__.py").write_text(
        '"""Extensions."""\n', encoding="utf-8"
    )
    (paths.instance_dir / "secrets" / "token.json").write_text("SECRET", encoding="utf-8")
    (paths.instance_dir / "data" / "private.md").write_text("PRIVATE", encoding="utf-8")

    subprocess.run(["git", "init", "-b", "main"], cwd=paths.root_dir, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Alfred Maintainer"], cwd=paths.root_dir, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "alfred@example.com"], cwd=paths.root_dir, check=True
    )
    subprocess.run(["git", "add", "."], cwd=paths.root_dir, check=True)
    subprocess.run(["git", "commit", "-m", "chore: init mono-repo"], cwd=paths.root_dir, check=True)
    assert not (paths.instance_dir / ".git").exists()
    return paths


def test_evolve_worktree_service_mono_repo_pr_and_leak_block(tmp_path: Path) -> None:
    """Verify single mono-repo git worktree, leak/architecture gates, and single PR artifact."""
    paths = _init_mono_git_fixture(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    tdd_runs: list[Path] = []
    evolve = EvolveWorktreeService(paths, settings, store, tdd_gate_fn=tdd_runs.append)

    with pytest.raises(ValueError, match="Invalid proposal_id"):
        evolve.prepare_evolution_plan("../bad", "Bad", "Path traversal")

    record, plan_md = evolve.prepare_evolution_plan("feat_01", "Add parser", "Receipt summary.")
    assert record.status == "PENDING_GATE" and "alfred-core" in plan_md
    wt = evolve.create_isolated_worktree("feat_01")
    assert isinstance(wt, Path) and wt.is_dir()
    assert not (paths.instance_dir / ".git").exists()

    bad_ip = ".".join(["192", "168", "1", "50"])
    (wt / "leaked.txt").write_text(f"Server {bad_ip}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="(?i)leak"):
        evolve.execute_and_open_pr("feat_01", "feat: x", "body", wt)
    (wt / "leaked.txt").unlink()

    (wt / "core" / "broken.py").write_text("def oops(:\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="(?i)architecture"):
        evolve.execute_and_open_pr("feat_01", "feat: x", "body", wt)
    (wt / "core" / "broken.py").unlink()

    (wt / "README.md").write_text("# Alfred Mono-Repo\n\nGeneric feature.\n", encoding="utf-8")
    (wt / "instance" / "examples" / "sample_ext.py").write_text(
        '"""Sample fictional extension."""\n', encoding="utf-8"
    )
    pr = evolve.execute_and_open_pr("feat_01", "feat: parser", "TDD", wt)
    assert isinstance(pr, PullRequestArtifact)
    assert pr.repo_name == "alfred-core"
    assert len(tdd_runs) >= 1
    assert "instance/examples/sample_ext.py" in pr.changed_files
    assert all("secrets" not in f and "data" not in f for f in pr.changed_files)
    _cleanup_proposal_worktree(evolve, "feat_01")


def _build_test_cli(paths: AlfredPaths) -> tuple[CliController, EvolveWorktreeService]:
    """Construct a mocked CliController and EvolveWorktreeService for V8 CLI tests."""
    ctrl = build_controller(str(paths.root_dir))
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
    cal_client.events.return_value.list.return_value.execute.return_value = {"items": []}
    oauth_mock = MagicMock()
    oauth_mock.bootstrap_interactive_token.return_value = paths.secrets_dir / "token.json"

    knowledge = KnowledgeService(paths, settings, store)
    drive = DriveService(paths, settings, store, drive_client=drive_client)
    gmail = GmailService(paths, settings, gmail_client=gmail_client)
    cal = CalendarService(paths, settings, calendar_client=cal_client, state_store=store)
    agy = AgySessionManager(paths, settings, store)
    inbox = InboxTriageService(store, gmail, knowledge, drive, agy, settings=settings)
    evolve = EvolveWorktreeService(paths, settings, store, tdd_gate_fn=lambda _p: None)
    ctrl._knowledge = knowledge
    ctrl._drive = drive
    ctrl._gmail = gmail
    ctrl._calendar = cal
    ctrl._agy = agy
    ctrl._inbox = inbox
    ctrl._evolve = evolve
    ctrl._oauth = oauth_mock
    return ctrl, evolve


def test_cli_controller_and_main_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify V8 CLI controller core commands and main() < 80-line entrypoint."""
    paths = _init_mono_git_fixture(tmp_path)
    paths.templates_knowledge_dir.mkdir(parents=True, exist_ok=True)
    for tpl in Path("/root/Alfred/core/templates/knowledge").glob("*.md"):
        (paths.templates_knowledge_dir / tpl.name).write_text(
            tpl.read_text(encoding="utf-8"), encoding="utf-8"
        )
    cli, evolve = _build_test_cli(paths)

    assert cli.run(["auth-google", "--no-browser"]) == 0
    assert cli.run(["scan-knowledge", "--query", "classement"]) == 0
    assert cli.run(["init-drive"]) == 0
    assert cli.run(["init-drive", "--execute"]) == 0
    assert cli.run(["poll-inbox"]) == 0
    assert (
        cli.run(
            ["send-draft", "--draft-id", "dr_1", "--to", "a@example.com", "--body", "Revised"]
        )
        == 0
    )
    ev_args = ["evolve", "--id", "ev1", "--title", "T", "--description", "D"]
    assert cli.run(ev_args) == 0
    assert cli.run([*ev_args, "--execute"]) == 0
    _cleanup_proposal_worktree(evolve, "ev1")

    monkeypatch.setenv("ALFRED_ROOT_DIR", str(tmp_path))
    assert main(["scan-knowledge"]) == 0


def test_evolve_custom_pr_publisher_and_worktree_reset(tmp_path: Path) -> None:
    """Verify pr_publisher_fn OPEN_REMOTE status and existing mono-repo worktree reset."""
    paths = _init_mono_git_fixture(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    published: list[str] = []

    def _publish(_wt: Path, br: str, _base: str, _t: str, _b: str) -> str:
        """Record branch publication and return synthetic PR URL."""
        published.append(br)
        return f"https://example.com/pr/{len(published)}"

    evolve = EvolveWorktreeService(
        paths,
        settings,
        store,
        tdd_gate_fn=lambda _p: None,
        pr_publisher_fn=_publish,
    )
    evolve.prepare_evolution_plan("rem1", "Remote PR", "Body")
    evolve.create_isolated_worktree("rem1")
    wt = evolve.create_isolated_worktree("rem1")
    (wt / "README.md").write_text("# Updated\n", encoding="utf-8")
    pr = evolve.execute_and_open_pr("rem1", "feat: remote", "body", wt)
    assert pr.status == "OPEN_REMOTE"
    assert len(published) == 1
    _cleanup_proposal_worktree(evolve, "rem1")
