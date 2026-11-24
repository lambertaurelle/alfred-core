"""Deterministic leak, AST architecture, ruff, mypy, pytest, and SPEC gate runners."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from alfred.domain.models import AlfredPaths
from alfred.services.core_spec_service import CoreSpecService
from alfred.services.evolve_governance_service import (
    EvolveGovernanceService,
    _sanitized_git_env,
)


def _run_english_only_gate(
    worktree: Path,
    python_bin: Path,
    git_env: dict[str, str],
) -> None:
    """Execute check_english_only.py against worktree when present."""
    eng_script = worktree / "core" / "scripts" / "check_english_only.py"
    if not eng_script.exists():
        return
    eng_env = dict(git_env)
    wt_core = worktree / "core"
    runner_core = Path(__file__).resolve().parents[2]
    py_paths = [
        str(p)
        for p in (wt_core, runner_core)
        if (p / "alfred" / "services" / "english_policy_service.py").is_file()
    ]
    if eng_env.get("PYTHONPATH"):
        py_paths.append(eng_env["PYTHONPATH"])
    if py_paths:
        eng_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(py_paths))
    eng_res = subprocess.run(
        [str(python_bin), str(eng_script), "--repo-root", str(worktree)],
        cwd=worktree,
        env=eng_env,
        capture_output=True,
        text=True,
        check=False,
    )
    if eng_res.returncode != 0:
        raise RuntimeError(f"English-only gate failed:\n{eng_res.stderr}\n{eng_res.stdout}")


def run_leak_and_ext_checks(
    worktree: Path,
    paths: AlfredPaths,
    python_bin: Path,
) -> None:
    """Enforce leak guard, AST limits, and pure-Python SPEC & isolation checks.

    Args:
        worktree: Isolated mono-repo git worktree path.
        paths: Resolved AlfredPaths layout.
        python_bin: Path to active Python interpreter.

    Raises:
        RuntimeError: If leak guard, architecture check, isolation, or SPEC fails.
    """
    leak_script = worktree / "core" / "scripts" / "check-upstream-leak.sh"
    git_env = _sanitized_git_env(paths.root_dir)
    if leak_script.exists():
        res = subprocess.run(
            ["bash", str(leak_script), str(worktree)],
            env=git_env,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"Zero Upstream Leak check failed:\n{res.stderr}\n{res.stdout}")

    arch_script = worktree / "core" / "scripts" / "check_code_architecture.py"
    if arch_script.exists():
        arch_cmd = [str(python_bin), str(arch_script), "--repo-root", str(worktree)]
        examples_dir = worktree / "instance" / "examples"
        if examples_dir.exists():
            arch_cmd.extend(["--extra-dir", str(examples_dir)])
        arch_res = subprocess.run(
            arch_cmd, cwd=worktree, env=git_env, capture_output=True, text=True, check=False
        )
        if arch_res.returncode != 0:
            msg = f"Architecture gate failed:\n{arch_res.stderr}\n{arch_res.stdout}"
            raise RuntimeError(msg)

    _run_english_only_gate(worktree, python_bin, git_env)

    if (worktree / "core" / "alfred" / "main.py").is_file() or (
        worktree / "docs" / "SPEC.md"
    ).is_file():
        iso_errors = EvolveGovernanceService().verify_standalone_core_isolation(worktree)
        if iso_errors:
            raise RuntimeError("Standalone core isolation check failed:\n" + "\n".join(iso_errors))
        spec_errors = CoreSpecService(paths).validate_spec_file(worktree)
        if spec_errors:
            raise RuntimeError("Living SPEC validation failed:\n" + "\n".join(spec_errors))


def run_tdd_and_leak_gates(
    worktree: Path,
    paths: AlfredPaths,
    python_bin: Path,
    tdd_gate_fn: Callable[[Path], None] | None = None,
) -> None:
    """Execute leak/isolation checks followed by custom or full 5-gate TDD verification.

    Args:
        worktree: Isolated mono-repo git worktree path.
        paths: Resolved AlfredPaths layout.
        python_bin: Path to active Python interpreter.
        tdd_gate_fn: Optional callback overriding ruff, mypy, and pytest gates.

    Raises:
        RuntimeError: If any deterministic gate fails.
    """
    run_leak_and_ext_checks(worktree, paths, python_bin)
    if tdd_gate_fn is not None:
        tdd_gate_fn(worktree)
        return

    venv_bin = paths.root_dir / ".venv" / "bin"
    wt_env = _sanitized_git_env(worktree)
    if (venv_bin / "ruff").exists() and (worktree / "core").exists():
        ruff_cmd = [str(venv_bin / "ruff"), "check"]
        if (worktree / "pyproject.toml").exists():
            ruff_cmd.extend(["--config", str(worktree / "pyproject.toml")])
        ruff_cmd.append(str(worktree / "core"))
        for sub_rel in ("extensions", "examples"):
            if (worktree / "instance" / sub_rel).exists():
                ruff_cmd.append(str(worktree / "instance" / sub_rel))
        r_res = subprocess.run(ruff_cmd, cwd=worktree, env=wt_env, capture_output=True, text=True)
        if r_res.returncode != 0:
            raise RuntimeError(f"Ruff gate failed:\n{r_res.stdout}\n{r_res.stderr}")
    if (venv_bin / "mypy").exists() and (worktree / "core" / "alfred").exists():
        targets = ["core/alfred", "core/scripts"]
        if (worktree / "core" / "tests").exists():
            targets.append("core/tests")
        m_res = subprocess.run(
            [str(venv_bin / "mypy"), "--strict", *targets],
            cwd=worktree, env=wt_env, capture_output=True, text=True,
        )
        if m_res.returncode != 0:
            raise RuntimeError(f"Mypy gate failed:\n{m_res.stdout}\n{m_res.stderr}")
    if (venv_bin / "pytest").exists() and (worktree / "core" / "tests").exists():
        t_res = subprocess.run(
            [str(venv_bin / "pytest"), "--cov=core", "--cov-fail-under=90"],
            cwd=worktree, env=wt_env, capture_output=True, text=True,
        )
        if t_res.returncode != 0:
            raise RuntimeError(f"Pytest TDD gate failed:\n{t_res.stdout}\n{t_res.stderr}")
