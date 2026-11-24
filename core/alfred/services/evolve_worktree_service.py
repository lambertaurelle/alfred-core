"""alfred-evolve isolated git worktree TDD runner, governance gate, and Mono-Repo PR builder."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from alfred.domain.evolve_models import EvolutionMode
from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    EvolutionProposalRecord,
    PullRequestArtifact,
)
from alfred.repositories.json_repository import atomic_write_text
from alfred.repositories.state_store import StateStore
from alfred.services.core_spec_service import CoreSpecService
from alfred.services.evolve_gate_runner import run_leak_and_ext_checks, run_tdd_and_leak_gates
from alfred.services.evolve_governance_service import (
    EvolveGovernanceService,
    _resolve_freshest_base_ref,
    _sanitized_git_env,
)
from alfred.services.gh_pr_publisher import publish_github_pr

SAFE_PROPOSAL_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _validate_proposal_id(proposal_id: str) -> str:
    """Ensure proposal_id is a safe slug without path traversal or git ref hazards."""
    if not SAFE_PROPOSAL_ID_RE.fullmatch(proposal_id):
        raise ValueError(
            f"Invalid proposal_id '{proposal_id}'; expected alphanumeric/underscore/hyphen slug."
        )
    return proposal_id


def _parse_porcelain_paths(status_output: str) -> list[str]:
    """Extract relative paths from 'git status --porcelain' preserving column offsets."""
    paths: list[str] = []
    for raw_line in status_output.splitlines():
        if len(raw_line) < 4:
            continue
        path_part = raw_line[3:].strip()
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        cleaned = path_part.strip('"')
        if cleaned:
            paths.append(cleaned)
    return paths


class EvolveWorktreeService:
    """Manage isolated git worktrees, V8 governance modes, TDD/Leak gates, and Mono-Repo PRs."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
        tdd_gate_fn: Callable[[Path], None] | None = None,
        pr_publisher_fn: Callable[[Path, str, str, str, str], str] | None = None,
    ) -> None:
        """Initialize the Mono-Repo evolution service.

        Args:
            paths: Resolved AlfredPaths layout.
            settings: Runtime structural settings.
            state_store: Local atomic state store.
            tdd_gate_fn: Optional override executing TDD & architecture checks in worktree.
            pr_publisher_fn: Optional callback opening remote GitHub PRs via gh/API.
        """
        self._paths = paths
        self._settings = settings
        self._state_store = state_store
        self._tdd_gate_fn = tdd_gate_fn
        self._pr_publisher_fn = pr_publisher_fn
        self._pr_dir = self._paths.state_dir / "pull_requests"
        self._pr_dir.mkdir(parents=True, exist_ok=True)

    def prepare_evolution_plan(
        self,
        proposal_id: str,
        title: str,
        description: str,
        mode: str = "upstream-pr",
        is_breaking: bool = False,
        is_hybrid: bool = False,
        core_complete: bool = False,
    ) -> tuple[EvolutionProposalRecord, str]:
        """Register an evolution proposal and render its Stage-2 Gate implementation_plan.md.

        Args:
            proposal_id: Short slug identifier for the evolution.
            title: Concise evolution title.
            description: Detailed functional & technical specification.
            mode: Target evolution mode ('instance', 'upstream-pr', or 'local-fork').
            is_breaking: Whether breaking core changes are anticipated.
            is_hybrid: Whether the evolution spans both core/ and private instance/.
            core_complete: Whether the core/ slice is standalone-complete without instance/.

        Returns:
            Tuple of (persisted EvolutionProposalRecord, implementation_plan.md content).
        """
        safe_id = _validate_proposal_id(proposal_id)
        norm_mode = EvolutionMode(mode).value
        if is_hybrid and norm_mode != EvolutionMode.INSTANCE.value:
            EvolveGovernanceService().validate_hybrid_completeness(is_hybrid, core_complete)
        record = EvolutionProposalRecord(
            proposal_id=safe_id,
            title=title,
            description=description,
            status="PENDING_GATE",
            mode=norm_mode,
            is_breaking=is_breaking,
        )
        self._save_proposal_record(safe_id, record)
        brk = "\n- **Breaking Core Change** : `[BREAKING]` requis." if is_breaking else ""
        plan_md = (
            f"# Plan d'Évolution `alfred-evolve` (`{safe_id}`)\n\n"
            f"- **Titre** : {title}\n- **Mode** : `{norm_mode}`{brk}\n"
            f"- **Description** : {description}\n\n"
            "## Qualification `evolve-qualifier`\n"
            "- **Par défaut (`instance/`)** : isoler le personnel sous `instance/` "
            "(`--mode instance`).\n"
            "- **En faire profiter la communauté `alfred-core`** *(optionnel)* : exemple anonyme "
            "sous `instance/examples/*.example.py` et PR `--mode upstream-pr`.\n"
            "- **Zéro demi-morceau dans `core/`** : si `--hybrid`, la brique `core/` doit être "
            "100 % autonome sans `instance/` (`--core-complete`), sinon `--mode instance` seul.\n\n"
            "## Contrats TDD & Bonnes Pratiques Obligatoires\n"
            "1. **3 Niveaux de Tests (`tdd-red` -> `tdd-green`)** : tests **unitaires**, "
            "tests **fonctionnels** inter-services et tests **E2E** (bout en bout).\n"
            "2. **Analyse & Tests de Non-Régression** : identification explicite des impacts "
            "et exécution complète de la suite anti-régression.\n"
            "3. **Adversarial Testing (`swarm`)** : campagne adversariale multi-agents `swarm` "
            "sur les entrées non fiables, traversées de chemin, injections et cas limites.\n\n"
            "## Pipeline Post-Validation (`Proceed`)\n"
            f"1. Création d'un `git worktree` isolé mono-repo sur `evolve/{safe_id}`.\n"
            "2. Synchronisation `docs/SPEC.md` (`sync-spec`) et validation TDD "
            "(`check_code_architecture.py`, `ruff`, `mypy`, `pytest >= 90%`).\n"
            "3. Vérification `check-upstream-leak.sh` (0 donnée personnelle).\n"
            "4. Pull Request unique sur **`alfred-core`**.\n"
        )
        return record, plan_md

    def create_isolated_worktree(self, proposal_id: str) -> Path:
        """Create an isolated git worktree for the unified Alfred mono-repo.

        Args:
            proposal_id: Evolution proposal slug.

        Returns:
            Path to the isolated git worktree checkout.
        """
        safe_id = _validate_proposal_id(proposal_id)
        branch = f"evolve/{safe_id}"
        wt_dir = self._paths.worktrees_dir / safe_id
        if wt_dir.exists():
            self.cleanup_worktree(safe_id)
        self._paths.worktrees_dir.mkdir(parents=True, exist_ok=True)
        git_env = _sanitized_git_env(self._paths.root_dir)
        start_ref = _resolve_freshest_base_ref(
            self._paths.root_dir, self._settings.core_base_branch, git_env
        )
        subprocess.run(
            ["git", "worktree", "add", "-B", branch, str(wt_dir), start_ref],
            cwd=self._paths.root_dir,
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        )
        self._state_store.evolution_ledger.update(
            lambda led: (
                led.model_copy(
                    update={
                        "proposals": {
                            **led.proposals,
                            safe_id: led.proposals[safe_id].model_copy(
                                update={"worktree_path": str(wt_dir), "status": "IN_WORKTREE"}
                            ),
                        }
                    }
                )
                if safe_id in led.proposals
                else led
            )
        )
        return wt_dir

    def execute_instance_only(
        self,
        proposal_id: str,
        title: str,
        description: str,
        repo_root: Path | None = None,
    ) -> EvolutionProposalRecord:
        """Apply a private instance/ evolution locally with 0 PR and strict path enforcement.

        Args:
            proposal_id: Evolution proposal slug.
            title: Concise evolution title.
            description: Summary of local instance/ changes.
            repo_root: Optional repository root override for git status checks.

        Returns:
            Persisted EvolutionProposalRecord marked APPLIED_INSTANCE.

        Raises:
            PermissionError: If any modified or untracked path touches core/, docs/, or manifests.
        """
        safe_id = _validate_proposal_id(proposal_id)
        root = (repo_root or self._paths.root_dir).resolve()
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            env=_sanitized_git_env(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if status_res.returncode == 0 and status_res.stdout.strip():
            changed = _parse_porcelain_paths(status_res.stdout)
            EvolveGovernanceService().verify_instance_only_paths(root, changed)

        record = EvolutionProposalRecord(
            proposal_id=safe_id,
            title=title,
            description=description,
            status="APPLIED_INSTANCE",
            mode=EvolutionMode.INSTANCE.value,
            pull_requests=[],
        )
        self._save_proposal_record(safe_id, record)
        return record

    def execute_local_fork(
        self,
        proposal_id: str,
        title: str,
        worktree: Path,
        body: str = "",
    ) -> EvolutionProposalRecord:
        """Commit custom core changes locally on evolve/<id> without opening any upstream PR.

        Args:
            proposal_id: Evolution proposal slug.
            title: Git commit message for the local fork branch.
            worktree: Isolated mono-repo worktree path.
            body: Optional proposal description stored in the ledger.

        Returns:
            Persisted EvolutionProposalRecord marked LOCAL_FORK_COMMITTED.
        """
        safe_id = _validate_proposal_id(proposal_id)
        self._sync_spec_if_present(worktree)
        self._run_tdd_and_leak_gates(worktree)
        branch = f"evolve/{safe_id}"
        self._stage_and_commit_worktree(worktree, branch, self._settings.core_base_branch, title)
        record = EvolutionProposalRecord(
            proposal_id=safe_id,
            title=title,
            description=body,
            status="LOCAL_FORK_COMMITTED",
            mode=EvolutionMode.LOCAL_FORK.value,
            worktree_path=str(worktree),
            pull_requests=[],
        )
        self._save_proposal_record(safe_id, record)
        return record

    def execute_and_open_pr(
        self,
        proposal_id: str,
        title: str,
        body: str,
        worktree: Path,
        *,
        is_hybrid: bool = False,
        core_complete: bool = True,
        is_breaking: bool = False,
        draft: bool = False,
    ) -> PullRequestArtifact:
        """Sync SPEC.md, run TDD + Zero-Leak gates, commit, and open the Mono-Repo PR.

        Args:
            proposal_id: Evolution proposal slug.
            title: Commit and Pull Request title.
            body: Pull Request summary body.
            worktree: Isolated mono-repo worktree path.
            is_hybrid: True when combining core/ changes with private instance/ pieces.
            core_complete: True when the core/ feature is standalone-complete.
            is_breaking: Explicit --breaking flag augmenting automated breaking detection.
            draft: When True, open remote GitHub PR as a Draft PR.

        Returns:
            Generated PullRequestArtifact descriptor.

        Raises:
            ValueError: If is_hybrid is True while core_complete is False.
            RuntimeError: If TDD gates, isolation checks, or leak checks fail.
        """
        safe_id = _validate_proposal_id(proposal_id)
        gov = EvolveGovernanceService()
        gov.validate_hybrid_completeness(
            is_hybrid=is_hybrid, core_is_standalone_complete=core_complete
        )
        breaking_report = gov.detect_breaking_core_changes(
            worktree, base_branch=self._settings.core_base_branch
        )
        effective_breaking = is_breaking or breaking_report.is_breaking
        self._sync_spec_if_present(worktree)
        self._run_tdd_and_leak_gates(worktree)
        pr_title, pr_body = gov.format_pr_metadata(
            title, body, breaking_report, force_breaking=effective_breaking
        )
        artifact = self._commit_and_open_pr(
            repo_name="alfred-core",
            worktree=worktree,
            branch=f"evolve/{safe_id}",
            base_branch=self._settings.core_base_branch,
            proposal_id=safe_id,
            title=pr_title,
            body=pr_body,
            draft=draft,
        )
        self._record_pr_opened(safe_id, pr_title, body, worktree, effective_breaking, artifact)
        return artifact

    def cleanup_worktree(self, proposal_id: str) -> None:
        """Detach an isolated git worktree checkout while preserving its committed branch.

        Args:
            proposal_id: Evolution proposal slug whose worktree should be detached.
        """
        safe_id = _validate_proposal_id(proposal_id)
        wt_dir = self._paths.worktrees_dir / safe_id
        if wt_dir.exists() and (self._paths.root_dir / ".git").exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_dir)],
                cwd=self._paths.root_dir,
                env=_sanitized_git_env(self._paths.root_dir),
                check=False,
                capture_output=True,
            )
        if wt_dir.exists():
            shutil.rmtree(wt_dir, ignore_errors=True)

    def _save_proposal_record(self, safe_id: str, record: EvolutionProposalRecord) -> None:
        """Persist or overwrite an EvolutionProposalRecord in the atomic ledger."""
        self._state_store.evolution_ledger.update(
            lambda led: led.model_copy(update={"proposals": {**led.proposals, safe_id: record}})
        )

    def _record_pr_opened(
        self,
        safe_id: str,
        pr_title: str,
        body: str,
        worktree: Path,
        effective_breaking: bool,
        artifact: PullRequestArtifact,
    ) -> None:
        """Update or create the ledger entry after PR generation."""
        self._state_store.evolution_ledger.update(
            lambda led: led.model_copy(
                update={
                    "proposals": {
                        **led.proposals,
                        safe_id: (
                            led.proposals[safe_id].model_copy(
                                update={
                                    "status": "PR_OPENED",
                                    "mode": EvolutionMode.UPSTREAM_PR.value,
                                    "is_breaking": effective_breaking,
                                    "pull_requests": [artifact],
                                }
                            )
                            if safe_id in led.proposals
                            else EvolutionProposalRecord(
                                proposal_id=safe_id,
                                title=pr_title,
                                description=body,
                                status="PR_OPENED",
                                mode=EvolutionMode.UPSTREAM_PR.value,
                                is_breaking=effective_breaking,
                                worktree_path=str(worktree),
                                pull_requests=[artifact],
                            )
                        ),
                    }
                }
            )
        )

    def _sync_spec_if_present(self, worktree: Path) -> None:
        """Regenerate docs/SPEC.md when the worktree represents a full core/ or SPEC checkout."""
        if (worktree / "core" / "alfred" / "main.py").is_file() or (
            worktree / "docs" / "SPEC.md"
        ).is_file():
            CoreSpecService(self._paths).sync_spec_file(worktree)

    def _resolve_python_bin(self) -> Path:
        """Locate the active .venv Python binary or fallback to sys.executable."""
        venv_py = self._paths.root_dir / ".venv" / "bin" / "python"
        return venv_py if venv_py.exists() else Path(sys.executable)

    def _run_leak_and_ext_checks(self, worktree: Path) -> None:
        """Enforce leak guard, AST limits, and pure-Python SPEC & isolation checks."""
        run_leak_and_ext_checks(worktree, self._paths, self._resolve_python_bin())

    def _run_tdd_and_leak_gates(self, worktree: Path) -> None:
        """Run leak checks and the full 5-gate TDD suite on the mono-repo worktree."""
        run_tdd_and_leak_gates(worktree, self._paths, self._resolve_python_bin(), self._tdd_gate_fn)

    def _stage_and_commit_worktree(
        self, worktree: Path, branch: str, base_branch: str, title: str
    ) -> list[str]:
        """Stage respected files, create commit when dirty, and return changed file list."""
        git_env = _sanitized_git_env(self._paths.root_dir)
        subprocess.run(
            ["git", "add", "-A"], cwd=worktree, env=git_env, check=True, capture_output=True
        )
        status_out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree, env=git_env, check=True, capture_output=True, text=True,
        ).stdout.strip()
        if status_out:
            subprocess.run(
                ["git", "commit", "-m", title],
                cwd=worktree, env=git_env, check=True, capture_output=True, text=True,
            )
        diff_base = _resolve_freshest_base_ref(worktree, base_branch, git_env, fetch=False)
        diff_out = subprocess.run(
            ["git", "diff", "--name-only", f"{diff_base}..{branch}"],
            cwd=worktree, env=git_env, check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        return [line.strip() for line in diff_out if line.strip()]

    def _try_open_remote_gh_pr(
        self,
        worktree: Path,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> str | None:
        """Push branch and open a GitHub PR via gh CLI (with fork fallback)."""
        return publish_github_pr(
            worktree, branch, base_branch, title, body,
            git_env=_sanitized_git_env(self._paths.root_dir), draft=draft,
        )

    def _commit_and_open_pr(
        self, repo_name: str, worktree: Path, branch: str, base_branch: str,
        proposal_id: str, title: str, body: str, draft: bool = False,
    ) -> PullRequestArtifact:
        """Commit worktree changes and publish or materialize the Pull Request artifact."""
        changed = self._stage_and_commit_worktree(worktree, branch, base_branch, title)
        if self._pr_publisher_fn is not None:
            pr_ref = self._pr_publisher_fn(worktree, branch, base_branch, title, body)
            status = "OPEN_REMOTE"
        else:
            remote_url = (
                self._try_open_remote_gh_pr(worktree, branch, base_branch, title, body, draft=draft)
                if changed
                else None
            )
            if remote_url:
                pr_ref, status = remote_url, "OPEN_REMOTE"
            else:
                pr_file = self._pr_dir / f"{proposal_id}_{repo_name}.md"
                files_md = "\n".join(f"- `{f}`" for f in changed) or "- `(No file delta)`"
                pr_md = (
                    f"# Pull Request — `{repo_name}` (`{branch}` -> `{base_branch}`)\n\n"
                    f"## {title}\n\n{body}\n\n### Changed Files\n{files_md}\n"
                )
                atomic_write_text(pr_file, pr_md)
                pr_ref, status = str(pr_file), "READY_FOR_REVIEW"
        return PullRequestArtifact(
            repo_name=repo_name, branch_name=branch, base_branch=base_branch,
            pr_url_or_ref=pr_ref, status=status, changed_files=changed,
        )
