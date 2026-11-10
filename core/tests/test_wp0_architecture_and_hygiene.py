"""Tests for WP0 architecture AST linter and upstream leak guard."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.check_code_architecture import inspect_python_file, run_architecture_check


def test_valid_clean_file_passes(tmp_path: Path) -> None:
    """Verify a compliant Python module with Google docstrings passes AST checks."""
    pkg = tmp_path / "core" / "alfred" / "domain"
    pkg.mkdir(parents=True)
    target = pkg / "sample.py"
    target.write_text(
        '"""Domain sample module."""\n\n\n'
        "class Item:\n"
        '    """Domain item entity."""\n\n'
        "    def compute(self, factor: int) -> int:\n"
        '        """Compute scaled value.\n\n'
        "        Args:\n"
        "            factor: Multiplier to apply.\n\n"
        "        Returns:\n"
        "            Scaled integer result.\n"
        '        """\n'
        "        return factor * 2\n",
        encoding="utf-8",
    )
    assert inspect_python_file(target, repo_root=tmp_path) == []
    assert run_architecture_check(tmp_path) == 0


def test_property_varargs_missing_args_and_env_root(tmp_path: Path, monkeypatch: object) -> None:
    """Verify @property passes without Args, missing Args fails, and ALFRED_ROOT_DIR resolves."""
    import pytest

    mp = pytest.MonkeyPatch()
    mp.setenv("ALFRED_ROOT_DIR", str(tmp_path))
    try:
        from scripts.check_code_architecture import resolve_default_root

        assert resolve_default_root() == tmp_path.resolve()
    finally:
        mp.undo()

    svc = tmp_path / "core" / "alfred" / "main.py"
    svc.parent.mkdir(parents=True)
    svc.write_text(
        '"""Entry module."""\n\n\n'
        "class Box:\n"
        '    """Container class."""\n\n'
        "    @property\n"
        "    def size(self) -> int:\n"
        '        """Return size."""\n'
        "        return 1\n\n"
        "    def bad_doc(self, *items: str, **opts: int) -> None:\n"
        '        """Missing Args section here."""\n'
        "        return None\n",
        encoding="utf-8",
    )
    violations = inspect_python_file(svc, repo_root=tmp_path)
    assert any("Google Docstring Args" in v for v in violations)


def test_file_exceeding_500_lines_fails(tmp_path: Path) -> None:
    """Verify any Python file over 500 physical lines is rejected."""
    target = tmp_path / "core" / "alfred" / "services" / "huge.py"
    target.parent.mkdir(parents=True)
    body = '"""Huge module."""\n' + "\n".join(f"CONST_{i} = {i}" for i in range(505))
    target.write_text(body, encoding="utf-8")
    violations = inspect_python_file(target, repo_root=tmp_path)
    assert any("500" in v for v in violations)


def test_main_exceeding_80_lines_fails(tmp_path: Path) -> None:
    """Verify core/alfred/main.py is strictly capped under 80 lines."""
    target = tmp_path / "core" / "alfred" / "main.py"
    target.parent.mkdir(parents=True)
    body = '"""Bootstrap entrypoint."""\n' + "\n".join(f"X_{i} = {i}" for i in range(80))
    target.write_text(body, encoding="utf-8")
    violations = inspect_python_file(target, repo_root=tmp_path)
    assert any("80" in v for v in violations)


def test_method_line_limit_excludes_google_docstring(tmp_path: Path) -> None:
    """Verify a method with 45 code lines and a 25-line docstring passes (<= 60 code lines)."""
    target = tmp_path / "core" / "alfred" / "services" / "doc_heavy.py"
    target.parent.mkdir(parents=True)
    doc_lines = "\n".join(f"        Doc detail line {i}." for i in range(25))
    code_lines = "\n".join(f"    val += {i}" for i in range(40))
    content = (
        '"""Service module."""\n\n\n'
        "def process(seed: int) -> int:\n"
        '    """Process seed value.\n\n'
        f"{doc_lines}\n\n"
        "    Args:\n"
        "        seed: Initial value.\n\n"
        "    Returns:\n"
        "        Processed integer.\n"
        '    """\n'
        "    val = seed\n"
        f"{code_lines}\n"
        "    return val\n"
    )
    target.write_text(content, encoding="utf-8")
    assert inspect_python_file(target, repo_root=tmp_path) == []


def test_method_exceeding_60_code_lines_fails(tmp_path: Path) -> None:
    """Verify a method exceeding 60 non-docstring lines is rejected."""
    target = tmp_path / "core" / "alfred" / "services" / "long_fn.py"
    target.parent.mkdir(parents=True)
    code_lines = "\n".join(f"    val += {i}" for i in range(62))
    content = (
        '"""Service module."""\n\n\n'
        "def too_long() -> int:\n"
        '    """Compute value.\n\n'
        "    Returns:\n"
        "        Result.\n"
        '    """\n'
        "    val = 0\n"
        f"{code_lines}\n"
        "    return val\n"
    )
    target.write_text(content, encoding="utf-8")
    violations = inspect_python_file(target, repo_root=tmp_path)
    assert any("60" in v for v in violations)


def test_missing_docstrings_and_syntax_error_detected(tmp_path: Path) -> None:
    """Verify missing module, class, and function docstrings and syntax errors fail."""
    target = tmp_path / "core" / "alfred" / "domain" / "undoc.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "class Foo:\n    def bar(self, x: int) -> int:\n        return x\n",
        encoding="utf-8",
    )
    violations = inspect_python_file(target, repo_root=tmp_path)
    assert len(violations) >= 3

    broken = tmp_path / "core" / "alfred" / "domain" / "broken.py"
    broken.write_text("def oops(:\n", encoding="utf-8")
    syntax_violations = inspect_python_file(broken, repo_root=tmp_path)
    assert any("Syntax" in v for v in syntax_violations)


def test_layer_import_violations_detected(tmp_path: Path) -> None:
    """Verify Clean Architecture inward dependency matrix blocks illegal imports."""
    domain_file = tmp_path / "core" / "alfred" / "domain" / "bad.py"
    domain_file.parent.mkdir(parents=True)
    domain_file.write_text(
        '"""Bad domain module."""\n\n'
        "from alfred.services.drive_service import DriveService\n"
        "from alfred import controllers\n"
        "from ..repositories import json_repository\n"
        "from .. import main\n",
        encoding="utf-8",
    )
    violations = inspect_python_file(domain_file, repo_root=tmp_path)
    assert len(violations) == 4
    assert all("Layer" in v for v in violations)

    repo_file = tmp_path / "core" / "alfred" / "repositories" / "bad_repo.py"
    repo_file.parent.mkdir(parents=True)
    repo_file.write_text(
        '"""Bad repository module."""\n\nimport alfred.controllers.cli_controller\n',
        encoding="utf-8",
    )
    assert any("Layer" in v for v in inspect_python_file(repo_file, repo_root=tmp_path))

    assert run_architecture_check(tmp_path) == 1
    assert run_architecture_check(tmp_path, only_targets=[tmp_path / "nonexistent"]) == 0


def test_upstream_leak_script_catches_private_ip_and_ct(tmp_path: Path) -> None:
    """Verify check-upstream-leak.sh blocks private LAN IPs, CT IDs, and webmail addresses."""
    script_path = Path("/root/Alfred/core/scripts/check-upstream-leak.sh")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Alfred Maintainer"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "alfred@example.com"],
        cwd=tmp_path,
        check=True,
    )
    leaked = tmp_path / "README.md"
    bad_ip = ".".join(["192", "168", "1", "42"])
    bad_ct = "CT " + "703"
    leaked.write_text(f"Host {bad_ip} on {bad_ct}\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)

    res = subprocess.run(
        ["bash", str(script_path), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0

    leaked.write_text("Clean documentation with user@example.com\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "chore: clean init"], cwd=tmp_path, check=True)

    clean_res = subprocess.run(
        ["bash", str(script_path), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert clean_res.returncode == 0, clean_res.stderr + clean_res.stdout


def test_leak_script_blocks_staged_data_dir_and_partial_staging(tmp_path: Path) -> None:
    """Verify check-upstream-leak.sh blocks force-staged data/ paths and partially staged IPs."""
    script_path = Path("/root/Alfred/core/scripts/check-upstream-leak.sh")
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Alfred Instance"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "instance@example.com"], cwd=tmp_path, check=True
    )
    (tmp_path / ".gitignore").write_text("data/\nsecrets/\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "note.md").write_text("Neutral text\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "data/note.md"], cwd=tmp_path, check=True)

    res_data = subprocess.run(
        ["bash", str(script_path), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_data.returncode != 0
    assert "data/note.md" in res_data.stderr
    subprocess.run(["git", "rm", "--cached", "-f", "data/note.md"], cwd=tmp_path, check=True)

    # Partial staging: stage secret IP in index, then overwrite working tree with clean text
    readme = tmp_path / "README.md"
    bad_lan = ".".join(["10", "0", "0", "25"])
    readme.write_text(f"Internal {bad_lan}\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    readme.write_text("Clean working copy\n", encoding="utf-8")

    res_partial = subprocess.run(
        ["bash", str(script_path), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_partial.returncode != 0
    assert "Staged Index" in res_partial.stderr
