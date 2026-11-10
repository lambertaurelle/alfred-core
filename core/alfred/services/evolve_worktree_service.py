"""alfred-evolve isolated git worktree TDD runner and Dual-Repo Pull Request generator."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    EvolutionProposalRecord,
    PullRequestArtifact,
)
from alfred.repositories.json_repository import atomic_write_text
from alfred.repositories.state_store import StateStore

SAFE_PROPOSAL_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def _sanitized_git_env(alfred_root: Path | None = None) -> dict[str, str]:
    """Return environment dict stripped of inherited GIT_* hook variables."""
    blocked = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
    }
    env = {k: v for k, v in os.environ.items() if k not in blocked}
    if alfred_root is not None:
        env["ALFRED_ROOT_DIR"] = str(alfred_root)
    return env


def _validate_proposal_id(proposal_id: str) -> str:
    """Ensure proposal_id is a safe slug without path traversal or git ref hazards."""
    if not SAFE_PROPOSAL_ID_RE.fullmatch(proposal_id):
        raise ValueError(
            f"Invalid proposal_id '{proposal_id}'; expected alphanumeric/underscore/hyphen slug."
        )
    return proposal_id


class EvolveWorktreeService:
    """Manage isolated git worktrees, TDD gates, and Dual-Repo PRs (core + instance)."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
        tdd_gate_fn: Callable[[Path], None] | None = None,
        pr_publisher_fn: Callable[[Path, str, str, str, str], str] | None = None,
    ) -> None:
        """Initialize the Dual-Repo evolution service.

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
    ) -> tuple[EvolutionProposalRecord, str]:
        """Register an evolution proposal and render its Stage-2 Gate implementation_plan.md.

        Args:
            proposal_id: Short slug identifier for the evolution.
            title: Concise evolution title.
            description: Detailed functional & technical specification.

        Returns:
            Tuple of (persisted EvolutionProposalRecord, implementation_plan.md content).
        """
        safe_id = _validate_proposal_id(proposal_id)
        record = EvolutionProposalRecord(
            proposal_id=safe_id,
            title=title,
            description=description,
            status="PENDING_GATE",
        )
        self._state_store.evolution_ledger.update(
            lambda led: led.model_copy(update={"proposals": {**led.proposals, safe_id: record}})
        )
        plan_md = (
            f"# Plan d'Évolution `alfred-evolve` (`{safe_id}`)\n\n"
            f"- **Titre** : {title}\n"
            f"- **Description** : {description}\n\n"
            f"## Pipeline Post-Validation (`Proceed`)\n"
            f"1. Création de `git worktree` isolés sur `evolve/{safe_id}`.\n"
            f"2. Validation TDD (`check_code_architecture.py`, `ruff`, `mypy`, `pytest >= 90%`).\n"
            f"3. Vérification `check-upstream-leak.sh` (0 donnée personnelle sur les 2 repos).\n"
            f"4. Pull Requests automatiques sur **`alfred-core`** et **`alfred-instance`**.\n"
        )
        return record, plan_md

    def create_isolated_worktrees(self, proposal_id: str) -> tuple[Path, Path]:
        """Create isolated git worktrees for both alfred-core and alfred-instance.

        Args:
            proposal_id: Evolution proposal slug.

        Returns:
            Tuple of (core_worktree_path, instance_worktree_path).
        """
        safe_id = _validate_proposal_id(proposal_id)
        branch = f"evolve/{safe_id}"
        base_dir = self._paths.worktrees_dir / safe_id
        core_wt = base_dir / "core"
        inst_wt = base_dir / "instance"
        if base_dir.exists():
            self.cleanup_worktrees(safe_id)

        base_dir.mkdir(parents=True, exist_ok=True)
        self._git_worktree_add(
            self._paths.root_dir, core_wt, branch, self._settings.core_base_branch
        )
        self._git_worktree_add(
            self._paths.instance_dir, inst_wt, branch, self._settings.instance_base_branch
        )
        self._state_store.evolution_ledger.update(
            lambda led: (
                led.model_copy(
                    update={
                        "proposals": {
                            **led.proposals,
                            safe_id: led.proposals[safe_id].model_copy(
                                update={"worktree_path": str(base_dir), "status": "IN_WORKTREE"}
                            ),
                        }
                    }
                )
                if safe_id in led.proposals
                else led
            )
        )
        return core_wt, inst_wt

    def execute_and_open_dual_prs(
        self,
        proposal_id: str,
        title: str,
        body: str,
        core_worktree: Path,
        instance_worktree: Path,
    ) -> list[PullRequestArtifact]:
        """Run TDD + Zero-Leak gates, commit worktrees, and open PRs on both Git repos.

        Args:
            proposal_id: Evolution proposal slug.
            title: Commit and Pull Request title.
            body: Pull Request summary body.
            core_worktree: Isolated worktree path for alfred-core.
            instance_worktree: Isolated worktree path for alfred-instance.

        Returns:
            List of two PullRequestArtifact descriptors (alfred-core and alfred-instance).

        Raises:
            RuntimeError: If TDD gates or check-upstream-leak.sh detect any violation.
        """
        safe_id = _validate_proposal_id(proposal_id)
        self._run_tdd_and_leak_gates(core_worktree, instance_worktree)
        branch = f"evolve/{safe_id}"
        core_pr = self._commit_and_open_pr(
            repo_name="alfred-core",
            worktree=core_worktree,
            branch=branch,
            base_branch=self._settings.core_base_branch,
            proposal_id=safe_id,
            title=title,
            body=body,
        )
        inst_pr = self._commit_and_open_pr(
            repo_name="alfred-instance",
            worktree=instance_worktree,
            branch=branch,
            base_branch=self._settings.instance_base_branch,
            proposal_id=safe_id,
            title=title,
            body=body,
        )
        artifacts = [core_pr, inst_pr]
        self._state_store.evolution_ledger.update(
            lambda led: (
                led.model_copy(
                    update={
                        "proposals": {
                            **led.proposals,
                            safe_id: led.proposals[safe_id].model_copy(
                                update={"status": "PR_OPENED", "pull_requests": artifacts}
                            ),
                        }
                    }
                )
                if safe_id in led.proposals
                else led
            )
        )
        return artifacts

    def cleanup_worktrees(self, proposal_id: str) -> None:
        """Detach isolated git worktree checkouts while preserving committed feature branches.

        Args:
            proposal_id: Evolution proposal slug whose worktrees should be detached.
        """
        safe_id = _validate_proposal_id(proposal_id)
        base_dir = self._paths.worktrees_dir / safe_id
        for repo_dir, wt_dir in (
            (self._paths.root_dir, base_dir / "core"),
            (self._paths.instance_dir, base_dir / "instance"),
        ):
            if wt_dir.exists() and (repo_dir / ".git").exists():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_dir)],
                    cwd=repo_dir,
                    env=_sanitized_git_env(self._paths.root_dir),
                    check=False,
                    capture_output=True,
                )
        if base_dir.exists():
            shutil.rmtree(base_dir, ignore_errors=True)

    def _git_worktree_add(
        self,
        repo_dir: Path,
        wt_dir: Path,
        branch: str,
        base_branch: str,
    ) -> None:
        """Create or reset a git worktree checkout on branch starting at base_branch."""
        subprocess.run(
            ["git", "worktree", "add", "-B", branch, str(wt_dir), base_branch],
            cwd=repo_dir,
            env=_sanitized_git_env(self._paths.root_dir),
            check=True,
            capture_output=True,
            text=True,
        )

    def _resolve_python_bin(self) -> Path:
        """Locate the active .venv Python binary or fallback to sys.executable."""
        import sys

        venv_py = self._paths.root_dir / ".venv" / "bin" / "python"
        return venv_py if venv_py.exists() else Path(sys.executable)

    def _run_leak_and_ext_checks(self, core_worktree: Path, instance_worktree: Path) -> None:
        """Enforce leak guard on both worktrees and AST check on instance/extensions."""
        leak_script = core_worktree / "core" / "scripts" / "check-upstream-leak.sh"
        git_env = _sanitized_git_env(self._paths.root_dir)
        if leak_script.exists():
            targets = (("alfred-core", core_worktree), ("alfred-instance", instance_worktree))
            for label, wt in targets:
                res = subprocess.run(
                    ["bash", str(leak_script), str(wt)],
                    env=git_env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode != 0:
                    raise RuntimeError(
                        f"Zero Upstream Leak check failed on {label}:\n{res.stderr}\n{res.stdout}"
                    )

        arch_script = core_worktree / "core" / "scripts" / "check_code_architecture.py"
        ext_dir = instance_worktree / "extensions"
        if arch_script.exists() and ext_dir.exists():
            ext_res = subprocess.run(
                [str(self._resolve_python_bin()), str(arch_script), "--only-dir", str(ext_dir)],
                cwd=core_worktree,
                env=git_env,
                capture_output=True,
                text=True,
                check=False,
            )
            if ext_res.returncode != 0:
                raise RuntimeError(f"Instance extensions architecture failed:\n{ext_res.stderr}")

    def _run_tdd_and_leak_gates(self, core_worktree: Path, instance_worktree: Path) -> None:
        """Run leak checks on both repos and full 5-gate TDD suite on core + extensions."""
        self._run_leak_and_ext_checks(core_worktree, instance_worktree)
        if self._tdd_gate_fn is not None:
            self._tdd_gate_fn(core_worktree)
            return

        venv_bin = self._paths.root_dir / ".venv" / "bin"
        py_bin = self._resolve_python_bin()
        arch_script = core_worktree / "core" / "scripts" / "check_code_architecture.py"
        if arch_script.exists():
            arch_res = subprocess.run(
                [str(py_bin), str(arch_script), "--repo-root", str(core_worktree)],
                cwd=core_worktree,
                capture_output=True,
                text=True,
            )
            if arch_res.returncode != 0:
                raise RuntimeError(f"Architecture gate failed:\n{arch_res.stderr}")
        if (venv_bin / "ruff").exists() and (core_worktree / "core").exists():
            ruff_targets = [str(core_worktree / "core")]
            if (instance_worktree / "extensions").exists():
                ruff_targets.append(str(instance_worktree / "extensions"))
            ruff_res = subprocess.run(
                [str(venv_bin / "ruff"), "check", *ruff_targets],
                cwd=core_worktree,
                capture_output=True,
                text=True,
            )
            if ruff_res.returncode != 0:
                raise RuntimeError(f"Ruff gate failed:\n{ruff_res.stdout}\n{ruff_res.stderr}")
        if (venv_bin / "mypy").exists() and (core_worktree / "core" / "alfred").exists():
            mypy_res = subprocess.run(
                [str(venv_bin / "mypy"), "--strict", "core/alfred", "core/scripts"],
                cwd=core_worktree,
                capture_output=True,
                text=True,
            )
            if mypy_res.returncode != 0:
                raise RuntimeError(f"Mypy gate failed:\n{mypy_res.stdout}\n{mypy_res.stderr}")
        if (venv_bin / "pytest").exists() and (core_worktree / "core" / "tests").exists():
            test_res = subprocess.run(
                [str(venv_bin / "pytest"), "--cov=core", "--cov-fail-under=90"],
                cwd=core_worktree,
                capture_output=True,
                text=True,
            )
            if test_res.returncode != 0:
                raise RuntimeError(f"Pytest TDD gate failed:\n{test_res.stdout}\n{test_res.stderr}")

    def _stage_and_commit_worktree(
        self,
        worktree: Path,
        branch: str,
        base_branch: str,
        title: str,
    ) -> list[str]:
        """Stage respected files, create commit when dirty, and return changed file list."""
        git_env = _sanitized_git_env(self._paths.root_dir)
        subprocess.run(
            ["git", "add", "-A"], cwd=worktree, env=git_env, check=True, capture_output=True
        )
        status_out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if status_out:
            subprocess.run(
                ["git", "commit", "-m", title],
                cwd=worktree,
                env=git_env,
                check=True,
                capture_output=True,
                text=True,
            )
        diff_out = subprocess.run(
            ["git", "diff", "--name-only", f"{base_branch}..{branch}"],
            cwd=worktree,
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        return [line.strip() for line in diff_out if line.strip()]

    def _try_open_remote_gh_pr(
        self,
        worktree: Path,
        branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> str | None:
        """Push branch and open a GitHub PR via gh CLI when remote 'origin' is configured."""
        git_env = _sanitized_git_env(self._paths.root_dir)
        remote_chk = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=worktree,
            env=git_env,
            capture_output=True,
            text=True,
            check=False,
        )
        gh_bin = shutil.which("gh")
        if remote_chk.returncode != 0 or not gh_bin:
            return None
        subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=worktree,
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        )
        pr_res = subprocess.run(
            [
                gh_bin,
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=worktree,
            env=git_env,
            check=True,
            capture_output=True,
            text=True,
        )
        return pr_res.stdout.strip()

    def _commit_and_open_pr(
        self,
        repo_name: str,
        worktree: Path,
        branch: str,
        base_branch: str,
        proposal_id: str,
        title: str,
        body: str,
    ) -> PullRequestArtifact:
        """Commit worktree changes and publish or materialize the Pull Request artifact."""
        changed_files = self._stage_and_commit_worktree(worktree, branch, base_branch, title)

        if self._pr_publisher_fn is not None:
            pr_ref = self._pr_publisher_fn(worktree, branch, base_branch, title, body)
            status = "OPEN_REMOTE"
        else:
            remote_url = (
                self._try_open_remote_gh_pr(worktree, branch, base_branch, title, body)
                if changed_files
                else None
            )
            if remote_url:
                pr_ref = remote_url
                status = "OPEN_REMOTE"
            else:
                pr_file = self._pr_dir / f"{proposal_id}_{repo_name}.md"
                pr_md = (
                    f"# Pull Request — `{repo_name}` (`{branch}` -> `{base_branch}`)\n\n"
                    f"## {title}\n\n{body}\n\n"
                    f"### Changed Files\n"
                    + ("\n".join(f"- `{f}`" for f in changed_files) or "- `(No file delta)`")
                    + "\n"
                )
                atomic_write_text(pr_file, pr_md)
                pr_ref = str(pr_file)
                status = "READY_FOR_REVIEW"

        return PullRequestArtifact(
            repo_name=repo_name,
            branch_name=branch,
            base_branch=base_branch,
            pr_url_or_ref=pr_ref,
            status=status,
            changed_files=changed_files,
        )
