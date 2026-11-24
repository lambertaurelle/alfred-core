"""Governance, qualification, isolation, and breaking-change detection for alfred-evolve."""

from __future__ import annotations

import ast
import contextlib
import os
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path

from alfred.domain.evolve_models import (
    BreakingChangeReport,
    EvolutionArchitectureKind,
    EvolutionMode,
    UseCaseQualification,
)
from alfred.services.core_spec_service import (
    CoreSpecService,
    extract_cli_commands_from_source,
    extract_features_from_source,
)

_ALLOWED_INSTANCE_RE = re.compile(
    r"^instance/(examples/.+|"
    r"config/[^/]+\.example\.(ya?ml|md|json)|"
    r"config/prompts/[^/]+\.example\.md|"
    r"extensions/(__init__\.py|README\.md|[^/]+\.example\.py))$"
)
_BREAKING_BANNER = "## ⚠️ BREAKING CORE CHANGE — Arbitrage Mainteneur Requis"


def _sanitized_git_env(root: Path | None = None) -> dict[str, str]:
    """Return process environment stripped of inherited GIT_* hook overrides."""
    blocked = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    }
    env = {k: v for k, v in os.environ.items() if k not in blocked}
    if root is not None:
        env["ALFRED_ROOT_DIR"] = str(root)
    return env


def _git_show_text(worktree: Path, spec: str) -> str | None:
    """Return UTF-8 text from 'git show <spec>' or None if missing."""
    res = subprocess.run(
        ["git", "show", spec],
        cwd=worktree,
        env=_sanitized_git_env(worktree),
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout if res.returncode == 0 else None


def _resolve_freshest_base_ref(
    repo_root: Path,
    base_branch: str,
    git_env: dict[str, str],
    *,
    fetch: bool = True,
) -> str:
    """Resolve origin/<base_branch> after optional quiet fetch when ahead of local base_branch."""
    env = {**git_env, "GIT_TERMINAL_PROMPT": "0"}
    if fetch:
        has_origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_root, env=env, capture_output=True, text=True, check=False,
        )
        if has_origin.returncode == 0:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    ["git", "fetch", "--quiet", "origin", base_branch],
                    cwd=repo_root, env=env, capture_output=True,
                    text=True, check=False, timeout=15,
                )
    remote_ref = f"origin/{base_branch}"
    if (
        subprocess.run(
            ["git", "rev-parse", "--verify", remote_ref],
            cwd=repo_root, env=env, capture_output=True, text=True, check=False,
        ).returncode
        != 0
    ):
        return base_branch
    local_exists = (
        subprocess.run(
            ["git", "rev-parse", "--verify", base_branch],
            cwd=repo_root, env=env, capture_output=True, text=True, check=False,
        ).returncode
        == 0
    )
    if not local_exists:
        return remote_ref
    is_anc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_branch, remote_ref],
        cwd=repo_root, env=env, capture_output=True, text=True, check=False,
    )
    return remote_ref if is_anc.returncode == 0 else base_branch


def _has_instance_import_ast(py_file: Path) -> list[str]:
    """Check a single Python file via AST for direct or deferred instance imports."""
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    except (OSError, SyntaxError):
        return []
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "instance" or alias.name.startswith("instance."):
                    hits.append(f"{py_file}:{node.lineno} imports '{alias.name}'")
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and (node.module == "instance" or node.module.startswith("instance."))
        ):
            hits.append(f"{py_file}:{node.lineno} imports from '{node.module}'")
    return hits


class EvolveGovernanceService:
    """Enforce V8 mono-repo boundaries, mode qualification, and breaking-change gates."""

    def qualify_use_case(
        self,
        touches_core: bool,
        reusable_for_community: bool = False,
        is_hybrid: bool = False,
        core_is_standalone_complete: bool = True,
    ) -> UseCaseQualification:
        """Qualify an evolution request into one of the 4 V8 architectural scenarios.

        Args:
            touches_core: True when files outside private instance/ must be changed.
            reusable_for_community: True when a local extension could also help upstream users.
            is_hybrid: True when combining core/ changes with instance/ extensions or config.
            core_is_standalone_complete: True when core/ works autonomously without instance/.

        Returns:
            UseCaseQualification verdict guiding evolution mode and re-architecture.
        """
        if not touches_core:
            if reusable_for_community:
                return UseCaseQualification(
                    architecture_kind=EvolutionArchitectureKind.INSTANCE_WITH_COMMUNITY_OPTION,
                    recommended_mode=EvolutionMode.INSTANCE,
                    rationale="Extension privée exécutable immédiatement sous instance/.",
                    community_pr_option_summary=(
                        "Option communautaire : anonymiser un exemple sous "
                        "instance/examples/*.example.py et soumettre une PR upstream-pr."
                    ),
                )
            return UseCaseQualification(
                architecture_kind=EvolutionArchitectureKind.PURE_INSTANCE,
                recommended_mode=EvolutionMode.INSTANCE,
                rationale="Besoin strictement personnel relevant de instance/ uniquement.",
            )

        if is_hybrid:
            if not core_is_standalone_complete:
                return UseCaseQualification(
                    architecture_kind=EvolutionArchitectureKind.HYBRID_CORE_AND_INSTANCE,
                    recommended_mode=EvolutionMode.INSTANCE,
                    core_feature_is_standalone_complete=False,
                    rationale="La partie core/ dépend encore d'éléments privés instance/.",
                    rearchitecture_suggestion=(
                        "Rendre la capacité dans core/ 100 % autonome avec valeurs par "
                        "défaut et protocole d'extension avant d'ouvrir une PR upstream-pr."
                    ),
                )
            return UseCaseQualification(
                architecture_kind=EvolutionArchitectureKind.HYBRID_CORE_AND_INSTANCE,
                recommended_mode=EvolutionMode.UPSTREAM_PR,
                core_feature_is_standalone_complete=True,
                rationale="La brique core/ est autonome et complète sans dépendance instance/.",
            )

        return UseCaseQualification(
            architecture_kind=EvolutionArchitectureKind.CORE_EVOLUTION,
            recommended_mode=EvolutionMode.UPSTREAM_PR,
            core_feature_is_standalone_complete=core_is_standalone_complete,
            rationale="Évolution générique de core/ éligible à une PR mono-repo.",
        )

    def validate_hybrid_completeness(
        self,
        is_hybrid: bool,
        core_is_standalone_complete: bool,
    ) -> None:
        """Reject hybrid upstream evolutions whose core/ feature is incomplete without instance/.

        Args:
            is_hybrid: True when the proposal combines core/ and instance/ changes.
            core_is_standalone_complete: True when core/ is standalone and self-contained.

        Raises:
            ValueError: If is_hybrid is True and core_is_standalone_complete is False.
        """
        if is_hybrid and not core_is_standalone_complete:
            raise ValueError(
                "Hybrid evolution rejected: the core/ feature must be 100% standalone-complete "
                "without depending on private instance/ files (pass --core-complete only after "
                "decoupling, or use --mode instance / --mode local-fork)."
            )

    def verify_instance_only_paths(
        self,
        repo_root: Path,
        changed_paths: Sequence[str | Path],
    ) -> None:
        """Ensure every changed path in instance mode resides strictly under instance/.

        Args:
            repo_root: Target repository root directory.
            changed_paths: Relative or absolute file paths modified by the operation.

        Raises:
            PermissionError: If any path escapes instance/ or targets core/, docs/, or manifests.
        """
        root = repo_root.resolve()
        inst_root = (root / "instance").resolve()
        for raw_path in changed_paths:
            rel_str = str(raw_path).strip()
            if not rel_str:
                continue
            candidate = Path(rel_str)
            resolved = (
                candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
            )
            if not resolved.is_relative_to(inst_root):
                raise PermissionError(
                    f"Forbidden path in --mode instance (must stay inside instance/): {raw_path}"
                )

    def verify_standalone_core_isolation(self, worktree: Path) -> list[str]:
        """Verify in pure Python that a worktree contains no private instance/ files or imports.

        Args:
            worktree: Isolated mono-repo git worktree path.

        Returns:
            List of isolation violation strings (empty when clean).
        """
        root = worktree.resolve()
        errors: list[str] = []
        inst_dir = root / "instance"
        if inst_dir.is_dir():
            for item in sorted(inst_dir.rglob("*")):
                if not item.is_file() or "__pycache__" in item.parts or item.suffix == ".pyc":
                    continue
                rel = item.relative_to(root).as_posix()
                if not _ALLOWED_INSTANCE_RE.fullmatch(rel):
                    errors.append(
                        f"Private instance file not allowed in upstream core worktree: {rel}"
                    )

        for sub in (root / "core" / "alfred", root / "core" / "scripts"):
            if not sub.is_dir():
                continue
            for py_file in sorted(sub.rglob("*.py")):
                if "__pycache__" in py_file.parts:
                    continue
                for hit in _has_instance_import_ast(py_file):
                    errors.append(f"Instance coupling forbidden under core/: {hit}")
        return errors

    def detect_breaking_core_changes(
        self,
        worktree: Path,
        base_branch: str = "main",
    ) -> BreakingChangeReport:
        """Compare worktree AST inventory against base_branch for removed public surfaces.

        Args:
            worktree: Isolated mono-repo git worktree path.
            base_branch: Git base branch to compare against (default 'main').

        Returns:
            BreakingChangeReport detailing removed modules, CLI commands, and feature flags.
        """
        root = worktree.resolve()
        git_env = _sanitized_git_env(root)
        base_ref = _resolve_freshest_base_ref(root, base_branch, git_env, fetch=False)
        ls_res = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", base_ref, "--", "core/alfred"],
            cwd=root,
            env=git_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if ls_res.returncode != 0:
            return BreakingChangeReport(is_breaking=False)

        base_files = [ln.strip() for ln in ls_res.stdout.splitlines() if ln.strip().endswith(".py")]
        current = CoreSpecService().inspect_core_snapshot(root)
        cur_modules = {m.module_path for m in current.modules}
        cur_cmds = {c.name for c in current.cli_commands}
        cur_flags = {f.flag_name for f in current.features}

        removed_modules = sorted(
            path
            for path in base_files
            if not path.endswith("/__init__.py") and path not in cur_modules
        )
        removed_cmds = self._diff_base_cli_commands(root, base_ref, base_files, cur_cmds)
        removed_flags = self._diff_base_feature_flags(root, base_ref, base_files, cur_flags)
        is_breaking = bool(removed_modules or removed_cmds or removed_flags)
        summary = self._render_breaking_summary(removed_modules, removed_cmds, removed_flags)
        return BreakingChangeReport(
            is_breaking=is_breaking,
            removed_modules=removed_modules,
            removed_cli_commands=removed_cmds,
            removed_feature_flags=removed_flags,
            summary_markdown=summary,
        )

    def format_pr_metadata(
        self,
        title: str,
        body: str,
        report: BreakingChangeReport,
        force_breaking: bool = False,
    ) -> tuple[str, str]:
        """Format PR title and body with [BREAKING] warning banner when breaking changes apply.

        Args:
            title: Base commit / Pull Request title.
            body: Base Pull Request description body.
            report: Automated BreakingChangeReport from detect_breaking_core_changes().
            force_breaking: Optional explicit --breaking CLI override.

        Returns:
            Tuple of (formatted_title, formatted_body).
        """
        if not (report.is_breaking or force_breaking):
            return title, body
        clean_title = title.strip()
        out_title = (
            clean_title if clean_title.startswith("[BREAKING]") else f"[BREAKING] {clean_title}"
        )
        parts = [_BREAKING_BANNER]
        if report.summary_markdown.strip():
            parts.append(report.summary_markdown.strip())
        if body.strip():
            parts.append(body.strip())
        return out_title, "\n\n".join(parts)

    def _diff_base_cli_commands(
        self,
        root: Path,
        base_branch: str,
        base_files: list[str],
        cur_cmds: set[str],
    ) -> list[str]:
        """Extract CLI commands present on base_branch controllers but missing in worktree."""
        base_cmds: set[str] = set()
        for rel in base_files:
            if not rel.startswith("core/alfred/controllers/") or rel.endswith("/__init__.py"):
                continue
            src = _git_show_text(root, f"{base_branch}:{rel}")
            if src:
                base_cmds.update(c.name for c in extract_cli_commands_from_source(src, rel))
        return sorted(cmd for cmd in base_cmds if cmd not in cur_cmds)

    def _diff_base_feature_flags(
        self,
        root: Path,
        base_branch: str,
        base_files: list[str],
        cur_flags: set[str],
    ) -> list[str]:
        """Extract feature flags present on base_branch but missing in worktree."""
        feat_rel = "core/alfred/domain/feature_models.py"
        if feat_rel not in base_files:
            return []
        src = _git_show_text(root, f"{base_branch}:{feat_rel}")
        if not src:
            return []
        base_flags = {f.flag_name for f in extract_features_from_source(src)}
        return sorted(flag for flag in base_flags if flag not in cur_flags)

    def _render_breaking_summary(
        self, removed_modules: list[str], removed_cmds: list[str], removed_flags: list[str]
    ) -> str:
        """Build Markdown bullet summary for detected breaking removals."""
        lines = [*(f"- Removed core module `{m}`" for m in removed_modules)]
        lines.extend(f"- Removed CLI command `{c}`" for c in removed_cmds)
        lines.extend(f"- Removed feature flag `{f}`" for f in removed_flags)
        return "\n".join(lines)
