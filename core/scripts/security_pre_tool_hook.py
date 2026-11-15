#!/usr/bin/env python3
"""Deterministic PreToolUse security hook protecting vault secrets and sandboxing inbox."""

from __future__ import annotations

import json
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path

PROTECTED_SECRET_TOKENS: tuple[str, ...] = (
    "signature_vault",
    "client_secrets.json",
    "token.json",
    "mcp_oauth.json",
    "oauth_pending.json",
    "leak_terms.txt",
)

WRITE_EDIT_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "multi_edit",
        "replace_file_content",
        "multi_replace_file_content",
        "create_file",
        "delete_file",
    }
)

COMMAND_TOOLS: frozenset[str] = frozenset({"run_command", "bash", "execute_command"})

PATH_INPUT_KEYS: tuple[str, ...] = (
    "Path",
    "path",
    "file_path",
    "TargetFile",
    "target_file",
    "Directory",
    "directory",
    "SearchPath",
)

ALLOWED_ALFRED_BINARIES: frozenset[str] = frozenset(
    {
        "alfred",
        "/root/Alfred/.venv/bin/alfred",
        "${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred",
        "$ALFRED_ROOT_DIR/.venv/bin/alfred",
        "${ALFRED_ROOT_DIR}/.venv/bin/alfred",
    }
)

INBOX_ALLOWED_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "poll-inbox",
        "send-draft",
        "archive-email",
        "backfill-inbox-archives",
        "search-gmail",
        "read-thread",
        "search-drive",
        "drive-read",
        "list-knowledge",
        "get-knowledge",
        "upsert-knowledge",
        "list-events",
        "create-event",
        "confirm-event",
        "update-event",
        "delete-event",
        "query-freebusy",
        "list-calendars",
        "delegate",
        "search-contacts",
        "list-tasks",
        "create-task",
    }
)


def _cwd_segments(cwd: str) -> set[str]:
    """Return lowercase path segments for a working directory string."""
    if not cwd.strip():
        return set()
    return {part.lower() for part in Path(cwd).parts if part}


def _is_inbox_cwd(cwd: str) -> bool:
    """Return True when cwd is inside the alfred-inbox workspace."""
    return bool(_cwd_segments(cwd) & {"alfred-inbox", "inbox"})


def _is_inbox_or_mcp_cwd(cwd: str) -> bool:
    """Return True when cwd is inside alfred-inbox or mcp workspaces."""
    return bool(_cwd_segments(cwd) & {"alfred-inbox", "inbox", "alfred-mcp", "mcp"})


def _extract_command_line(tool_input: Mapping[str, object]) -> str:
    """Extract command line text from a run_command tool_input mapping."""
    for key in ("CommandLine", "command", "cmd"):
        val = tool_input.get(key)
        if isinstance(val, str):
            return val.strip()
    return ""


def _extract_target_paths(tool_input: Mapping[str, object]) -> list[str]:
    """Extract file or directory path strings from tool_input."""
    paths: list[str] = []
    for key in PATH_INPUT_KEYS:
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            paths.append(val.strip())
    return paths


def _find_protected_secret_hit(tool_name: str, tool_input: Mapping[str, object]) -> str | None:
    """Return matched secret token if tool_input touches vault or OAuth secret files."""
    if tool_name.lower() in WRITE_EDIT_TOOLS:
        targets = _extract_target_paths(tool_input)
        corpus = "\n".join(targets) if targets else json.dumps(dict(tool_input))
    else:
        corpus = json.dumps(dict(tool_input))
    lowered = corpus.lower()
    for token in PROTECTED_SECRET_TOKENS:
        if token in lowered:
            return token
    return None


def _is_allowed_artifact_path(raw_path: str) -> bool:
    """Return True only when raw_path resolves inside an Antigravity brain artifact dir."""
    if not raw_path:
        return False
    try:
        resolved = Path(raw_path).resolve().as_posix()
    except OSError:
        return False
    return "/.gemini/" in resolved and "/brain/" in resolved


def _has_forbidden_shell_syntax(cmd: str) -> bool:
    """Detect shell chaining, command substitution, control newlines, or redirections."""
    if any(sub in cmd for sub in ("`", "$(", "\n", "\r", "&&", "||", ";", "|")):
        return True
    in_single = False
    in_double = False
    escaped = False
    for ch in cmd:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not in_single:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double and ch in {"&", ">", "<"}:
            return True
    return in_single or in_double


def _is_allowed_inbox_command(cmd: str) -> bool:
    """Validate that cmd is a single whitelisted alfred CLI subcommand without chaining."""
    if not cmd or _has_forbidden_shell_syntax(cmd):
        return False
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return False
    if len(tokens) < 2:
        return False
    return tokens[0] in ALLOWED_ALFRED_BINARIES and tokens[1] in INBOX_ALLOWED_SUBCOMMANDS


def evaluate_pre_tool_event(payload: Mapping[str, object]) -> dict[str, str]:
    """Evaluate a PreToolUse JSON event and return an allow/deny/force_ask verdict.

    Args:
        payload: Parsed PreToolUse JSON dictionary from Antigravity hook stdin.

    Returns:
        Dictionary containing 'decision' ('allow', 'deny', 'force_ask') and 'reason'.
    """
    tool_name = str(payload.get("tool_name", "")).strip()
    cwd = str(payload.get("cwd", "")).strip()
    raw_input = payload.get("tool_input")
    tool_input: Mapping[str, object] = raw_input if isinstance(raw_input, Mapping) else {}

    secret_hit = _find_protected_secret_hit(tool_name, tool_input)
    if secret_hit is not None:
        return {
            "decision": "deny",
            "reason": f"Access to protected secret resource '{secret_hit}' is forbidden.",
        }

    lower_tool = tool_name.lower()
    if lower_tool in COMMAND_TOOLS:
        cmd = _extract_command_line(tool_input)
        if "apply-signature" in cmd:
            if _is_inbox_or_mcp_cwd(cwd):
                return {
                    "decision": "deny",
                    "reason": "apply-signature is forbidden inside alfred-inbox and mcp.",
                }
            return {
                "decision": "force_ask",
                "reason": "Explicit human approval required before applying handwritten signature.",
            }

    if _is_inbox_cwd(cwd):
        if lower_tool in WRITE_EDIT_TOOLS:
            targets = _extract_target_paths(tool_input)
            if not targets or not all(_is_allowed_artifact_path(p) for p in targets):
                return {
                    "decision": "deny",
                    "reason": "alfred-inbox only allows writing Antigravity brain artifacts.",
                }
        elif lower_tool in COMMAND_TOOLS:
            cmd = _extract_command_line(tool_input)
            if not _is_allowed_inbox_command(cmd):
                return {
                    "decision": "deny",
                    "reason": "alfred-inbox restricts run_command to safe single alfred commands.",
                }

    return {"decision": "allow", "reason": "Permitted by Alfred V8 PreToolUse security policy."}


def main() -> int:
    """Read PreToolUse JSON payload from stdin and emit JSON decision on stdout."""
    raw_stdin = sys.stdin.read().strip()
    if not raw_stdin:
        print(json.dumps({"decision": "deny", "reason": "Empty PreToolUse stdin payload."}))
        return 0
    try:
        parsed = json.loads(raw_stdin)
    except json.JSONDecodeError:
        print(json.dumps({"decision": "deny", "reason": "Invalid PreToolUse JSON payload."}))
        return 0
    if not isinstance(parsed, Mapping):
        print(json.dumps({"decision": "deny", "reason": "PreToolUse payload must be an object."}))
        return 0
    verdict = evaluate_pre_tool_event(parsed)
    print(json.dumps(verdict, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
