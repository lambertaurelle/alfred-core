#!/usr/bin/env python3
"""Deterministic PreToolUse security hook protecting vault, Drive IDs, and workspace sandboxes."""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml

PROTECTED_SECRET_TOKENS: tuple[str, ...] = (
    "signature_vault", "client_secret", "client_secrets.json", "client_secret.json",
    "token.json", "mcp_oauth.json", "oauth_pending.json", "leak_terms.txt", "instance/secrets",
)

WRITE_EDIT_TOOLS: frozenset[str] = frozenset({
    "write_file", "edit_file", "multi_edit", "replace_file_content",
    "multi_replace_file_content", "create_file", "delete_file",
})

COMMAND_TOOLS: frozenset[str] = frozenset({"run_command", "bash", "execute_command"})

PATH_INPUT_KEYS: tuple[str, ...] = (
    "Path", "path", "file_path", "TargetFile",
    "target_file", "Directory", "directory", "SearchPath",
)

ALLOWED_ALFRED_BINARIES: frozenset[str] = frozenset({
    "alfred",
    "/root/Alfred/.venv/bin/alfred",
    "${ALFRED_ROOT_DIR:-/root/Alfred}/.venv/bin/alfred",
    "$ALFRED_ROOT_DIR/.venv/bin/alfred",
    "${ALFRED_ROOT_DIR}/.venv/bin/alfred",
})

DRIVE_EXCLUSIVE_SUBCOMMANDS: frozenset[str] = frozenset({
    "watch-drive", "move-drive", "rename-drive", "create-drive-folder", "create-drive-shortcut",
})

DRIVE_WATCHER_ISOLATED_SUBCOMMANDS: frozenset[str] = frozenset({
    "upsert-knowledge", "save-attachment-to-drive",
})

INBOX_ALLOWED_SUBCOMMANDS: frozenset[str] = frozenset({
    "poll-inbox", "send-draft", "archive-email", "backfill-inbox-archives",
    "search-gmail", "read-thread", "list-attachments", "read-attachment",
    "save-attachment-to-drive", "search-drive", "read-drive", "drive-read",
    "scan-knowledge", "load-knowledge", "list-knowledge", "get-knowledge",
    "upsert-knowledge", "list-events", "create-event", "confirm-event",
    "update-event", "delete-event", "query-freebusy", "list-calendars",
    "delegate", "search-contacts", "create-contact", "list-tasklists",
    "list-tasks", "create-task", "complete-task", "update-task", "delete-task",
})

DRIVE_ALLOWED_SUBCOMMANDS: frozenset[str] = frozenset({
    "watch-drive", "move-drive", "rename-drive", "create-drive-folder",
    "create-drive-shortcut", "init-drive", "discover-drive", "search-drive",
    "sync-drive", "read-drive", "drive-read", "scan-knowledge", "load-knowledge",
    "list-knowledge", "get-knowledge", "upsert-knowledge", "list-attachments",
    "read-attachment", "save-attachment-to-drive", "delegate",
})


def _cwd_segments(cwd: str) -> set[str]:
    """Return lowercase path segments for a working directory string."""
    if not cwd.strip():
        return set()
    return {part.lower() for part in Path(cwd).parts if part}


def _is_inbox_cwd(cwd: str) -> bool:
    """Return True when cwd is inside the alfred-inbox workspace."""
    return bool(_cwd_segments(cwd) & {"alfred-inbox", "inbox"})


def _is_drive_cwd(cwd: str) -> bool:
    """Return True when cwd is inside the alfred-drive workspace."""
    return bool(_cwd_segments(cwd) & {"alfred-drive", "drive"})


def _is_untrusted_content_cwd(cwd: str) -> bool:
    """Return True when cwd is inside alfred-inbox, alfred-drive, or mcp workspaces."""
    return bool(
        _cwd_segments(cwd)
        & {"alfred-inbox", "inbox", "alfred-drive", "drive", "alfred-mcp", "mcp"}
    )


def _resolve_root_dir(cwd: str) -> Path:
    """Resolve active ALFRED_ROOT_DIR from environment, workspace cwd, or script location."""
    env_root = os.environ.get("ALFRED_ROOT_DIR", "").strip()
    if env_root:
        return Path(env_root).resolve()
    if cwd.strip():
        cwd_path = Path(cwd).resolve()
        for anc in (cwd_path, *cwd_path.parents):
            if (anc / "core" / "workspaces").is_dir():
                return anc
    return Path(__file__).resolve().parents[2]


def _load_runtime_hook_policy(cwd: str) -> tuple[bool, frozenset[str]]:
    """Load (drive_watcher_enabled, protected_signature_drive_ids) from settings.yaml."""
    settings_file = _resolve_root_dir(cwd) / "instance" / "config" / "settings.yaml"
    if not settings_file.is_file():
        return (False, frozenset())
    try:
        raw = yaml.safe_load(settings_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return (False, frozenset())
    if not isinstance(raw, dict):
        return (False, frozenset())
    feat_raw = raw.get("features")
    watcher_on = bool(feat_raw.get("drive_watcher", False)) if isinstance(feat_raw, dict) else False
    sec_raw = raw.get("security")
    raw_ids = (
        sec_raw.get("protected_signature_drive_ids", []) if isinstance(sec_raw, dict) else []
    )
    protected = (
        frozenset(str(i).strip() for i in raw_ids if str(i).strip())
        if isinstance(raw_ids, list)
        else frozenset()
    )
    return (watcher_on, protected)


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


def _find_protected_drive_id_hit(
    tool_name: str,
    tool_input: Mapping[str, object],
    protected_ids: frozenset[str],
) -> str | None:
    """Return matched protected Drive ID even when obfuscated by shell quote-splitting."""
    if not protected_ids:
        return None
    if tool_name.lower() in COMMAND_TOOLS:
        cmd = _extract_command_line(tool_input)
        tokens, _ = _extract_command_tokens(cmd)
        collapsed = cmd.replace("''", "").replace('""', "")
        haystacks = [*tokens, *(t.split("=", 1)[-1] for t in tokens if "=" in t), collapsed]
    else:
        haystacks = [json.dumps(dict(tool_input))]
    for pid in protected_ids:
        if any(pid in hay for hay in haystacks):
            return pid
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
    """Detect unsafe shell chaining, substitutions, newlines, or redirections outside '...'."""
    in_single = False
    in_double = False
    escaped = False
    idx = 0
    while idx < len(cmd):
        ch = cmd[idx]
        if in_single:
            if ch == "'":
                in_single = False
            idx += 1
            continue
        if escaped:
            if ch in {"\n", "\r"}:
                return True
            escaped = False
            idx += 1
            continue
        if ch == "\\":
            escaped = True
            idx += 1
            continue
        if not in_double and ch == "'":
            in_single = True
            idx += 1
            continue
        if ch == '"':
            in_double = not in_double
            idx += 1
            continue
        if ch in {"`", "\n", "\r", ";", "|", "&", ">", "<"}:
            return True
        if cmd[idx : idx + 2] in {"$(", "$'", "<(", ">("}:
            return True
        idx += 1
    return in_single or in_double or escaped


def _is_safe_upsert_knowledge_args(rest: list[str]) -> bool:
    """Ensure upsert-knowledge --body-file does not exfiltrate settings.yaml or secrets."""
    for body_file in _extract_flag_values(rest, "--body-file"):
        norm = body_file.replace("\\", "/").lower()
        if "settings.yaml" in norm or "instance/secrets" in norm:
            return False
    return True


def _is_allowed_inbox_command(cmd: str) -> bool:
    """Validate that cmd is a single whitelisted alfred CLI subcommand for alfred-inbox."""
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
    return not (subcmd == "upsert-knowledge" and not _is_safe_upsert_knowledge_args(rest))


def _is_allowed_drive_command(cmd: str) -> bool:
    """Validate that cmd is a single whitelisted alfred CLI subcommand for alfred-drive."""
    if not cmd or _has_forbidden_shell_syntax(cmd):
        return False
    tokens, _ = _extract_command_tokens(cmd)
    if len(tokens) < 2 or tokens[0] not in ALLOWED_ALFRED_BINARIES:
        return False
    subcmd, rest = tokens[1], tokens[2:]
    if subcmd not in DRIVE_ALLOWED_SUBCOMMANDS:
        return False
    if subcmd == "delegate":
        targets = [v.strip().lower() for v in _extract_flag_values(rest, "--to")]
        if not targets or any(t in {"evolve", "alfred-evolve"} for t in targets):
            return False
    return not (subcmd == "upsert-knowledge" and not _is_safe_upsert_knowledge_args(rest))


def _evaluate_command_isolation(
    cmd: str,
    cwd: str,
    drive_watcher_enabled: bool,
) -> dict[str, str] | None:
    """Enforce signature gates, alfred-drive exclusives, and opt-in write isolation."""
    tokens, norm_cmd = _extract_command_tokens(cmd)
    lower_tokens = [t.lower() for t in tokens]
    if "apply-signature" in lower_tokens or "apply-signature" in norm_cmd:
        if _is_untrusted_content_cwd(cwd):
            return {
                "decision": "deny",
                "reason": "apply-signature is forbidden inside untrusted content workspaces.",
            }
        return {
            "decision": "force_ask",
            "reason": "Explicit human approval required before applying handwritten signature.",
        }
    if (
        "prepare-signature" in lower_tokens or "prepare-signature" in norm_cmd
    ) and _is_untrusted_content_cwd(cwd):
        return {
            "decision": "deny",
            "reason": "prepare-signature is forbidden inside untrusted content workspaces.",
        }

    if len(tokens) >= 2 and (
        tokens[0] in ALLOWED_ALFRED_BINARIES or Path(tokens[0]).name == "alfred"
    ):
        subcmd = tokens[1]
        if subcmd in DRIVE_EXCLUSIVE_SUBCOMMANDS and not _is_drive_cwd(cwd):
            return {
                "decision": "deny",
                "reason": f"'{subcmd}' is reserved exclusively for workspace alfred-drive.",
            }
        if (
            drive_watcher_enabled
            and subcmd in DRIVE_WATCHER_ISOLATED_SUBCOMMANDS
            and not _is_drive_cwd(cwd)
        ):
            return {
                "decision": "deny",
                "reason": (
                    f"'{subcmd}' is isolated to alfred-drive when features.drive_watcher is true; "
                    "delegate via 'alfred delegate --to drive'."
                ),
            }
    return None


def _evaluate_workspace_sandbox(
    lower_tool: str,
    tool_input: Mapping[str, object],
    cwd: str,
) -> dict[str, str] | None:
    """Enforce write/command sandboxes inside alfred-inbox and alfred-drive."""
    in_inbox = _is_inbox_cwd(cwd)
    in_drive = _is_drive_cwd(cwd)
    if not in_inbox and not in_drive:
        return None
    ws_label = "alfred-drive" if in_drive else "alfred-inbox"
    if lower_tool in WRITE_EDIT_TOOLS:
        targets = _extract_target_paths(tool_input)
        if not targets or not all(_is_allowed_artifact_path(p) for p in targets):
            return {
                "decision": "deny",
                "reason": f"{ws_label} only allows writing Antigravity brain artifacts.",
            }
    elif lower_tool in COMMAND_TOOLS:
        cmd = _extract_command_line(tool_input)
        allowed = _is_allowed_drive_command(cmd) if in_drive else _is_allowed_inbox_command(cmd)
        if not allowed:
            return {
                "decision": "deny",
                "reason": f"{ws_label} restricts run_command to safe single alfred commands.",
            }
    return None


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

    watcher_on, protected_ids = _load_runtime_hook_policy(cwd)
    drive_id_hit = _find_protected_drive_id_hit(tool_name, tool_input, protected_ids)
    if drive_id_hit is not None:
        return {
            "decision": "deny",
            "reason": f"Access to protected signature Drive ID '{drive_id_hit}' is forbidden.",
        }

    lower_tool = tool_name.lower()
    if lower_tool in COMMAND_TOOLS:
        cmd_verdict = _evaluate_command_isolation(
            _extract_command_line(tool_input), cwd, watcher_on
        )
        if cmd_verdict is not None:
            return cmd_verdict

    sandbox_verdict = _evaluate_workspace_sandbox(lower_tool, tool_input, cwd)
    if sandbox_verdict is not None:
        return sandbox_verdict

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
