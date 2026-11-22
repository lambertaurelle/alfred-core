#!/usr/bin/env python3
"""Deterministic AST verifier for Clean Architecture OOP, line limits, and Google docstrings."""

from __future__ import annotations

import argparse
import ast
import os
import sys
from pathlib import Path

MAX_FILE_LINES = 500
MAX_MAIN_LINES = 79
MAX_METHOD_LINES = 60

KNOWN_LAYERS = {"domain", "repositories", "services", "controllers", "main"}

FORBIDDEN_LAYER_IMPORTS: dict[str, set[str]] = {
    "domain": {"repositories", "services", "controllers", "main"},
    "repositories": {"services", "controllers", "main"},
    "services": {"controllers", "main"},
    "controllers": {"main"},
}


def resolve_default_root() -> Path:
    """Resolve the Alfred repository root directory from environment or script path.

    Returns:
        Resolved repository root directory path.
    """
    env_root = os.environ.get("ALFRED_ROOT_DIR")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parents[2]


def _compute_function_code_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Calculate function line count excluding its leading Google-style docstring.

    Args:
        node: AST function or async function definition node.

    Returns:
        Number of lines in the function excluding any docstring expression.
    """
    end_lineno = getattr(node, "end_lineno", node.lineno) or node.lineno
    total_lines = end_lineno - node.lineno + 1
    if node.body:
        first_stmt = node.body[0]
        if (
            isinstance(first_stmt, ast.Expr)
            and isinstance(first_stmt.value, ast.Constant)
            and isinstance(first_stmt.value.value, str)
        ):
            doc_end = getattr(first_stmt, "end_lineno", first_stmt.lineno) or first_stmt.lineno
            doc_lines = doc_end - first_stmt.lineno + 1
            return max(1, total_lines - doc_lines)
    return total_lines


def _alfred_rel_parts(file_path: Path, repo_root: Path) -> tuple[str, ...] | None:
    """Return path parts relative to core/alfred, or None if outside core/alfred."""
    try:
        rel = file_path.resolve().relative_to((repo_root / "core" / "alfred").resolve())
    except ValueError:
        return None
    return rel.parts or None


def _detect_layer(file_path: Path, repo_root: Path) -> str | None:
    """Identify the Clean Architecture layer for a file inside core/alfred.

    Args:
        file_path: Python source file path.
        repo_root: Repository root directory.

    Returns:
        Layer name ('domain', 'repositories', 'services', 'controllers', 'main') or None.
    """
    parts = _alfred_rel_parts(file_path, repo_root)
    if not parts:
        return None
    if parts[0] == "main.py":
        return "main"
    if parts[0] in FORBIDDEN_LAYER_IMPORTS:
        return parts[0]
    return None


def _resolve_import_from_layers(
    node: ast.ImportFrom,
    pkg_parts: list[str],
    current_layer: str,
) -> list[tuple[int, str]]:
    """Resolve target Clean Architecture layers for an ImportFrom AST node."""
    hits: list[tuple[int, str]] = []
    if node.level == 0:
        if not node.module:
            return hits
        parts = node.module.split(".")
        if parts[0] != "alfred":
            return hits
        if len(parts) >= 2 and parts[1] in KNOWN_LAYERS:
            hits.append((node.lineno, parts[1]))
        elif len(parts) == 1:
            for alias in node.names:
                if alias.name in KNOWN_LAYERS:
                    hits.append((node.lineno, alias.name))
        return hits

    climb = node.level - 1
    anchor = pkg_parts[: max(0, len(pkg_parts) - climb)] if climb <= len(pkg_parts) else []
    mod_parts = node.module.split(".") if node.module else []
    combined = [*anchor, *mod_parts]
    if combined and combined[0] in KNOWN_LAYERS:
        hits.append((node.lineno, combined[0]))
    elif not combined:
        for alias in node.names:
            if alias.name in KNOWN_LAYERS and alias.name != current_layer:
                hits.append((node.lineno, alias.name))
    return hits


def _extract_imported_alfred_layers(
    tree: ast.Module,
    current_layer: str,
    pkg_parts: list[str],
) -> list[tuple[int, str]]:
    """Extract target Clean Architecture layers referenced by import statements.

    Args:
        tree: Parsed AST module.
        current_layer: Current file's Clean Architecture layer.
        pkg_parts: Package path components inside core/alfred.

    Returns:
        List of (lineno, target_layer) pairs imported from alfred.
    """
    imported: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if len(parts) >= 2 and parts[0] == "alfred" and parts[1] in KNOWN_LAYERS:
                    imported.append((node.lineno, parts[1]))
        elif isinstance(node, ast.ImportFrom):
            imported.extend(_resolve_import_from_layers(node, pkg_parts, current_layer))
    return imported


def _check_function_docstring_sections(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    docstring: str,
    is_test_file: bool,
) -> list[str]:
    """Check Google-style Args section presence on public non-test functions with parameters."""
    if is_test_file or node.name.startswith("_"):
        return []
    decorator_names = {
        (d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")) for d in node.decorator_list
    }
    if "property" in decorator_names:
        return []
    params = [
        arg.arg
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if arg.arg not in {"self", "cls"}
    ]
    if node.args.vararg is not None:
        params.append(node.args.vararg.arg)
    if node.args.kwarg is not None:
        params.append(node.args.kwarg.arg)
    if params and "Args:" not in docstring:
        return [
            f"ERROR [Google Docstring Args]: line {node.lineno} '{node.name}' "
            f"has parameters {params} but lacks an 'Args:' section."
        ]
    return []


def _inspect_ast_nodes(
    tree: ast.Module,
    file_path: Path,
    is_test_file: bool,
) -> list[str]:
    """Validate docstrings and method line limits across all AST classes and functions."""
    violations: list[str] = []
    if not ast.get_docstring(tree):
        violations.append(f"ERROR [Module Docstring]: {file_path} lacks a module docstring.")

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and not ast.get_docstring(node):
            violations.append(
                f"ERROR [Class Docstring]: {file_path}:{node.lineno} "
                f"class '{node.name}' lacks a docstring."
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            code_lines = _compute_function_code_lines(node)
            if code_lines > MAX_METHOD_LINES:
                violations.append(
                    f"ERROR [Method Lines]: {file_path}:{node.lineno} '{node.name}' "
                    f"has {code_lines} code lines excluding docstring (max {MAX_METHOD_LINES})."
                )
            doc = ast.get_docstring(node)
            if not doc:
                violations.append(
                    f"ERROR [Function Docstring]: {file_path}:{node.lineno} "
                    f"function '{node.name}' lacks a docstring."
                )
            else:
                violations.extend(_check_function_docstring_sections(node, doc, is_test_file))
    return violations


def _is_inside_core(resolved: Path, root: Path) -> bool:
    """Return True when resolved resides inside repo_root/core/."""
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return False
    return bool(rel.parts and rel.parts[0] == "core")


def _check_instance_coupling(tree: ast.Module, resolved: Path) -> list[str]:
    """Reject any top-level or deferred import from instance/ inside core/."""
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "instance" or alias.name.startswith("instance."):
                    violations.append(
                        f"ERROR [Instance Coupling]: {resolved}:{node.lineno} "
                        f"core/ module cannot import '{alias.name}'."
                    )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and (node.module == "instance" or node.module.startswith("instance."))
        ):
            violations.append(
                f"ERROR [Instance Coupling]: {resolved}:{node.lineno} "
                f"core/ module cannot import from '{node.module}'."
            )
    return violations


def inspect_python_file(file_path: Path, repo_root: Path | None = None) -> list[str]:
    """Inspect a single Python file against all V8.0 Clean Architecture invariants.

    Args:
        file_path: Path to the Python file to inspect.
        repo_root: Optional repository root override for layer resolution.

    Returns:
        List of human-readable violation strings (empty when compliant).
    """
    root = (repo_root or resolve_default_root()).resolve()
    resolved = file_path.resolve()
    violations: list[str] = []
    lines = resolved.read_text(encoding="utf-8").splitlines()
    line_count = len(lines)

    if line_count > MAX_FILE_LINES:
        violations.append(
            f"ERROR [File Lines]: {resolved} has {line_count} lines (max {MAX_FILE_LINES})."
        )
    if resolved.name == "main.py" and "alfred" in resolved.parts and line_count > MAX_MAIN_LINES:
        violations.append(
            f"ERROR [Main Lines]: {resolved} has {line_count} lines (must be < 80 lines)."
        )

    try:
        tree = ast.parse("\n".join(lines), filename=str(resolved))
    except SyntaxError as exc:
        violations.append(f"ERROR [Syntax]: {resolved} failed AST parse: {exc}")
        return violations

    is_test = "tests" in resolved.parts or resolved.name.startswith("test_")
    violations.extend(_inspect_ast_nodes(tree, resolved, is_test_file=is_test))
    if _is_inside_core(resolved, root):
        violations.extend(_check_instance_coupling(tree, resolved))

    rel_parts = _alfred_rel_parts(resolved, root)
    layer = _detect_layer(resolved, root)
    if layer and layer in FORBIDDEN_LAYER_IMPORTS and rel_parts is not None:
        pkg_parts = list(rel_parts[:-1])
        forbidden = FORBIDDEN_LAYER_IMPORTS[layer]
        for lineno, target_layer in _extract_imported_alfred_layers(tree, layer, pkg_parts):
            if target_layer in forbidden:
                violations.append(
                    f"ERROR [Layer Violation]: {resolved}:{lineno} layer '{layer}' "
                    f"cannot import from layer '{target_layer}'."
                )
    return violations


def run_architecture_check(
    repo_root: Path | None = None,
    extra_targets: list[Path] | None = None,
    only_targets: list[Path] | None = None,
) -> int:
    """Scan Python files in core/ and instance/extensions/ and report violations.

    Args:
        repo_root: Optional repository root directory override.
        extra_targets: Additional directories to inspect alongside default targets.
        only_targets: Explicit directory list replacing default targets when provided.

    Returns:
        Process exit code (0 on success, 1 when violations exist).
    """
    root = (repo_root or resolve_default_root()).resolve()
    targets = (
        [p.resolve() for p in only_targets]
        if only_targets
        else [root / "core", root / "instance" / "extensions", *(extra_targets or [])]
    )
    all_violations: list[str] = []

    for target in targets:
        if not target.exists():
            continue
        if target.is_file() and target.suffix == ".py":
            all_violations.extend(inspect_python_file(target, repo_root=root))
            continue
        for py_file in sorted(target.rglob("*.py")):
            if "__pycache__" in py_file.parts or ".venv" in py_file.parts:
                continue
            all_violations.extend(inspect_python_file(py_file, repo_root=root))

    if all_violations:
        print("\n".join(all_violations), file=sys.stderr)
        print(
            f"\nArchitecture check failed with {len(all_violations)} violation(s).",
            file=sys.stderr,
        )
        return 1

    print("Clean Architecture check passed (<=500L/file, <=60L/method, 100% docstrings, 4 layers).")
    return 0


def _parse_cli_args(argv: list[str]) -> argparse.Namespace:
    """Parse optional CLI directory flags for targeted or worktree architecture checks."""
    parser = argparse.ArgumentParser(description="Clean Architecture AST verifier")
    parser.add_argument("--root", default=None)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--extra-dir", action="append", default=[])
    parser.add_argument("--only-dir", action="append", default=[])
    return parser.parse_args(argv)


def _resolve_cli_targets(
    ns: argparse.Namespace,
) -> tuple[Path | None, list[Path], list[Path] | None]:
    """Resolve (repo_root, extra_targets, only_targets) from CLI flags including --root."""
    repo_root = Path(ns.repo_root).resolve() if ns.repo_root else None
    extra_targets = [Path(p) for p in ns.extra_dir]
    only_targets: list[Path] | None = [Path(p) for p in ns.only_dir] if ns.only_dir else None

    if ns.root:
        root_target = Path(ns.root).resolve()
        is_nested_wt_container = (
            (root_target / "core" / "core" / "alfred").is_dir()
            and not (root_target / "core" / "alfred").is_dir()
        )
        if is_nested_wt_container:
            if repo_root is None:
                repo_root = root_target / "core"
            only_targets = [*(only_targets or []), Path(ns.root)]
        elif (root_target / "core").is_dir():
            if repo_root is None:
                repo_root = root_target
        else:
            if repo_root is None:
                inferred = next(
                    (anc for anc in root_target.parents if (anc / "core" / "alfred").is_dir()),
                    None,
                )
                repo_root = inferred or resolve_default_root()
            only_targets = [*(only_targets or []), Path(ns.root)]

    return repo_root, extra_targets, only_targets


if __name__ == "__main__":
    resolved_root, resolved_extra, resolved_only = _resolve_cli_targets(
        _parse_cli_args(sys.argv[1:])
    )
    sys.exit(
        run_architecture_check(
            repo_root=resolved_root,
            extra_targets=resolved_extra,
            only_targets=resolved_only,
        )
    )
