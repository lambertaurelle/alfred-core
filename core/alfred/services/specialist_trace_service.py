"""Cross-workspace specialist trace surfacing, sandbox guard, and deduplication."""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path

from alfred.domain.models import ActiveSessionRecord, AlfredSettings, utc_now_iso
from alfred.services.agy_archive_store import sanitize_annotation_title
from alfred.services.agy_daemon_sync import DaemonSyncStatus
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.agy_triage_parser import extract_conversation_id, parse_json_object

logger = logging.getLogger(__name__)


def should_surface_trace(
    archive_trace_flag: bool | None,
    target_project: str,
    cwd: Path | None = None,
) -> bool:
    """Return True when a mutation should surface a visible trace in target_project.

    Args:
        archive_trace_flag: Explicit CLI override (True forces, False suppresses, None auto).
        target_project: Target specialist workspace slug (e.g. 'alfred-calendar').
        cwd: Optional working directory path; defaults to Path.cwd().
    """
    if archive_trace_flag is False:
        return False
    if archive_trace_flag is True:
        return True
    raw_cwd = cwd if cwd is not None else Path.cwd()
    try:
        resolved = raw_cwd.resolve()
    except OSError:
        resolved = raw_cwd
    cleaned = target_project.strip()
    candidates = {cleaned}
    if cleaned and not cleaned.startswith("alfred-"):
        candidates.add(f"alfred-{cleaned}")
    parts = resolved.parts
    for idx in range(len(parts) - 1):
        if parts[idx] == "workspaces" and parts[idx + 1] in candidates:
            return False
    return True


def _host_agy_binary_paths() -> set[Path]:
    """Return resolved candidate paths for the real host agy binary."""
    paths = {
        (Path.home() / ".local" / "bin" / "agy").resolve(),
        Path("/root/.local/bin/agy").resolve(),
    }
    which_agy = shutil.which("agy")
    if which_agy:
        paths.add(Path(which_agy).resolve())
    return paths


def _is_custom_test_binary(agy_binary: str) -> bool:
    """Return True when agy_binary points to a non-host isolated test executable."""
    cfg_bin = agy_binary.strip()
    if not cfg_bin or cfg_bin == "agy":
        return False
    try:
        return Path(cfg_bin).resolve() not in _host_agy_binary_paths()
    except OSError:
        return False


def _has_live_overrides(agy_manager: AgySessionManager) -> bool:
    """Return True when agy_manager carries runner_fn or agy_locator_fn test doubles."""
    return bool(
        getattr(agy_manager, "has_live_overrides", False)
        or getattr(agy_manager, "_runner_fn", None) is not None
        or getattr(agy_manager, "_agy_locator_fn", None) is not None
    )


def is_live_trace_spawn_allowed(
    agy_manager: AgySessionManager,
    settings: AlfredSettings,
) -> bool:
    """Block live agy spawns against the real host data dir during un-mocked pytest runs.

    Args:
        agy_manager: Wired AgySessionManager instance.
        settings: Runtime AlfredSettings configuration.
    """
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    if _has_live_overrides(agy_manager) or _is_custom_test_binary(settings.agy_binary):
        return True
    default_host_dir = (Path.home() / ".gemini" / "antigravity-cli").resolve()
    raw_dir: object = getattr(agy_manager, "agy_data_dir", None) or getattr(
        agy_manager, "_agy_data_dir", default_host_dir
    )
    if not isinstance(raw_dir, (Path, str)):
        return False
    try:
        return Path(raw_dir).resolve() != default_host_dir
    except OSError:
        return False


def _is_safe_conv_id(conv_id: str | None) -> bool:
    """Return True when conv_id is a non-synthetic, path-traversal-safe conversation ID."""
    if not conv_id:
        return False
    cleaned = conv_id.strip()
    if not cleaned or cleaned.startswith("agy_"):
        return False
    if any(bad in cleaned for bad in ("/", "\\", "..", "\x00")) or cleaned in {".", ".."}:
        return False
    return Path(cleaned).name == cleaned


class SpecialistTraceService:
    """Surface visible EXECUTED traces into specialist Antigravity workspaces."""

    def surface_mutation_trace(
        self,
        agy_manager: AgySessionManager | None,
        settings: AlfredSettings,
        target_role: str,
        title: str,
        trace_markdown: str,
        source_id: str = "",
        archive_trace_flag: bool | None = None,
        cwd: Path | None = None,
    ) -> ActiveSessionRecord | None:
        """Spawn and persist a visible EXECUTED trace in target_role unless suppressed.

        Args:
            agy_manager: Optional AgySessionManager handling CLI spawns and daemon sync.
            settings: Runtime AlfredSettings resolving target workspace slugs.
            target_role: Target specialist role or slug ('calendar', 'tasks', etc.).
            title: Human-readable conversation title written to .pbtxt and SQLite.
            trace_markdown: Concise audit summary embedded in the zero-tool trace prompt.
            source_id: Optional source identifier (event_id, task_id, resource_name, etc.).
            archive_trace_flag: Explicit CLI override (True, False, or None for auto).
            cwd: Optional current working directory for same-workspace deduplication.
        """
        if agy_manager is None:
            return None
        target_project = settings.resolve_workspace_project(target_role)
        if not should_surface_trace(archive_trace_flag, target_project, cwd=cwd):
            return None
        clean_title = sanitize_annotation_title(title)
        live_id: str | None = None
        if is_live_trace_spawn_allowed(agy_manager, settings):
            live_id = self._spawn_trace_conversation(
                agy_manager, target_project, clean_title, trace_markdown
            )
        session_id = (
            live_id
            if _is_safe_conv_id(live_id)
            else f"agy_{target_project.replace('-', '_')}_{uuid.uuid4().hex[:10]}"
        )
        assert session_id is not None
        return self._persist_and_sync_trace(
            agy_manager, target_project, session_id, clean_title, source_id
        )

    def _persist_and_sync_trace(
        self,
        agy_manager: AgySessionManager,
        target_project: str,
        session_id: str,
        clean_title: str,
        source_id: str,
    ) -> ActiveSessionRecord | None:
        """Persist annotation title, EXECUTED StateStore record, and daemon sync safely."""
        try:
            if _is_safe_conv_id(session_id):
                agy_manager.archive_store.set_conversation_title(
                    session_id, clean_title, archived=False
                )
        except Exception as exc:
            logger.warning("Failed to set trace title for %s: %s", session_id, exc)
        ts = utc_now_iso()
        record = ActiveSessionRecord(
            session_id=session_id, workspace=target_project, status="EXECUTED",
            title=clean_title, outcome=clean_title, source_message_id=source_id,
            created_at=ts, archived_at=None,
        )
        try:
            agy_manager.state_store.conversations.update(
                lambda cur: cur.model_copy(
                    update={"sessions": {**cur.sessions, session_id: record}}
                )
            )
        except Exception as exc:
            logger.warning("Failed to persist trace record %s: %s", session_id, exc)
            return None
        try:
            agy_manager.daemon_sync.mark_dirty(session_id, reason=f"surface:{target_project}")
            if (
                agy_manager.daemon_sync.sync_if_needed().status
                == DaemonSyncStatus.DEFERRED_ACTIVE_TURN
            ):
                agy_manager.daemon_sync.spawn_deferred_sync_worker()
        except Exception as exc:
            logger.debug("Skipped daemon sync for trace %s: %s", session_id, exc)
        return record

    def _is_host_subprocess_blocked(self, agy_bin: str, agy_manager: AgySessionManager) -> bool:
        """Prevent invoking a real host agy binary via subprocess under pytest."""
        if not os.environ.get("PYTEST_CURRENT_TEST") or _has_live_overrides(agy_manager):
            return False
        settings_obj = getattr(agy_manager, "_settings", None)
        configured = str(getattr(settings_obj, "agy_binary", "agy") or "agy").strip()
        if configured == "agy":
            return True
        try:
            return Path(agy_bin).resolve() in _host_agy_binary_paths()
        except OSError:
            return True

    def _spawn_trace_conversation(
        self,
        agy_manager: AgySessionManager,
        target_project: str,
        clean_title: str,
        trace_markdown: str,
    ) -> str | None:
        """Execute a zero-tool trace spawn via agy CLI and return a safe conversation ID."""
        before_ids: set[str] | None = None
        try:
            agy_bin = agy_manager.locate_agy_binary()
            if agy_bin is None or self._is_host_subprocess_blocked(agy_bin, agy_manager):
                return None
            before_ids = agy_manager.archive_store.snapshot_summary_conversation_ids()
            prompt = (
                f"# {clean_title}\n\n"
                "Trace automatique d'exécution inter-workspace (zéro appel d'outil ; "
                "confirme en une phrase) :\n\n"
                f"{trace_markdown}"
            )
            cmd = [
                agy_bin, "--project", target_project,
                *agy_manager.build_model_cli_flags(target_project, trace=True),
                "--disable-slash-commands", "--output-format", "json", "--print", prompt,
            ]
            completed = agy_manager.run_agy_command(
                cmd, cwd=agy_manager.resolve_workspace_cwd(target_project)
            )
            live_id: str | None = None
            if completed is not None and completed.returncode == 0:
                parsed = parse_json_object(completed.stdout)
                if parsed is not None:
                    cand = extract_conversation_id(parsed)
                    if _is_safe_conv_id(cand):
                        live_id = cand
            if live_id is None and before_ids is not None:
                agy_manager.archive_store.purge_incomplete_stubs(exclude_ids=before_ids)
            return live_id
        except Exception:
            if before_ids is not None:
                agy_manager.archive_store.purge_incomplete_stubs(exclude_ids=before_ids)
            return None
