"""Functional tests for V8 evolve modes (instance, upstream-pr, local-fork) and PR fork."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.repositories.state_store import StateStore
from alfred.services.evolve_worktree_service import EvolveWorktreeService

WORKTREE_ROOT = Path(__file__).resolve().parents[2]


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a deterministic git command inside cwd for test fixture setup."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_synced_mono_repo(tmp_path: Path) -> AlfredPaths:
    """Create a realistic V8 mono-repo Git fixture with synced docs/SPEC.md on main."""
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
        "instance/extensions/*.py\n!instance/extensions/__init__.py\n"
        "!instance/extensions/*.example.py\n",
        encoding="utf-8",
    )
    ws_dir = paths.core_dir / "workspaces" / "alfred-evolve"
    ws_dir.mkdir(parents=True)
    (ws_dir / "AGENTS.md").write_text("# Workspace alfred-evolve\n\nTDD.\n", encoding="utf-8")

    dom_dir = paths.core_dir / "alfred" / "domain"
    svc_dir = paths.core_dir / "alfred" / "services"
    ctl_dir = paths.core_dir / "alfred" / "controllers"
    for d in (dom_dir, svc_dir, ctl_dir):
        d.mkdir(parents=True)
        (d / "__init__.py").write_text('"""Pkg."""\n', encoding="utf-8")
    (dom_dir / "feature_models.py").write_text(
        '"""Flags."""\nfrom pydantic import BaseModel\n'
        'class FeatureFlagsConfig(BaseModel):\n    """Flags."""\n'
        "    drive_knowledge_sync: bool = True\n",
        encoding="utf-8",
    )
    (svc_dir / "legacy_helper.py").write_text('"""Legacy helper."""\n', encoding="utf-8")
    (ctl_dir / "cli_controller.py").write_text(
        '"""CLI."""\n\n\ndef reg(sub: object) -> None:\n'
        '    """Reg.\n\n    Args:\n        sub: Parser.\n    """\n'
        '    sub.add_parser("legacy-cmd")\n',
        encoding="utf-8",
    )
    (paths.instance_dir / "extensions").mkdir(parents=True)
    (paths.instance_dir / "extensions" / "__init__.py").write_text('"""Ext."""\n', encoding="utf-8")
    (paths.instance_dir / "examples").mkdir(parents=True)
    CoreSpecService(paths).sync_spec_file(paths.root_dir)

    _git(paths.root_dir, "init", "-b", "main")
    _git(paths.root_dir, "config", "user.name", "Alfred Maintainer")
    _git(paths.root_dir, "config", "user.email", "alfred@example.com")
    _git(paths.root_dir, "add", ".")
    _git(paths.root_dir, "commit", "-m", "chore: initial synced core")
    return paths


def test_mode_instance_applies_without_pr_and_blocks_core_paths(tmp_path: Path) -> None:
    """Verify --mode instance updates instance/ only, keeps SPEC intact, and blocks core edits."""
    from alfred.services.evolve_governance_service import EvolveGovernanceService

    paths = _init_synced_mono_repo(tmp_path)
    spec_before = (paths.root_dir / "docs" / "SPEC.md").read_bytes()
    gov = EvolveGovernanceService()
    gov.verify_instance_only_paths(
        paths.root_dir, ["instance/extensions/my_routine.py", "instance/config/settings.yaml"]
    )
    for forbidden in ("core/alfred/services/foo.py", "docs/SPEC.md", "scripts/bootstrap.sh"):
        with pytest.raises(PermissionError):
            gov.verify_instance_only_paths(paths.root_dir, [forbidden])

    store = StateStore(paths)
    evolve = EvolveWorktreeService(paths, AlfredSettings.load(paths), store)
    rec, _ = evolve.prepare_evolution_plan("inst_01", "Perso ext", "Private", mode="instance")
    assert rec.mode == "instance"

    (paths.instance_dir / "extensions" / "my_routine.py").write_text(
        '"""Private."""\n', encoding="utf-8"
    )
    applied = evolve.execute_instance_only(
        "inst_01", "Perso ext", "Private", repo_root=paths.root_dir
    )
    assert applied.status == "APPLIED_INSTANCE"
    assert applied.mode == "instance"
    assert applied.pull_requests == []
    assert (paths.root_dir / "docs" / "SPEC.md").read_bytes() == spec_before

    (paths.core_dir / "alfred" / "services" / "legacy_helper.py").write_text(
        '"""Modified core."""\n', encoding="utf-8"
    )
    with pytest.raises(PermissionError):
        evolve.execute_instance_only(
            "inst_01", "Perso ext", "Private", repo_root=paths.root_dir
        )


def test_mode_upstream_pr_syncs_spec_and_guards_hybrid_completeness(tmp_path: Path) -> None:
    """Verify upstream-pr auto-syncs docs/SPEC.md and enforces hybrid completeness."""
    paths = _init_synced_mono_repo(tmp_path)
    store = StateStore(paths)
    captured: list[tuple[str, str]] = []

    def _pub(_wt: Path, _br: str, _base: str, title: str, body: str) -> str:
        """Record PR title/body and return synthetic URL."""
        captured.append((title, body))
        return "https://example.com/pr/10"

    evolve = EvolveWorktreeService(
        paths,
        AlfredSettings.load(paths),
        store,
        tdd_gate_fn=lambda _p: None,
        pr_publisher_fn=_pub,
    )
    evolve.prepare_evolution_plan("feat_gen", "Generic parser", "Adds generic tool")
    wt = evolve.create_isolated_worktree("feat_gen")
    (wt / "core" / "alfred" / "services" / "generic_parser.py").write_text(
        '"""Generic receipt parser."""\n', encoding="utf-8"
    )

    with pytest.raises(ValueError):
        evolve.execute_and_open_pr(
            "feat_gen", "feat: half hybrid", "Half", wt, is_hybrid=True, core_complete=False
        )

    pr = evolve.execute_and_open_pr(
        "feat_gen",
        "feat: generic parser",
        "Complete standalone core capability",
        wt,
        is_hybrid=True,
        core_complete=True,
    )
    assert pr.status == "OPEN_REMOTE"
    assert "docs/SPEC.md" in pr.changed_files
    assert "generic_parser" in (wt / "docs" / "SPEC.md").read_text(encoding="utf-8")
    assert captured and not captured[0][0].startswith("[BREAKING]")
    evolve.cleanup_worktree("feat_gen")


def test_mode_upstream_pr_detects_breaking_changes_and_tags_pr(tmp_path: Path) -> None:
    """Verify removing a core module or CLI command triggers [BREAKING] PR metadata."""
    from alfred.services.evolve_governance_service import EvolveGovernanceService

    paths = _init_synced_mono_repo(tmp_path)
    store = StateStore(paths)
    captured: list[tuple[str, str]] = []

    def _pub(_wt: Path, _br: str, _base: str, title: str, body: str) -> str:
        """Capture PR title/body."""
        captured.append((title, body))
        return "https://example.com/pr/99"

    evolve = EvolveWorktreeService(
        paths,
        AlfredSettings.load(paths),
        store,
        tdd_gate_fn=lambda _p: None,
        pr_publisher_fn=_pub,
    )
    evolve.prepare_evolution_plan("drop_legacy", "Drop legacy", "Remove legacy command")
    wt = evolve.create_isolated_worktree("drop_legacy")
    (wt / "core" / "alfred" / "services" / "legacy_helper.py").unlink()
    (wt / "core" / "alfred" / "controllers" / "cli_controller.py").write_text(
        '"""CLI."""\n\n\ndef reg(sub: object) -> None:\n'
        '    """Reg.\n\n    Args:\n        sub: Parser.\n    """\n    _ = sub\n',
        encoding="utf-8",
    )

    report = EvolveGovernanceService().detect_breaking_core_changes(wt, base_branch="main")
    assert report.is_breaking is True
    assert "legacy-cmd" in report.removed_cli_commands
    assert any("legacy_helper" in m for m in report.removed_modules)

    evolve.execute_and_open_pr("drop_legacy", "refactor: drop legacy", "Cleanup.", wt)
    assert captured
    pr_title, pr_body = captured[0]
    assert pr_title.startswith("[BREAKING] ")
    assert "## ⚠️ BREAKING CORE CHANGE — Maintainer Approval Required" in pr_body
    persisted = store.evolution_ledger.load().proposals["drop_legacy"]
    assert persisted.is_breaking is True
    evolve.cleanup_worktree("drop_legacy")


def test_publish_github_pr_falls_back_to_fork_on_push_403(tmp_path: Path) -> None:
    """Verify publish_github_pr falls back to gh repo fork + fork push when origin rejects push."""
    from alfred.services.gh_pr_publisher import publish_github_pr

    calls: list[list[str]] = []

    def _runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Simulate origin 403 rejection followed by fork, push, api login, and PR creation."""
        calls.append(list(cmd))
        if cmd[:4] == ["git", "remote", "get-url", "origin"]:
            return subprocess.CompletedProcess(
                cmd, 0, "https://github.com/upstream-org/alfred.git\n", ""
            )
        if cmd[:4] == ["git", "push", "-u", "origin"]:
            return subprocess.CompletedProcess(cmd, 1, "", "remote: Permission denied (403)")
        if cmd[:3] == ["gh", "repo", "fork"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:4] == ["git", "push", "-u", "fork"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(cmd, 0, "alice-dev\n", "")
        if cmd[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                cmd, 0, "https://github.com/upstream-org/alfred/pull/7\n", ""
            )
        return subprocess.CompletedProcess(cmd, 1, "", "unexpected")

    pr_url = publish_github_pr(
        tmp_path,
        "evolve/feat-x",
        "main",
        "feat: x",
        "Body",
        git_env={"PATH": "/usr/bin"},
        draft=True,
        runner_fn=_runner,
        gh_locator_fn=lambda: "gh",
    )
    assert pr_url == "https://github.com/upstream-org/alfred/pull/7"
    assert any(c[:3] == ["gh", "repo", "fork"] and "--remote-name" in c for c in calls)
    assert ["git", "push", "-u", "fork", "evolve/feat-x"] in calls
    pr_cmd = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert "--repo" in pr_cmd and "upstream-org/alfred" in pr_cmd
    assert "--head" in pr_cmd and "alice-dev:evolve/feat-x" in pr_cmd
    assert "--draft" in pr_cmd

    no_gh = publish_github_pr(
        tmp_path,
        "evolve/feat-x",
        "main",
        "feat: x",
        "Body",
        git_env={},
        runner_fn=_runner,
        gh_locator_fn=lambda: None,
    )
    assert no_gh is None


def test_mode_local_fork_and_leak_guard_branch_isolation(tmp_path: Path) -> None:
    """Verify local-fork commits locally with 0 PRs and check-upstream-leak isolates HEAD."""
    paths = _init_synced_mono_repo(tmp_path)
    store = StateStore(paths)
    evolve = EvolveWorktreeService(
        paths,
        AlfredSettings.load(paths),
        store,
        tdd_gate_fn=lambda _p: None,
    )
    evolve.prepare_evolution_plan("fork_01", "Local fork tweak", "Custom core", mode="local-fork")
    wt = evolve.create_isolated_worktree("fork_01")
    (wt / "core" / "alfred" / "services" / "local_only.py").write_text(
        '"""Local fork service."""\n', encoding="utf-8"
    )
    record = evolve.execute_local_fork("fork_01", "feat(local): custom", wt)
    assert record.status == "LOCAL_FORK_COMMITTED"
    assert record.mode == "local-fork"
    assert record.pull_requests == []
    evolve.cleanup_worktree("fork_01")

    personal_email = "contrib@" + "g" + "mail.com"
    _git(paths.root_dir, "checkout", "-b", "evolve/local-fork-author")
    _git(paths.root_dir, "config", "user.email", personal_email)
    (paths.root_dir / "LOCAL_NOTE.md").write_text("Custom local note\n", encoding="utf-8")
    _git(paths.root_dir, "add", "LOCAL_NOTE.md")
    _git(paths.root_dir, "commit", "-m", "feat(local): personal branch")

    _git(paths.root_dir, "checkout", "main")
    _git(paths.root_dir, "checkout", "-b", "evolve/clean-pr")
    leak_script = WORKTREE_ROOT / "core" / "scripts" / "check-upstream-leak.sh"
    res = subprocess.run(
        ["bash", str(leak_script), str(paths.root_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr + res.stdout


def _setup_leak_terms_root(tmp_path: Path, terms: list[str] | None = None) -> Path:
    """Create an isolated ALFRED_ROOT_DIR containing private leak terms."""
    alfred_root = tmp_path / "alfred_root"
    secrets_dir = alfred_root / "instance" / "secrets"
    secrets_dir.mkdir(parents=True)
    lines = terms if terms is not None else ["ownerhandle"]
    (secrets_dir / "leak_terms.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return alfred_root


def _commit_as(
    repo: Path,
    filename: str,
    content: str,
    author_name: str,
    author_email: str,
    message: str,
) -> None:
    """Write a file, stage it, and create a commit with explicit author metadata."""
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(
        repo,
        "-c",
        f"user.name={author_name}",
        "-c",
        f"user.email={author_email}",
        "commit",
        "--author",
        f"{author_name} <{author_email}>",
        "-m",
        message,
    )


def _run_leak_guard(repo: Path, alfred_root: Path) -> subprocess.CompletedProcess[str]:
    """Execute check-upstream-leak.sh against repo using an isolated ALFRED_ROOT_DIR."""
    leak_script = WORKTREE_ROOT / "core" / "scripts" / "check-upstream-leak.sh"
    env = {**os.environ, "ALFRED_ROOT_DIR": str(alfred_root)}
    return subprocess.run(
        ["bash", str(leak_script), str(repo)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _init_upstream_tracked_repo(
    base_dir: Path,
    author_name: str,
    author_email: str,
) -> Path:
    """Initialize a bare origin remote, clone it, and push an initial main commit."""
    origin_bare = base_dir / "origin.git"
    _git(base_dir, "init", "--bare", "-b", "main", str(origin_bare))
    repo = base_dir / "repo"
    _git(base_dir, "clone", str(origin_bare), str(repo))
    _git(repo, "checkout", "-B", "main")
    _commit_as(repo, "README.md", "# Alfred\n", author_name, author_email, "chore: seed main")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def test_leak_guard_scopes_private_author_terms_to_unmerged_commits(tmp_path: Path) -> None:
    """Verify leak_terms author checks ignore origin/main ancestors and scan unmerged HEAD."""
    alfred_root = _setup_leak_terms_root(tmp_path)
    repo = _init_upstream_tracked_repo(
        tmp_path,
        "Upstream Owner",
        "12345+ownerhandle@users.noreply.github.com",
    )
    _git(repo, "checkout", "-b", "evolve/clean-feature")
    _commit_as(
        repo,
        "clean_feature.py",
        '"""Clean feature."""\n',
        "Alfred Core Bot",
        "bot@alfred.local",
        "feat: add clean feature",
    )

    clean_res = _run_leak_guard(repo, alfred_root)
    assert clean_res.returncode == 0, clean_res.stderr + clean_res.stdout

    _commit_as(
        repo,
        "author_check.py",
        '"""Author check."""\n',
        "Owner Handle",
        "ownerhandle@example.com",
        "feat: commit with private author handle",
    )
    bad_author_res = _run_leak_guard(repo, alfred_root)
    assert bad_author_res.returncode != 0
    assert "Personal Identity Found in Git Commit Metadata" in bad_author_res.stderr
    assert "ownerhandle" in bad_author_res.stderr

    _git(repo, "reset", "--hard", "HEAD~1")
    _commit_as(
        repo,
        "leaked_note.md",
        "Private note mentioning ownerhandle in file body.\n",
        "Alfred Core Bot",
        "bot@alfred.local",
        "docs: add note with private term",
    )
    bad_file_res = _run_leak_guard(repo, alfred_root)
    assert bad_file_res.returncode != 0
    assert "Sensitive Data in File" in bad_file_res.stderr


def test_leak_guard_keeps_full_author_scan_without_upstream_remote(tmp_path: Path) -> None:
    """Verify full HEAD author scan holds without upstream remote and for generic webmail."""
    alfred_root = _setup_leak_terms_root(tmp_path)

    local_repo = tmp_path / "local_only"
    local_repo.mkdir()
    _git(local_repo, "init", "-b", "main")
    _commit_as(
        local_repo,
        "README.md",
        "# Local repo\n",
        "Owner Handle",
        "ownerhandle@example.com",
        "chore: local commit with private handle",
    )
    local_res = _run_leak_guard(local_repo, alfred_root)
    assert local_res.returncode != 0
    assert "Personal Identity Found in Git Commit Metadata" in local_res.stderr
    assert "ownerhandle" in local_res.stderr

    upstream_webmail = "contrib@" + "g" + "mail.com"
    remote_case_dir = tmp_path / "with_origin"
    remote_case_dir.mkdir()
    webmail_repo = _init_upstream_tracked_repo(
        remote_case_dir,
        "Generic Contributor",
        upstream_webmail,
    )
    _git(webmail_repo, "checkout", "-b", "evolve/clean-work")
    _commit_as(
        webmail_repo,
        "clean.py",
        '"""Clean."""\n',
        "Alfred Core Bot",
        "bot@alfred.local",
        "feat: clean commit",
    )
    webmail_res = _run_leak_guard(webmail_repo, alfred_root)
    assert webmail_res.returncode != 0
    assert "Personal Identity Found in Git Commit Metadata" in webmail_res.stderr
    assert upstream_webmail in webmail_res.stderr
