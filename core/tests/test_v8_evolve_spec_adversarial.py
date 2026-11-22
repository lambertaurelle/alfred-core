"""Adversarial security & isolation tests for V8 living spec, evolve modes, and core purity."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alfred.controllers.app_factory import build_cli_controller
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.repositories.state_store import StateStore
from alfred.services.evolve_worktree_service import EvolveWorktreeService
from scripts.check_code_architecture import inspect_python_file

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


def _init_adv_git_repo(tmp_path: Path) -> AlfredPaths:
    """Create a clean synced Git mono-repo fixture for adversarial evolve isolation tests."""
    from alfred.services.core_spec_service import CoreSpecService

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
    dom_dir = paths.core_dir / "alfred" / "domain"
    dom_dir.mkdir(parents=True)
    (dom_dir / "__init__.py").write_text('"""Domain."""\n', encoding="utf-8")
    (dom_dir / "feature_models.py").write_text(
        '"""Flags."""\nfrom pydantic import BaseModel\n'
        'class FeatureFlagsConfig(BaseModel):\n    """Flags."""\n'
        "    drive_knowledge_sync: bool = True\n",
        encoding="utf-8",
    )
    (paths.instance_dir / "extensions").mkdir(parents=True)
    (paths.instance_dir / "extensions" / "__init__.py").write_text('"""Ext."""\n', encoding="utf-8")
    CoreSpecService(paths).sync_spec_file(paths.root_dir)
    subprocess.run(["git", "init", "-b", "main"], cwd=paths.root_dir, check=True)
    subprocess.run(["git", "config", "user.name", "Maintainer"], cwd=paths.root_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "alfred@example.com"], cwd=paths.root_dir, check=True
    )
    subprocess.run(["git", "add", "."], cwd=paths.root_dir, check=True)
    subprocess.run(["git", "commit", "-m", "chore: init"], cwd=paths.root_dir, check=True)
    return paths


def test_adversarial_malicious_extension_ast_extraction_without_exec(tmp_path: Path) -> None:
    """Verify validate_spec_file uses pure AST on extensions without executing top-level code."""
    from alfred.services.core_spec_service import CoreSpecService

    paths = _init_adv_git_repo(tmp_path)
    svc = CoreSpecService(paths)
    malicious = paths.instance_dir / "extensions" / "malicious_ext.py"
    malicious.write_text(
        '"""Malicious extension trap."""\n\n'
        'raise RuntimeError("must not import")\n\n'
        'class Trap:\n    """Trap."""\n    extension_id = "secret_custom_routine"\n',
        encoding="utf-8",
    )
    live_ext_dir = tmp_path / "external_live_instance" / "extensions"
    live_ext_dir.mkdir(parents=True)
    (live_ext_dir / "live_trap.py").write_text(
        '"""Live trap."""\n\nraise RuntimeError("must not exec live")\n\n'
        'class LiveTrap:\n    """Live."""\n    @property\n'
        '    def extension_id(self) -> str:\n        return "live_secret_routine"\n',
        encoding="utf-8",
    )

    assert svc.validate_spec_file(paths.root_dir, instance_dirs=[live_ext_dir]) == []
    spec_path = paths.root_dir / "docs" / "SPEC.md"
    clean_spec = spec_path.read_text(encoding="utf-8")

    for leaked_tok in ("secret_custom_routine", "malicious_ext", "live_secret_routine"):
        spec_path.write_text(f"{clean_spec}\nLeak {leaked_tok}\n", encoding="utf-8")
        errs = svc.validate_spec_file(paths.root_dir, instance_dirs=[live_ext_dir])
        assert errs, f"Expected leak error for {leaked_tok}"


def test_adversarial_instance_import_infiltration_under_core_rejected(tmp_path: Path) -> None:
    """Verify top-level and deferred imports from instance inside core/ fail AST check."""
    svc_dir = tmp_path / "core" / "alfred" / "services"
    svc_dir.mkdir(parents=True)
    direct = svc_dir / "sneaky_direct.py"
    direct.write_text(
        '"""Sneaky."""\n\nfrom instance.extensions import secret\n',
        encoding="utf-8",
    )
    deferred = svc_dir / "sneaky_deferred.py"
    deferred.write_text(
        '"""Sneaky deferred."""\n\n'
        'def helper() -> None:\n    """Run."""\n    import instance\n    _ = instance\n',
        encoding="utf-8",
    )

    for target in (direct, deferred):
        violations = inspect_python_file(target, repo_root=tmp_path)
        assert any("Instance Coupling" in v for v in violations)


def test_adversarial_hybrid_half_baked_blocked_by_service_and_cli(tmp_path: Path) -> None:
    """Verify --hybrid without --core-complete is rejected in both service and CLI."""
    paths = _init_adv_git_repo(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    evolve = EvolveWorktreeService(paths, settings, store, tdd_gate_fn=lambda _p: None)
    wt = evolve.create_isolated_worktree("half_hyb")

    with pytest.raises(ValueError):
        evolve.execute_and_open_pr(
            "half_hyb", "feat: half", "Depends on instance", wt, is_hybrid=True, core_complete=False
        )

    ctrl = build_cli_controller(
        paths=paths, settings=settings, state_store=store, evolve_service=evolve
    )
    with pytest.raises(ValueError):
        ctrl.run(
            [
                "evolve",
                "--id",
                "half_hyb",
                "--title",
                "Half hybrid",
                "--description",
                "Half",
                "--mode",
                "upstream-pr",
                "--hybrid",
                "--execute",
                "--worktree",
                str(wt),
            ]
        )
    evolve.cleanup_worktree("half_hyb")


def test_adversarial_mode_instance_blocks_core_docs_and_manifest_edits(tmp_path: Path) -> None:
    """Verify --mode instance rejects edits to core/, docs/, AGENTS.md, and pyproject.toml."""
    from alfred.services.evolve_governance_service import EvolveGovernanceService

    paths = _init_adv_git_repo(tmp_path)
    gov = EvolveGovernanceService()
    for sensitive in (
        "core/alfred/services/sneaky.py",
        "docs/SPEC.md",
        ".agents/agents/evolve-qualifier.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
        "README.md",
        "AGENTS.md",
        "pyproject.toml",
        "scripts/bootstrap.sh",
    ):
        with pytest.raises(PermissionError):
            gov.verify_instance_only_paths(paths.root_dir, [sensitive])

    (paths.core_dir / "alfred" / "domain" / "tampered.py").write_text(
        '"""Tampered."""\n', encoding="utf-8"
    )
    evolve = EvolveWorktreeService(paths, AlfredSettings.load(paths), StateStore(paths))
    with pytest.raises(PermissionError):
        evolve.execute_instance_only("tamper", "Tamper", "Bad", repo_root=paths.root_dir)


def test_adversarial_contaminated_worktree_instance_files_rejected(tmp_path: Path) -> None:
    """Verify tracked private instance files in a worktree fail isolation & leak gates."""
    from alfred.services.evolve_governance_service import EvolveGovernanceService

    paths = _init_adv_git_repo(tmp_path)
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    evolve = EvolveWorktreeService(paths, settings, store, tdd_gate_fn=lambda _p: None)
    wt = evolve.create_isolated_worktree("contaminated")

    (wt / "instance" / "config").mkdir(parents=True, exist_ok=True)
    cfg = wt / "instance" / "config" / "settings.yaml"
    cfg.write_text("project_name: Personal\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "instance/config/settings.yaml"], cwd=wt, check=True)

    ext = wt / "instance" / "extensions" / "active.py"
    ext.write_text('"""Private active extension."""\n', encoding="utf-8")
    subprocess.run(["git", "add", "-f", "instance/extensions/active.py"], cwd=wt, check=True)

    iso_errors = EvolveGovernanceService().verify_standalone_core_isolation(wt)
    assert iso_errors

    with pytest.raises(RuntimeError, match="(?i)leak|instance"):
        evolve._run_leak_and_ext_checks(wt)
    evolve.cleanup_worktree("contaminated")
