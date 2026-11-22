"""End-to-end tests for V8 sync-spec CLI, evolve modes CLI, and repository invariants."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from alfred.controllers.app_factory import build_cli_controller
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.main import main
from alfred.repositories.state_store import StateStore
from alfred.services.evolve_worktree_service import EvolveWorktreeService

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


def _init_cli_git_repo(tmp_path: Path) -> AlfredPaths:
    """Create a minimal Git mono-repo fixture for E2E sync-spec and evolve CLI checks."""
    paths = AlfredPaths.from_env(tmp_path)
    (paths.core_dir / "scripts").mkdir(parents=True)
    for name in ("check-upstream-leak.sh", "check_code_architecture.py"):
        src = WORKTREE_ROOT / "core" / "scripts" / name
        dst = paths.core_dir / "scripts" / name
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        dst.chmod(0o755)
    (paths.root_dir / "pyproject.toml").write_text(
        '[project]\nname = "alfred-core"\nversion = "8.0.0"\n', encoding="utf-8"
    )
    (paths.root_dir / ".gitignore").write_text(
        "worktrees/\ninstance/data/\ninstance/secrets/\ninstance/config/settings.yaml\n"
        "instance/extensions/*.py\n!instance/extensions/__init__.py\n",
        encoding="utf-8",
    )
    (paths.core_dir / "workspaces" / "alfred-evolve").mkdir(parents=True)
    (paths.core_dir / "workspaces" / "alfred-evolve" / "AGENTS.md").write_text(
        "# Workspace alfred-evolve\n\nTDD.\n", encoding="utf-8"
    )
    dom_dir = paths.core_dir / "alfred" / "domain"
    ctl_dir = paths.core_dir / "alfred" / "controllers"
    dom_dir.mkdir(parents=True)
    ctl_dir.mkdir(parents=True)
    (dom_dir / "__init__.py").write_text('"""Domain."""\n', encoding="utf-8")
    (dom_dir / "feature_models.py").write_text(
        '"""Flags."""\nfrom pydantic import BaseModel\n'
        'class FeatureFlagsConfig(BaseModel):\n    """Flags."""\n'
        "    drive_knowledge_sync: bool = True\n",
        encoding="utf-8",
    )
    (ctl_dir / "__init__.py").write_text('"""Controllers."""\n', encoding="utf-8")
    (ctl_dir / "cli_controller.py").write_text(
        '"""CLI."""\n\n\ndef reg(sub: object) -> None:\n'
        '    """Reg.\n\n    Args:\n        sub: Parser.\n    """\n'
        '    sub.add_parser("sync-spec")\n',
        encoding="utf-8",
    )
    (paths.instance_dir / "extensions").mkdir(parents=True)
    (paths.instance_dir / "extensions" / "__init__.py").write_text('"""Ext."""\n', encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=paths.root_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Maintainer"], cwd=paths.root_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "alfred@example.com"], cwd=paths.root_dir, check=True
    )
    subprocess.run(["git", "add", "."], cwd=paths.root_dir, check=True)
    subprocess.run(["git", "commit", "-m", "chore: init"], cwd=paths.root_dir, check=True)
    return paths


def test_cli_sync_spec_and_check_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify alfred sync-spec writes SPEC.md and --check exits 0 when synced, 1 on drift."""
    paths = _init_cli_git_repo(tmp_path)
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(paths.root_dir))

    rc_sync = main(["sync-spec", "--repo-root", str(paths.root_dir)])
    _ = capsys.readouterr()
    assert rc_sync == 0
    spec_file = paths.root_dir / "docs" / "SPEC.md"
    assert spec_file.is_file()

    rc_ok = main(["sync-spec", "--check", "--repo-root", str(paths.root_dir)])
    _ = capsys.readouterr()
    assert rc_ok == 0

    spec_file.write_text(
        spec_file.read_text(encoding="utf-8").replace("sync-spec", "drifted-cmd"),
        encoding="utf-8",
    )
    rc_drift = main(["sync-spec", "--check", "--repo-root", str(paths.root_dir)])
    _ = capsys.readouterr()
    assert rc_drift == 1


def test_cli_evolve_instance_mode_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify CLI alfred evolve --mode instance --execute applies locally without PR."""
    paths = _init_cli_git_repo(tmp_path)
    monkeypatch.setenv("ALFRED_ROOT_DIR", str(paths.root_dir))
    assert main(["sync-spec", "--repo-root", str(paths.root_dir)]) == 0
    _ = capsys.readouterr()
    subprocess.run(["git", "add", "docs/SPEC.md"], cwd=paths.root_dir, check=True)
    subprocess.run(["git", "commit", "-m", "docs: sync spec"], cwd=paths.root_dir, check=True)

    (paths.instance_dir / "extensions" / "my_ext.py").write_text('"""Ext."""\n', encoding="utf-8")
    rc_inst = main(
        [
            "evolve",
            "--id",
            "inst_cli_1",
            "--title",
            "Perso ext",
            "--description",
            "Local extension",
            "--mode",
            "instance",
            "--execute",
        ]
    )
    inst_out: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert rc_inst == 0
    assert inst_out["status"] == "APPLIED_INSTANCE"
    assert inst_out["mode"] == "instance"


def test_cli_evolve_upstream_breaking_mode_execute(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify CLI alfred evolve --mode upstream-pr --breaking --execute tags PR metadata."""
    paths = _init_cli_git_repo(tmp_path)
    captured_prs: list[tuple[str, str]] = []

    def _pub(_wt: Path, _br: str, _base: str, title: str, body: str) -> str:
        """Capture PR title and body."""
        captured_prs.append((title, body))
        return "https://example.com/pr/55"

    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    evolve = EvolveWorktreeService(
        paths, settings, store, tdd_gate_fn=lambda _p: None, pr_publisher_fn=_pub
    )
    ctrl = build_cli_controller(
        paths=paths, settings=settings, state_store=store, evolve_service=evolve
    )
    wt = evolve.create_isolated_worktree("brk_cli_1")
    (wt / "core" / "alfred" / "controllers" / "extra.py").write_text(
        '"""Extra controller."""\n', encoding="utf-8"
    )
    rc_brk = ctrl.run(
        [
            "evolve",
            "--id",
            "brk_cli_1",
            "--title",
            "Drop legacy API",
            "--description",
            "Breaking flag removal",
            "--mode",
            "upstream-pr",
            "--breaking",
            "--execute",
            "--worktree",
            str(wt),
        ]
    )
    pr_out: dict[str, Any] = json.loads(capsys.readouterr().out)
    assert rc_brk == 0
    assert pr_out["status"] == "OPEN_REMOTE"
    assert captured_prs and captured_prs[0][0].startswith("[BREAKING] ")
    assert "BREAKING CORE CHANGE" in captured_prs[0][1]
    evolve.cleanup_worktree("brk_cli_1")


def test_worktree_docs_spec_and_legacy_cleanup() -> None:
    """Verify V7.1 legacy docs are removed and docs/SPEC.md passes validate_spec_file."""
    from alfred.services.core_spec_service import CoreSpecService

    assert not (WORKTREE_ROOT / "docs" / "vision" / "alfred_vision_and_architecture.md").exists()
    assert not (WORKTREE_ROOT / "docs" / "architecture" / "implementation_plan.md").exists()
    spec_file = WORKTREE_ROOT / "docs" / "SPEC.md"
    assert spec_file.is_file()
    errors = CoreSpecService().validate_spec_file(WORKTREE_ROOT)
    assert errors == [], f"docs/SPEC.md validation errors: {errors}"


def test_worktree_manifests_hooks_pyproject_and_readme() -> None:
    """Verify evolve-qualifier manifest, pre-commit hook, 8.0.0 version, and concise README."""
    eq_path = WORKTREE_ROOT / ".agents" / "agents" / "evolve-qualifier.md"
    assert eq_path.is_file()
    raw_eq = eq_path.read_text(encoding="utf-8")
    assert raw_eq.startswith("---\n")
    fm_block, _ = raw_eq.split("\n---\n", maxsplit=1)
    fm: Any = yaml.safe_load(fm_block[4:])
    assert isinstance(fm, dict)
    assert fm.get("name") == "evolve-qualifier"
    assert fm.get("subagent") is True
    assert fm.get("mainAgent") is False
    assert isinstance(fm.get("tools"), list) and fm["tools"]

    for doc_rel in (
        "AGENTS.md",
        "core/workspaces/alfred-evolve/AGENTS.md",
        ".agents/agents/plan-synthesizer.md",
    ):
        assert "evolve-qualifier" in (WORKTREE_ROOT / doc_rel).read_text(encoding="utf-8")

    tdd_ver = (WORKTREE_ROOT / ".agents" / "agents" / "tdd-verifier.md").read_text(encoding="utf-8")
    assert "ALFRED_ROOT_DIR" in tdd_ver
    assert (WORKTREE_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").is_file()
    pre_commit = (WORKTREE_ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "sync-spec --check" in pre_commit

    pyproject_text = (WORKTREE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'version = "8.0.0"' in pyproject_text

    readme_text = (WORKTREE_ROOT / "README.md").read_text(encoding="utf-8")
    assert len(readme_text.splitlines()) <= 195
    assert "Gemini App" in readme_text
    assert "--onboard" in readme_text
    assert "|" in readme_text
    lower = readme_text.lower()
    assert "cloudflared" in lower
    assert "tls" in lower
    for scope_tok in ("drive", "gmail.modify", "calendar", "tasks", "contacts"):
        assert scope_tok in readme_text
