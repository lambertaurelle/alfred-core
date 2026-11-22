"""Pure-AST extraction helpers for living specification generation and leak checks."""

from __future__ import annotations

import ast
from pathlib import Path

from alfred.domain.evolve_models import CliCommandSpec, FeatureSpecEntry


def assigned_target_name(stmt: ast.stmt) -> str:
    """Return the simple assigned variable name for Assign or AnnAssign, else empty.

    Args:
        stmt: AST statement node.

    Returns:
        Target identifier string or empty string.
    """
    if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        return stmt.target.id
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target = stmt.targets[0]
        if isinstance(target, ast.Name):
            return target.id
    return ""


def _safe_literal_dict(node: ast.AST | None) -> dict[str, tuple[str, ...]]:
    """Evaluate a literal dict[str, tuple|list[str]] AST node without code execution."""
    if node is None:
        return {}
    try:
        evaluated = ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return {}
    if not isinstance(evaluated, dict):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for key, val in evaluated.items():
        if isinstance(key, str) and isinstance(val, (tuple, list)):
            result[key] = tuple(str(item) for item in val if isinstance(item, str))
    return result


def extract_features_from_source(source: str) -> list[FeatureSpecEntry]:
    """Extract FeatureFlagsConfig flags, defaults, APIs, and OAuth scopes via pure AST.

    Args:
        source: Raw Python source of core/alfred/domain/feature_models.py.

    Returns:
        Ordered list of FeatureSpecEntry records.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    oauth_map: dict[str, tuple[str, ...]] = {}
    gcp_map: dict[str, tuple[str, ...]] = {}
    flags_class: ast.ClassDef | None = None
    for stmt in tree.body:
        name = assigned_target_name(stmt)
        val_node = getattr(stmt, "value", None)
        if name == "FEATURE_OAUTH_MAP":
            oauth_map = _safe_literal_dict(val_node)
        elif name == "FEATURE_GCP_APIS_MAP":
            gcp_map = _safe_literal_dict(val_node)
        elif isinstance(stmt, ast.ClassDef) and stmt.name == "FeatureFlagsConfig":
            flags_class = stmt
    if flags_class is None:
        return []
    entries: list[FeatureSpecEntry] = []
    for member in flags_class.body:
        if not isinstance(member, ast.AnnAssign) or not isinstance(member.target, ast.Name):
            continue
        flag_name = member.target.id
        if flag_name == "model_config":
            continue
        default_val = False
        if isinstance(member.value, ast.Constant) and isinstance(member.value.value, bool):
            default_val = member.value.value
        entries.append(
            FeatureSpecEntry(
                flag_name=flag_name,
                default_enabled=default_val,
                gcp_apis=list(gcp_map.get(flag_name, ())),
                oauth_scopes=list(oauth_map.get(flag_name, ())),
            )
        )
    return entries


def _extract_add_parser_name(expr: ast.AST) -> str:
    """Return subcommand string when expr is '<subparsers>.add_parser("<name>", ...)'."""
    if not isinstance(expr, ast.Call):
        return ""
    if not isinstance(expr.func, ast.Attribute) or expr.func.attr != "add_parser":
        return ""
    if expr.args and isinstance(expr.args[0], ast.Constant) and isinstance(expr.args[0].value, str):
        return expr.args[0].value
    return ""


def _record_add_argument(
    expr: ast.AST,
    var_to_cmd: dict[str, str],
    cmd_options: dict[str, list[str]],
    loop_vars: dict[str, list[str]],
) -> None:
    """Record '--flag' options from '<parser_var>.add_argument(...)' into cmd_options."""
    if not isinstance(expr, ast.Call):
        return
    if not isinstance(expr.func, ast.Attribute) or expr.func.attr != "add_argument":
        return
    if not isinstance(expr.func.value, ast.Name):
        return
    cmd_name = var_to_cmd.get(expr.func.value.id)
    if not cmd_name:
        return
    opts = cmd_options.setdefault(cmd_name, [])
    for arg in expr.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if arg.value.startswith("-") and arg.value not in opts:
                opts.append(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in loop_vars:
            for item in loop_vars[arg.id]:
                if item.startswith("-") and item not in opts:
                    opts.append(item)


def _handle_for_loop(
    stmt: ast.For,
    var_to_cmd: dict[str, str],
    cmd_options: dict[str, list[str]],
    loop_vars: dict[str, list[str]],
) -> None:
    """Extract constant string iterables in a for-loop before walking its body."""
    added_var = ""
    if isinstance(stmt.target, ast.Name) and isinstance(stmt.iter, (ast.Tuple, ast.List)):
        str_items = [
            elt.value
            for elt in stmt.iter.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
        if str_items:
            added_var = stmt.target.id
            loop_vars[added_var] = str_items
    _collect_cli_from_block(stmt.body, var_to_cmd, cmd_options, loop_vars)
    if added_var:
        loop_vars.pop(added_var, None)


def _collect_cli_from_block(
    stmts: list[ast.stmt],
    var_to_cmd: dict[str, str],
    cmd_options: dict[str, list[str]],
    loop_vars: dict[str, list[str]],
) -> None:
    """Walk statement blocks collecting add_parser() commands and add_argument() options."""
    for stmt in stmts:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            cmd_name = _extract_add_parser_name(stmt.value)
            if isinstance(target, ast.Name) and cmd_name:
                var_to_cmd[target.id] = cmd_name
                cmd_options.setdefault(cmd_name, [])
        elif isinstance(stmt, ast.Expr):
            cmd_name = _extract_add_parser_name(stmt.value)
            if cmd_name:
                cmd_options.setdefault(cmd_name, [])
            _record_add_argument(stmt.value, var_to_cmd, cmd_options, loop_vars)
        elif isinstance(stmt, ast.For):
            _handle_for_loop(stmt, var_to_cmd, cmd_options, loop_vars)
        elif isinstance(stmt, ast.If):
            _collect_cli_from_block(stmt.body, var_to_cmd, cmd_options, loop_vars)
            _collect_cli_from_block(stmt.orelse, var_to_cmd, cmd_options, loop_vars)


def extract_cli_commands_from_source(
    source: str,
    source_module: str = "",
) -> list[CliCommandSpec]:
    """Extract subparser CLI commands and option flags from controller Python source via AST.

    Args:
        source: Raw Python source of a controller module.
        source_module: Relative module path recorded on each CliCommandSpec.

    Returns:
        List of CliCommandSpec descriptors parsed without importing the module.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    cmd_options: dict[str, list[str]] = {}
    top_vars: dict[str, str] = {}
    _collect_cli_from_block(tree.body, top_vars, cmd_options, {})
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _collect_cli_from_block(node.body, dict(top_vars), cmd_options, {})
    return [
        CliCommandSpec(name=cmd, options=opts, source_module=source_module)
        for cmd, opts in cmd_options.items()
    ]


def extract_extension_tokens_ast(py_file: Path) -> list[str]:
    """Extract private extension stem and static AST extension_id without executing code.

    Args:
        py_file: Path to a private extension Python file.

    Returns:
        List containing file stem and any AST-discovered extension_id literals.
    """
    tokens: list[str] = [py_file.stem]
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return tokens
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and assigned_target_name(node) == "extension_id"
        ):
            val = getattr(node, "value", None)
            if isinstance(val, ast.Constant) and isinstance(val.value, str) and val.value.strip():
                tokens.append(val.value.strip())
        elif isinstance(node, ast.FunctionDef) and node.name == "extension_id":
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Return)
                    and isinstance(inner.value, ast.Constant)
                    and isinstance(inner.value.value, str)
                    and inner.value.value.strip()
                ):
                    tokens.append(inner.value.value.strip())
    return tokens
