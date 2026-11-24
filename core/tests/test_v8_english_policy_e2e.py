"""End-to-end tests for English-only CLI guard, doc-maintainer manifest, hooks, and README."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

WORKTREE_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = WORKTREE_ROOT / "core" / "scripts" / "check_english_only.py"


def _run_check_english_cli(repo_root: Path) -> subprocess.CompletedProcess[str]:
    """Execute check_english_only.py as a standalone CLI subprocess against repo_root."""
    assert SCRIPT_PATH.is_file(), f"Missing English-only script: {SCRIPT_PATH}"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["ALFRED_ROOT_DIR"] = str(repo_root)
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo-root", str(repo_root)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_worktree_check_english_only_cli_exits_zero() -> None:
    """Verify check_english_only.py passes with exit code 0 on the full worktree."""
    res = _run_check_english_cli(WORKTREE_ROOT)
    assert res.returncode == 0, f"check_english_only.py failed:\n{res.stderr}\n{res.stdout}"


def test_check_english_only_cli_rejects_synthetic_french_repos(tmp_path: Path) -> None:
    """Verify check_english_only.py exits 1 when French enters README, AGENTS, or docstrings."""
    (tmp_path / "core" / "alfred" / "services").mkdir(parents=True)
    readme = tmp_path / "README.md"
    agents = tmp_path / "AGENTS.md"
    svc = tmp_path / "core" / "alfred" / "services" / "demo.py"

    def _reset_clean() -> None:
        """Reset all files in synthetic repo to clean English."""
        readme.write_text("# Alfred\n\nClean English README.\n", encoding="utf-8")
        agents.write_text("# Agents\n\nClean English governance.\n", encoding="utf-8")
        svc.write_text('"""Clean English module docstring."""\n', encoding="utf-8")

    _reset_clean()
    ok_res = _run_check_english_cli(tmp_path)
    assert ok_res.returncode == 0, ok_res.stderr + ok_res.stdout

    readme.write_text("# Alfred\n\nGuide d'installation en français.\n", encoding="utf-8")
    assert _run_check_english_cli(tmp_path).returncode == 1
    _reset_clean()

    agents.write_text("# Agents\n\nRègles pour l'utilisateur.\n", encoding="utf-8")
    assert _run_check_english_cli(tmp_path).returncode == 1
    _reset_clean()

    svc.write_text('"""Documentation française du module."""\n', encoding="utf-8")
    assert _run_check_english_cli(tmp_path).returncode == 1


def test_doc_maintainer_manifest_and_policy_contract() -> None:
    """Verify .agents/agents/doc-maintainer.md exists with valid frontmatter and boundaries."""
    from alfred.services.english_policy_service import detect_french_in_text

    doc_agent = WORKTREE_ROOT / ".agents" / "agents" / "doc-maintainer.md"
    assert doc_agent.is_file(), f"Missing doc-maintainer manifest: {doc_agent}"
    raw = doc_agent.read_text(encoding="utf-8")
    assert detect_french_in_text(raw) == []
    assert raw.startswith("---\n")
    fm_block, body = raw.split("\n---\n", maxsplit=1)
    fm: Any = yaml.safe_load(fm_block[4:])
    assert isinstance(fm, dict)
    assert fm.get("name") == "doc-maintainer"
    assert fm.get("subagent") is True
    assert fm.get("mainAgent") is False
    assert isinstance(fm.get("tools"), list) and fm["tools"]

    lower = body.lower()
    assert "core/tests/" in body and "core/alfred/" in body
    assert any(tok in lower for tok in ("forbid", "never edit", "must not", "prohibit"))
    assert "readme.md" in lower and "docs/spec.md" in lower and "sync-spec" in lower
    assert "check_english_only.py" in body or "english" in lower
    assert "bilingual" in lower or ("french" in lower and "english" in lower)


def test_governance_manifests_and_hooks_wire_doc_maintainer_and_english_gate() -> None:
    """Verify manifests, PR template, and git hooks wire doc-maintainer & English gate."""
    from alfred.services.english_policy_service import detect_french_in_text

    markdown_targets = (
        "AGENTS.md",
        "core/workspaces/alfred-evolve/AGENTS.md",
        ".agents/agents/evolve-qualifier.md",
        ".agents/agents/plan-synthesizer.md",
        ".agents/agents/tdd-green.md",
        ".agents/agents/tdd-verifier.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
    )
    for rel in markdown_targets:
        path = WORKTREE_ROOT / rel
        assert path.is_file(), f"Missing file: {path}"
        text = path.read_text(encoding="utf-8")
        assert detect_french_in_text(text) == [], f"French prose found in {rel}"
        assert "doc-maintainer" in text, f"Missing doc-maintainer reference in {rel}"
        assert "check_english_only.py" in text, f"Missing check_english_only.py in {rel}"

    for hook_rel in (".githooks/pre-commit", ".pre-commit-config.yaml"):
        hook_path = WORKTREE_ROOT / hook_rel
        assert hook_path.is_file(), f"Missing hook file: {hook_path}"
        hook_text = hook_path.read_text(encoding="utf-8")
        assert detect_french_in_text(hook_text) == [], f"French prose found in {hook_rel}"
        assert "check_english_only.py" in hook_text, f"Missing check_english_only.py in {hook_rel}"


def test_operational_workspaces_and_subagents_are_english_and_document_bilingual_runtime() -> None:
    """Verify 6 operational workspaces & subagents are English and note FR/EN runtime."""
    from alfred.services.english_policy_service import detect_french_in_text

    op_workspaces = ("onboard", "life", "inbox", "calendar", "tasks", "drive")
    for slug in op_workspaces:
        ws_md = WORKTREE_ROOT / "core" / "workspaces" / f"alfred-{slug}" / "AGENTS.md"
        assert ws_md.is_file(), f"Missing workspace manifest: {ws_md}"
        content = ws_md.read_text(encoding="utf-8")
        assert detect_french_in_text(content) == [], f"French prose in {ws_md}"
        lower = content.lower()
        assert "bilingual" in lower or ("french" in lower and "english" in lower), (
            f"Missing bilingual FR/EN runtime documentation in {ws_md}"
        )

    op_agents = (
        "drive-custodian.md",
        "plan-executor.md",
        "plan-synthesizer.md",
        "research-drive.md",
        "research-gmail-cal.md",
        "research-web.md",
        "study-orchestrator.md",
    )
    for fname in op_agents:
        agent_md = WORKTREE_ROOT / ".agents" / "agents" / fname
        assert agent_md.is_file(), f"Missing subagent manifest: {agent_md}"
        content = agent_md.read_text(encoding="utf-8")
        assert detect_french_in_text(content) == [], f"French prose in {agent_md}"
        lower = content.lower()
        assert "bilingual" in lower or ("french" in lower and "english" in lower), (
            f"Missing bilingual FR/EN runtime documentation in {agent_md}"
        )

    tdd_red = WORKTREE_ROOT / ".agents" / "agents" / "tdd-red.md"
    assert detect_french_in_text(tdd_red.read_text(encoding="utf-8")) == []


def test_readme_english_budget_infographic_bilingual_note_and_sync_spec_cli() -> None:
    """Verify English README constraints and alfred sync-spec --check exit code 0 on worktree."""
    from alfred.services.english_policy_service import detect_french_in_text

    readme_path = WORKTREE_ROOT / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    lines = readme.splitlines()
    assert len(lines) <= 195, f"README.md exceeds 195 lines ({len(lines)})."
    assert detect_french_in_text(readme) == []

    intro = readme.split("\n## ", maxsplit=1)[0]
    assert re.search(r"!\[[^\]]+\]\(docs/assets/alfred-workflow\.jpg\)", intro) is not None
    lower = readme.lower()
    assert "bilingual" in lower or ("french" in lower and "english" in lower)

    env = {
        **os.environ,
        "PYTHONPATH": str(WORKTREE_ROOT / "core"),
        "ALFRED_ROOT_DIR": str(WORKTREE_ROOT),
    }
    res = subprocess.run(
        [
            sys.executable,
            "-m",
            "alfred.main",
            "sync-spec",
            "--check",
            "--repo-root",
            str(WORKTREE_ROOT),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"sync-spec --check failed:\n{res.stdout}\n{res.stderr}"
