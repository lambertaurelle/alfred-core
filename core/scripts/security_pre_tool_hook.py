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
    "client_secret",
    "client_secrets.json",
    "client_secret.json",
    "token.json",
    "mcp_oauth.json",
    "oauth_pending.json",
    "leak_terms.txt",
    "instance/secrets",
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
        "list-attachments",
        "read-attachment",
        "save-attachment-to-drive",
        "search-drive",
        "read-drive",
        "drive-read",
        "scan-knowledge",
        "load-knowledge",
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
        "list-tasklists",
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


def _extract_command_tokens(cmd: str) -> tuple[list[str], str]:
    """Tokenize cmd with shlex (collapsing quote-splits) and return tokens + normalized text."""
    try:
        tokens = shlex.split(cmd, posix=True)
        return tokens, " ".join(tokens).lower()
    except ValueError:
        collapsed = cmd.replace("''", "").replace('""', "").lower()
        return [], collapsed


def _extract_flag_values(args: list[str], flag: str) -> list[str]:
    """Extract all CLI values passed for --flag val or --flag=val."""
    values: list[str] = []
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok == flag and idx + 1 < len(args):
            values.append(args[idx + 1])
            idx += 2
            continue
        if tok.startswith(flag + "="):
            values.append(tok[len(flag) + 1 :])
        idx += 1
    return values


def _find_protected_secret_hit(tool_name: str, tool_input: Mapping[str, object]) -> str | None:
    """Return matched secret token if tool_input touches vault or OAuth secret files."""
    lower_tool = tool_name.lower()
    if lower_tool in WRITE_EDIT_TOOLS:
        targets = _extract_target_paths(tool_input)
        corpus = "\n".join(targets) if targets else json.dumps(dict(tool_input))
    elif lower_tool in COMMAND_TOOLS:
        cmd = _extract_command_line(tool_input)
        _, norm_cmd = _extract_command_tokens(cmd)
        corpus = f"{json.dumps(dict(tool_input))}\n{norm_cmd}"
    else:
        corpus = json.dumps(dict(tool_input))
    lowered = corpus.lower().replace("\\", "/")
    for token in PROTECTED_SECRET_TOKENS:
        if token in lowered:
            return token
    return None


def _is_allowed_artifact_path(raw_path: str) -> bool:
    """Return True only when raw_path resolves inside the Antigravity brain artifact dir."""
    if not raw_path or "\x00" in raw_path:
        return False
    try:
        allowed_root = (Path.home() / ".gemini" / "antigravity-cli" / "brain").resolve()
        resolved = Path(raw_path).resolve()
    except (OSError, ValueError):
        return False
    return str(resolved).startswith(str(allowed_root) + "/")


def _has_forbidden_shell_syntax(cmd: str) -> bool:
    """Detect shell chaining, ANSI-C/process substitutions, newlines, or redirections."""
    forbidden_subs = ("`", "$(", "$'", "<(", ">(", "\n", "\r", "&&", "||", ";", "|")
    if any(sub in cmd for sub in forbidden_subs):
        return True
    in_single = False
    in_double = False
    in_ansi = False
    escaped = False
    idx = 0
    while idx < len(cmd):
        ch = cmd[idx]
        if escaped:
            escaped = False
        elif in_ansi:
            if ch == "\\":
                escaped = True
            elif ch == "'":
                in_ansi = False
        elif ch == "\\" and not in_single:
            escaped = True
        elif not in_single and not in_double and cmd[idx : idx + 2] == "$'":
            in_ansi = True
            idx += 1
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double and ch in {"&", ">", "<"}:
            return True
        idx += 1
    return in_single or in_double or in_ansi


def _is_allowed_inbox_command(cmd: str) -> bool:
    """Validate that cmd is a single whitelisted alfred CLI subcommand without policy bypass."""
    if not cmd or _has_forbidden_shell_syntax(cmd):
        return False
    tokens, _ = _extract_command_tokens(cmd)
    if len(tokens) < 2 or tokens[0] not in ALLOWED_ALFRED_BINARIES:
        return False
    subcmd, rest = tokens[1], tokens[2:]
    if subcmd not in INBOX_ALLOWED_SUBCOMMANDS:
        return False
    if subcmd == "delegate":
        targets = [v.strip().lower() for v in _extract_flag_values(rest, "--to")]
        if any(t in {"evolve", "alfred-evolve"} for t in targets):
            ws = [v.strip().lower() for v in _extract_flag_values(rest, "--source-workspace")]
            msg_ids = [v.strip() for v in _extract_flag_values(rest, "--source-message-id")]
            if not ws or ws[-1] != "alfred-inbox" or not msg_ids or not msg_ids[-1]:
                return False
    if subcmd == "upsert-knowledge":
        for body_file in _extract_flag_values(rest, "--body-file"):
            norm = body_file.replace("\\", "/").lower()
            if "settings.yaml" in norm or "instance/secrets" in norm:
                return False
    return True


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
        tokens, norm_cmd = _extract_command_tokens(cmd)
        lower_tokens = [t.lower() for t in tokens]
        if "apply-signature" in lower_tokens or "apply-signature" in norm_cmd:
            if _is_inbox_or_mcp_cwd(cwd):
                return {
                    "decision": "deny",
                    "reason": "apply-signature is forbidden inside alfred-inbox and mcp.",
                }
            return {
                "decision": "force_ask",
                "reason": "Explicit human approval required before applying handwritten signature.",
            }
        if (
            ("prepare-signature" in lower_tokens or "prepare-signature" in norm_cmd)
            and _is_inbox_or_mcp_cwd(cwd)
        ):
            return {
                "decision": "deny",
                "reason": "prepare-signature is forbidden inside alfred-inbox and mcp.",
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
