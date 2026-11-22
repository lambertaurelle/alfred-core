"""Unit tests for V8 living spec AST introspection, evolve governance, and instance coupling."""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.domain.calendar_models import EvolutionProposalRecord
from scripts.check_code_architecture import inspect_python_file

_FEATURE_MODEL_SAMPLE = '''"""Feature flags schema."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

FEATURE_OAUTH_MAP: dict[str, tuple[str, ...]] = {
    "drive_knowledge_sync": ("https://www.googleapis.com/auth/drive",),
    "mcp_gemini_bridge": (),
}

FEATURE_GCP_APIS_MAP: dict[str, tuple[str, ...]] = {
    "drive_knowledge_sync": ("drive.googleapis.com",),
    "mcp_gemini_bridge": (),
}


class FeatureFlagsConfig(BaseModel):
    """Per-capability flags."""

    model_config = ConfigDict(frozen=True)

    drive_knowledge_sync: bool = True
    mcp_gemini_bridge: bool = False
'''

_CLI_SAMPLE = '''"""CLI controller module."""

from __future__ import annotations

import argparse
from typing import Any


def register_parsers(sub: Any) -> None:
    """Register CLI commands."""
    p_sync = sub.add_parser("sync-spec")
    p_sync.add_argument("--check", action="store_true")
    p_sync.add_argument("--repo-root", default=None)
    p_ev = sub.add_parser("evolve")
    p_ev.add_argument("--mode", default="upstream-pr")
    p_ev.add_argument("--breaking", action="store_true")
    _ = argparse.ArgumentParser()
'''


def _build_mini_repo(root: Path) -> Path:
    """Create a minimal synthetic Alfred V8 mono-repo layout for AST spec tests."""
    (root / "pyproject.toml").write_text(
        '[project]\nname = "alfred-core"\nversion = "8.0.0"\n',
        encoding="utf-8",
    )
    for ws, summary in (
        ("alfred-life", "Hub Family Office."),
        ("alfred-evolve", "Worktree TDD."),
    ):
        ws_dir = root / "core" / "workspaces" / ws
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "AGENTS.md").write_text(f"# Workspace {ws}\n\n{summary}\n", encoding="utf-8")

    layers: dict[str, tuple[str, str]] = {
        "domain": ("sample_model.py", '"""Sample domain schema."""\n'),
        "repositories": ("sample_repo.py", '"""Sample repository."""\n'),
        "services": (
            "sample_svc.py",
            '"""Sample service."""\n\nraise RuntimeError("must not import dynamically")\n',
        ),
        "controllers": ("cli_controller.py", _CLI_SAMPLE),
    }
    for layer, (filename, content) in layers.items():
        d = root / "core" / "alfred" / layer
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").write_text('"""Package."""\n', encoding="utf-8")
        (d / filename).write_text(content, encoding="utf-8")

    (root / "core" / "alfred" / "domain" / "feature_models.py").write_text(
        _FEATURE_MODEL_SAMPLE, encoding="utf-8"
    )
    (root / "core" / "alfred" / "main.py").write_text('"""CLI entrypoint."""\n', encoding="utf-8")
    return root


def test_core_spec_service_inspects_ast_snapshot_without_importing(tmp_path: Path) -> None:
    """Verify CoreSpecService extracts version, workspaces, features, layers, and CLI via AST."""
    from alfred.domain.evolve_models import (
        CliCommandSpec,
        CoreModuleEntry,
        CoreSpecSnapshot,
        FeatureSpecEntry,
        WorkspaceSpecEntry,
    )
    from alfred.services.core_spec_service import CoreSpecService

    repo = _build_mini_repo(tmp_path)
    svc = CoreSpecService()
    snap = svc.inspect_core_snapshot(repo)

    assert isinstance(snap, CoreSpecSnapshot)
    assert snap.version == "8.0.0"
    assert all(isinstance(w, WorkspaceSpecEntry) for w in snap.workspaces)
    assert {w.name for w in snap.workspaces} == {"alfred-life", "alfred-evolve"}

    feat_map = {f.flag_name: f for f in snap.features}
    assert all(isinstance(f, FeatureSpecEntry) for f in snap.features)
    assert feat_map["drive_knowledge_sync"].default_enabled is True
    assert "drive.googleapis.com" in feat_map["drive_knowledge_sync"].gcp_apis
    assert "https://www.googleapis.com/auth/drive" in feat_map["drive_knowledge_sync"].oauth_scopes
    assert feat_map["mcp_gemini_bridge"].default_enabled is False

    assert all(isinstance(m, CoreModuleEntry) for m in snap.modules)
    layers = {m.layer for m in snap.modules}
    assert {"domain", "repositories", "services", "controllers", "main"}.issubset(layers)
    assert any("Sample service." in m.summary for m in snap.modules)

    cmd_map = {c.name: c for c in snap.cli_commands}
    assert all(isinstance(c, CliCommandSpec) for c in snap.cli_commands)
    assert "--check" in cmd_map["sync-spec"].options
    assert "--repo-root" in cmd_map["sync-spec"].options
    assert "--mode" in cmd_map["evolve"].options
    assert "--breaking" in cmd_map["evolve"].options


def test_core_spec_service_sync_preserves_curated_prose_and_validates(tmp_path: Path) -> None:
    """Verify sync_spec_file creates/updates generated block while preserving human prose."""
    from alfred.services.core_spec_service import (
        BEGIN_GENERATED,
        END_GENERATED,
        CoreSpecService,
    )

    repo = _build_mini_repo(tmp_path)
    svc = CoreSpecService()

    spec_path = svc.sync_spec_file(repo)
    assert spec_path == repo / "docs" / "SPEC.md"
    raw = spec_path.read_text(encoding="utf-8")
    assert BEGIN_GENERATED in raw and END_GENERATED in raw
    assert svc.validate_spec_file(repo) == []

    curated_header = "# Alfred Living Spec\n\nProse humaine curatee en tete.\n\n"
    curated_footer = "\n\n## Decisions Architecturales\n\nConserver strictement ce texte.\n"
    spec_path.write_text(
        f"{curated_header}{BEGIN_GENERATED}\nstale\n{END_GENERATED}{curated_footer}",
        encoding="utf-8",
    )
    (repo / "core" / "alfred" / "services" / "new_generic_svc.py").write_text(
        '"""New generic service."""\n', encoding="utf-8"
    )

    svc.sync_spec_file(repo)
    updated = spec_path.read_text(encoding="utf-8")
    assert updated.startswith(curated_header)
    assert updated.endswith(curated_footer)
    assert "new_generic_svc" in updated
    assert svc.validate_spec_file(repo) == []


def test_core_spec_service_validate_detects_drift_legacy_docs_and_banned_terms(
    tmp_path: Path,
) -> None:
    """Verify validate_spec_file flags missing docs, drift, V7.1 legacy docs, and banned terms."""
    from alfred.services.core_spec_service import CoreSpecService

    repo = _build_mini_repo(tmp_path)
    svc = CoreSpecService()
    assert svc.validate_spec_file(repo)

    spec_path = svc.sync_spec_file(repo)
    assert svc.validate_spec_file(repo) == []

    original_spec = spec_path.read_text(encoding="utf-8")
    spec_path.write_text("# No markers here\n", encoding="utf-8")
    assert svc.validate_spec_file(repo)

    spec_path.write_text(original_spec + "\n<!-- drift -->\n", encoding="utf-8")
    (repo / "core" / "alfred" / "services" / "undocumented.py").write_text(
        '"""Undocumented in spec."""\n', encoding="utf-8"
    )
    assert svc.validate_spec_file(repo)
    (repo / "core" / "alfred" / "services" / "undocumented.py").unlink()
    svc.sync_spec_file(repo)

    for rel_legacy in (
        "docs/vision/alfred_vision_and_architecture.md",
        "docs/architecture/implementation_plan.md",
    ):
        legacy_file = repo / rel_legacy
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_text("# Old V7.1\n", encoding="utf-8")
        assert svc.validate_spec_file(repo)
        legacy_file.unlink()

    banned = "nou" + "nou"
    clean_text = spec_path.read_text(encoding="utf-8")
    spec_path.write_text(f"{clean_text}\nMention {banned} interdite.\n", encoding="utf-8")
    assert svc.validate_spec_file(repo)
    spec_path.write_text(clean_text, encoding="utf-8")

    ext_dir = repo / "instance" / "extensions"
    ext_dir.mkdir(parents=True, exist_ok=True)
    (ext_dir / "_ignored_helper.py").write_text('"""Ignored."""\n', encoding="utf-8")
    (ext_dir / "sample_ignored.example.py").write_text('"""Ignored example."""\n', encoding="utf-8")
    (ext_dir / "custom_private_ext.py").write_text(
        '"""Private."""\n\nclass Ext:\n    """Ext."""\n'
        '    @property\n    def extension_id(self) -> str:\n        return "secret_ext_token"\n',
        encoding="utf-8",
    )
    assert svc.validate_spec_file(repo) == []
    spec_path.write_text(f"{clean_text}\nLeak secret_ext_token here.\n", encoding="utf-8")
    assert svc.validate_spec_file(repo)


def test_evolve_governance_qualify_use_case_and_hybrid_validation() -> None:
    """Verify 4 use-case qualification paths and ValueError on incomplete hybrid evolutions."""
    from alfred.domain.evolve_models import (
        EvolutionArchitectureKind,
        EvolutionMode,
        UseCaseQualification,
    )
    from alfred.services.evolve_governance_service import EvolveGovernanceService

    gov = EvolveGovernanceService()

    pure = gov.qualify_use_case(touches_core=False, reusable_for_community=False)
    assert isinstance(pure, UseCaseQualification)
    assert pure.architecture_kind == EvolutionArchitectureKind.PURE_INSTANCE
    assert pure.recommended_mode == EvolutionMode.INSTANCE

    comm = gov.qualify_use_case(touches_core=False, reusable_for_community=True)
    assert comm.architecture_kind == EvolutionArchitectureKind.INSTANCE_WITH_COMMUNITY_OPTION
    assert comm.recommended_mode == EvolutionMode.INSTANCE
    assert comm.community_pr_option_summary.strip() != ""

    hyb_bad = gov.qualify_use_case(
        touches_core=True, is_hybrid=True, core_is_standalone_complete=False
    )
    assert hyb_bad.architecture_kind == EvolutionArchitectureKind.HYBRID_CORE_AND_INSTANCE
    assert hyb_bad.recommended_mode == EvolutionMode.INSTANCE
    assert hyb_bad.core_feature_is_standalone_complete is False
    assert hyb_bad.rearchitecture_suggestion.strip() != ""

    hyb_ok = gov.qualify_use_case(
        touches_core=True, is_hybrid=True, core_is_standalone_complete=True
    )
    assert hyb_ok.architecture_kind == EvolutionArchitectureKind.HYBRID_CORE_AND_INSTANCE
    assert hyb_ok.recommended_mode == EvolutionMode.UPSTREAM_PR
    assert hyb_ok.core_feature_is_standalone_complete is True

    core_ev = gov.qualify_use_case(touches_core=True, is_hybrid=False)
    assert core_ev.architecture_kind == EvolutionArchitectureKind.CORE_EVOLUTION
    assert core_ev.recommended_mode == EvolutionMode.UPSTREAM_PR

    with pytest.raises(ValueError):
        gov.validate_hybrid_completeness(is_hybrid=True, core_is_standalone_complete=False)
    gov.validate_hybrid_completeness(is_hybrid=True, core_is_standalone_complete=True)
    gov.validate_hybrid_completeness(is_hybrid=False, core_is_standalone_complete=False)


def test_evolve_governance_format_pr_metadata_and_record_backcompat() -> None:
    """Verify [BREAKING] metadata formatting without duplicates and record defaults."""
    from alfred.domain.evolve_models import BreakingChangeReport
    from alfred.services.evolve_governance_service import EvolveGovernanceService

    gov = EvolveGovernanceService()
    clean_rep = BreakingChangeReport(is_breaking=False)
    t_clean, b_clean = gov.format_pr_metadata("feat: add helper", "Summary", clean_rep)
    assert t_clean == "feat: add helper"
    assert "BREAKING CORE CHANGE" not in b_clean

    brk_rep = BreakingChangeReport(
        is_breaking=True,
        removed_cli_commands=["old-cmd"],
        summary_markdown="- Removed `old-cmd`",
    )
    t_brk, b_brk = gov.format_pr_metadata("feat: drop old cmd", "Details", brk_rep)
    assert t_brk == "[BREAKING] feat: drop old cmd"
    assert b_brk.startswith("## ⚠️ BREAKING CORE CHANGE — Arbitrage Mainteneur Requis")
    assert "Details" in b_brk

    t_forced, _ = gov.format_pr_metadata("[BREAKING] Already tagged", "Body", clean_rep, True)
    assert t_forced == "[BREAKING] Already tagged"
    assert t_forced.count("[BREAKING]") == 1

    rec = EvolutionProposalRecord(proposal_id="p1", title="T", description="D")
    assert rec.mode == "upstream-pr"
    assert rec.is_breaking is False
    legacy = EvolutionProposalRecord.model_validate(
        {"proposal_id": "p0", "title": "Legacy", "description": "Old ledger entry"}
    )
    assert legacy.mode == "upstream-pr"
    assert legacy.is_breaking is False


def test_check_code_architecture_rejects_instance_coupling_under_core(tmp_path: Path) -> None:
    """Verify AST check rejects import instance... / from instance... under core/."""
    svc_dir = tmp_path / "core" / "alfred" / "services"
    svc_dir.mkdir(parents=True)
    bad_from = svc_dir / "coupled_from.py"
    bad_from.write_text(
        '"""Coupled service."""\n\nfrom instance.extensions import private_routine\n',
        encoding="utf-8",
    )
    bad_import = svc_dir / "coupled_import.py"
    bad_import.write_text(
        '"""Coupled service."""\n\nimport instance.config\n',
        encoding="utf-8",
    )
    scripts_bad = tmp_path / "core" / "scripts" / "coupled_script.py"
    scripts_bad.parent.mkdir(parents=True)
    scripts_bad.write_text(
        '"""Coupled script."""\n\nimport instance\n',
        encoding="utf-8",
    )

    for target in (bad_from, bad_import, scripts_bad):
        violations = inspect_python_file(target, repo_root=tmp_path)
        assert any("Instance Coupling" in v for v in violations), f"Expected violation on {target}"
