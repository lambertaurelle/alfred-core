"""Unified V8 periodic routine runner and systemd --user timer installer (0-token idle)."""

from __future__ import annotations

import importlib.util
import inspect
import subprocess
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from zoneinfo import ZoneInfo

from alfred.domain.extension_protocol import (
    AlfredExtension,
    ExtensionContext,
    ExtensionJobOutcome,
)
from alfred.domain.models import AlfredPaths, AlfredSettings
from alfred.domain.routine_models import (
    RoutineJobRun,
    RoutinesStatusReport,
    RoutineTickReport,
)
from alfred.repositories.json_repository import atomic_write_text
from alfred.repositories.state_store import StateStore
from alfred.services.agy_daemon_sync import DaemonSyncStatus
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.drive_governance_service import DriveGovernanceService
from alfred.services.drive_service import DriveService
from alfred.services.drive_watcher_service import DriveWatcherService
from alfred.services.extension_loader import ExtensionLoaderService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.systemd_unit_renderer import (
    compute_stale_systemd_specs,
    render_alfred_systemd_units,
)

ALL_SYSTEMD_TIMERS: tuple[str, ...] = ("alfred-inbox.timer", "alfred-periodic.timer")


class RoutineService:
    """Orchestrate 0-token periodic Gmail, Drive, extension, and backup routines + systemd."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
        inbox_service: InboxTriageService,
        drive_service: DriveService,
        agy_manager: AgySessionManager | None = None,
        systemctl_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
        drive_watcher_service: DriveWatcherService | None = None,
    ) -> None:
        """Initialize the V8 periodic routine orchestrator.

        Args:
            paths: Resolved AlfredPaths layout.
            settings: Structural runtime configuration including features and routines blocks.
            state_store: Local atomic state facade.
            inbox_service: Gmail incremental triage service.
            drive_service: Google Drive incremental delta sync and backup service.
            agy_manager: Optional Antigravity Web session manager for daemon reconciliation.
            systemctl_runner: Optional subprocess override for systemctl --user invocations.
            drive_watcher_service: Optional 2-speed DriveWatcherService for scope 'drive'.
        """
        self._paths = paths
        self._settings = settings
        self._store = state_store
        self._inbox = inbox_service
        self._drive = drive_service
        self._agy = agy_manager
        self._systemctl_runner = systemctl_runner
        self._drive_watcher = drive_watcher_service

    def run_routines(
        self,
        scope: str = "all",
        today: date | None = None,
    ) -> RoutineTickReport:
        """Execute inbox, drive, and/or periodic routines under a non-blocking flock.

        Args:
            scope: Execution scope ('all', 'inbox', 'drive', or 'periodic').
            today: Optional reference date override (defaults to configured timezone today).

        Returns:
            RoutineTickReport summarizing each job's status and processed item counts.
        """
        ref_day = today or datetime.now(ZoneInfo(self._settings.timezone)).date()
        lock_name = f"alfred_{scope}"
        with self._store.acquire_routine_lock(lock_name) as acquired:
            if not acquired:
                skip = RoutineJobRun(
                    job_name=lock_name,
                    status="SKIPPED_LOCKED",
                    details="Another routine process already holds this scope lock.",
                )
                return RoutineTickReport(scope=scope, locked_skip=True, jobs=[skip])

            jobs: list[RoutineJobRun] = []
            if scope in {"all", "inbox"}:
                jobs.append(self._run_guarded("inbox_poll", self._job_inbox_poll))
            if scope in {"all", "drive"}:
                jobs.append(self._run_guarded("drive_watcher", self._job_drive_watcher))
            if scope in {"all", "periodic"}:
                jobs.append(self._run_guarded("drive_delta_sync", self._job_drive_delta_sync))
                jobs.append(
                    self._run_guarded(
                        "instance_extensions",
                        lambda: self._job_instance_extensions(ref_day),
                    )
                )
                jobs.append(
                    self._run_guarded("instance_drive_backup", self._job_instance_drive_backup)
                )
            jobs.append(self._run_guarded("agy_ui_sync", self._job_agy_ui_sync))
            self._record_job_runs(jobs)
            return RoutineTickReport(scope=scope, jobs=jobs)

    def render_systemd_units(self) -> dict[str, str]:
        """Render systemd --user unit files for periodic, inbox, drive, and mcp services.

        Returns:
            Mapping from unit file name to UTF-8 systemd unit text.
        """
        return render_alfred_systemd_units(self._paths, self._settings)

    def install_systemd_timers(
        self,
        systemd_user_dir: Path | None = None,
        enable_now: bool = True,
    ) -> list[Path]:
        """Write systemd --user units, prune stale/disabled units, and optionally enable/restart.

        Args:
            systemd_user_dir: Optional target directory (defaults to ~/.config/systemd/user).
            enable_now: When True, invoke systemctl --user daemon-reload and enable --now.

        Returns:
            Ordered list of written unit file Paths.
        """
        target_dir = systemd_user_dir or (Path.home() / ".config" / "systemd" / "user")
        target_dir.mkdir(parents=True, exist_ok=True)
        units = self.render_systemd_units()
        self._prune_stale_systemd_units(target_dir, enable_now=enable_now)

        written: list[Path] = []
        for name, content in units.items():
            unit_path = target_dir / name
            atomic_write_text(unit_path, content)
            written.append(unit_path)

        if enable_now:
            self._run_systemctl(["systemctl", "--user", "daemon-reload"])
            for name in units:
                if name.endswith(".timer"):
                    self._run_systemctl(["systemctl", "--user", "enable", "--now", name])
            if "alfred-mcp.service" in units:
                self._run_systemctl(
                    ["systemctl", "--user", "enable", "--now", "alfred-mcp.service"]
                )
                self._run_systemctl(["systemctl", "--user", "restart", "alfred-mcp.service"])
        return written

    def _prune_stale_systemd_units(self, target_dir: Path, enable_now: bool) -> None:
        """Disable and unlink legacy V7 routine units and disabled optional feature units."""
        for disable_unit, filenames in compute_stale_systemd_specs(self._settings):
            if any(
                (target_dir / fname).exists() or (target_dir / fname).is_symlink()
                for fname in filenames
            ):
                if enable_now:
                    self._run_systemctl(["systemctl", "--user", "disable", "--now", disable_unit])
                for fname in filenames:
                    (target_dir / fname).unlink(missing_ok=True)

    def get_status(self, systemd_user_dir: Path | None = None) -> RoutinesStatusReport:
        """Build a live status report of bookmarks, enabled features, extensions, and timers.

        Args:
            systemd_user_dir: Optional directory override for systemd --user unit inspection.

        Returns:
            Populated RoutinesStatusReport value object.
        """
        unit_dir = systemd_user_dir or (Path.home() / ".config" / "systemd" / "user")
        bookmarks = self._store.bookmarks.load()
        drive_token = (
            (self._store.drive_index.load().start_page_token or "").strip()
            or (bookmarks.drive_page_token or "").strip()
            or (bookmarks.drive_watcher_page_token or "").strip()
        )
        units_to_check = list(ALL_SYSTEMD_TIMERS)
        if self._settings.features.drive_watcher:
            units_to_check.append("alfred-drive.timer")
        if self._settings.features.mcp_gemini_bridge:
            units_to_check.append("alfred-mcp.service")
        installed = [u for u in units_to_check if (unit_dir / u).is_file()]
        active_map: dict[str, bool] = {}
        for unit_name in units_to_check:
            if unit_name == "alfred-inbox.timer" and not self._settings.features.inbox_auto_triage:
                active_map[unit_name] = False
                continue
            res = self._run_systemctl(["systemctl", "--user", "is-active", unit_name])
            active_map[unit_name] = bool(
                res is not None and res.returncode == 0 and res.stdout.strip() == "active"
            )
        r_state = self._store.routines.load()
        active_exts = [ext.extension_id for ext in self._discover_all_extensions()]
        return RoutinesStatusReport(
            gmail_history_id=bookmarks.gmail_history_id,
            gmail_last_polled_at=bookmarks.gmail_last_polled_at,
            drive_page_token_initialized=bool(drive_token),
            drive_last_synced_at=bookmarks.drive_last_synced_at,
            enabled_features=self._settings.features.enabled_feature_names(),
            active_extensions=active_exts,
            systemd_timers_installed=installed,
            systemd_timers_active=active_map,
            last_runs=r_state.last_runs,
            completed_periods=r_state.completed_periods,
        )

    def _run_guarded(self, job_name: str, fn: Callable[[], RoutineJobRun]) -> RoutineJobRun:
        """Execute a single routine job and isolate unexpected exceptions into ERROR status."""
        try:
            return fn()
        except Exception as exc:
            return RoutineJobRun(job_name=job_name, status="ERROR", details=str(exc))

    def _job_inbox_poll(self) -> RoutineJobRun:
        """Poll incremental Gmail history when inbox_auto_triage is enabled."""
        if not self._settings.features.inbox_auto_triage:
            return RoutineJobRun(
                job_name="inbox_poll",
                status="SKIPPED_DISABLED",
                details="features.inbox_auto_triage is disabled.",
            )
        cfg = self._settings.routines
        decisions = self._inbox.poll_new_messages(
            spawn_live_agy=cfg.spawn_agy,
            use_cognitive_agy=cfg.cognitive_agy,
        )
        status = "OK" if decisions else "NO_CHANGES"
        summary = ", ".join(f"{d.message_id}:{d.output_type}" for d in decisions)
        return RoutineJobRun(
            job_name="inbox_poll",
            status=status,
            items_processed=len(decisions),
            details=summary or "0 new incoming emails",
        )

    def _job_drive_watcher(self) -> RoutineJobRun:
        """Poll incremental Google Drive changes with 2-speed governance when enabled."""
        if not self._settings.features.drive_watcher:
            return RoutineJobRun(
                job_name="drive_watcher",
                status="SKIPPED_DISABLED",
                details="features.drive_watcher is disabled.",
            )
        watcher = self._drive_watcher or self._build_fallback_watcher()
        cfg = self._settings.routines
        decisions = watcher.poll_and_process(
            spawn_live_agy=cfg.spawn_agy,
            use_cognitive_agy=cfg.cognitive_agy,
        )
        status = "OK" if decisions else "NO_CHANGES"
        summary = ", ".join(f"{d.batch_id}:{d.output_type}" for d in decisions)
        return RoutineJobRun(
            job_name="drive_watcher",
            status=status,
            items_processed=len(decisions),
            details=summary or "0 eligible Drive changes",
        )

    def _build_fallback_watcher(self) -> DriveWatcherService:
        """Construct a lazy DriveWatcherService when not explicitly injected."""
        agy = self._agy or AgySessionManager(self._paths, self._settings, self._store)
        gov = DriveGovernanceService(self._settings, self._store, self._drive)
        know = KnowledgeService(
            self._paths, self._settings, self._store, backup_fn=self._drive.backup_markdown_file
        )
        return DriveWatcherService(
            self._paths, self._settings, self._store, self._drive, gov, know, agy
        )

    def _job_drive_delta_sync(self) -> RoutineJobRun:
        """Run incremental 0-token Google Drive metadata delta sync when enabled and initialized."""
        if not self._settings.features.drive_knowledge_sync:
            return RoutineJobRun(
                job_name="drive_delta_sync",
                status="SKIPPED_DISABLED",
                details="features.drive_knowledge_sync is disabled.",
            )
        if self._settings.features.drive_watcher:
            return RoutineJobRun(
                job_name="drive_delta_sync",
                status="SKIPPED_DELEGATED_TO_WATCHER",
                details="Delegated to 2-speed drive_watcher (features.drive_watcher is true).",
            )
        bm = self._store.bookmarks.load()
        token = (
            (self._store.drive_index.load().start_page_token or "").strip()
            or (bm.drive_page_token or "").strip()
            or (bm.drive_watcher_page_token or "").strip()
        )
        if not token:
            return RoutineJobRun(
                job_name="drive_delta_sync",
                status="SKIPPED_UNINITIALIZED",
                details="Drive start_page_token absent; run 'alfred init-drive' first.",
            )
        idx = self._drive.sync_changes()
        return RoutineJobRun(
            job_name="drive_delta_sync",
            status="OK",
            items_processed=idx.file_count,
            details=f"start_page_token={idx.start_page_token}",
        )

    def _job_instance_extensions(self, ref_day: date) -> RoutineJobRun:
        """Execute periodic checks across all active personal instance extensions."""
        today_iso = ref_day.isoformat()
        loader = ExtensionLoaderService(self._paths, self._settings)
        discovered_by_loader = loader.discover_active_extensions()
        seen_ids = {ext.extension_id for ext in discovered_by_loader}
        outcomes: list[ExtensionJobOutcome] = list(loader.run_all_periodic(today_iso))
        for ext in self._discover_class_extensions(seen_ids):
            ext_cfg = self._settings.extensions_config.get(ext.extension_id, {})
            errors = ext.validate_config(ext_cfg)
            if errors:
                outcomes.append(
                    ExtensionJobOutcome(
                        extension_id=ext.extension_id,
                        job_name="config_validation",
                        status="INVALID_CONFIG",
                        summary="; ".join(errors),
                    )
                )
                continue
            ctx = ExtensionContext(
                today_iso=today_iso,
                timezone=self._settings.timezone,
                extension_settings=ext_cfg,
            )
            outcomes.extend(ext.run_periodic(ctx))
        status = "OK" if outcomes else "NO_CHANGES"
        details = ", ".join(f"{o.extension_id}:{o.status}" for o in outcomes)
        return RoutineJobRun(
            job_name="instance_extensions",
            status=status,
            items_processed=len(outcomes),
            details=details or "0 extension outcomes",
        )

    def _job_instance_drive_backup(self) -> RoutineJobRun:
        """Back up gitignored instance/config/settings.yaml and active extensions to Drive."""
        if (
            not self._settings.features.drive_knowledge_sync
            or not self._settings.routines.backup_instance_config_to_drive
        ):
            return RoutineJobRun(
                job_name="instance_drive_backup",
                status="SKIPPED_DISABLED",
                details="Instance Drive backup is disabled.",
            )
        loader = ExtensionLoaderService(
            self._paths,
            self._settings,
            backup_fn=self._drive.backup_markdown_file,
        )
        uploaded = loader.backup_private_instance_files()
        status = "OK" if uploaded else "NO_CHANGES"
        return RoutineJobRun(
            job_name="instance_drive_backup",
            status=status,
            items_processed=len(uploaded),
            details=f"{len(uploaded)} file(s) backed up",
        )

    def _job_agy_ui_sync(self) -> RoutineJobRun:
        """Reconcile any pending CLI conversations into antigravity-cli-daemon when idle."""
        if self._agy is None:
            return RoutineJobRun(job_name="agy_ui_sync", status="NO_CHANGES")
        sync_res = self._agy.daemon_sync.sync_if_needed(runner_override=self._systemctl_runner)
        mapped = "OK" if sync_res.status == DaemonSyncStatus.SYNCED else sync_res.status.value
        return RoutineJobRun(
            job_name="agy_ui_sync",
            status=mapped,
            items_processed=len(sync_res.synced_ids),
            details=sync_res.details,
        )

    def _discover_all_extensions(self) -> list[AlfredExtension]:
        """Discover active extensions via build_extension() or compliant class definitions."""
        loader = ExtensionLoaderService(self._paths, self._settings)
        primary = loader.discover_active_extensions()
        seen_ids = {ext.extension_id for ext in primary}
        return [*primary, *self._discover_class_extensions(seen_ids)]

    def _discover_class_extensions(self, skip_ids: set[str]) -> list[AlfredExtension]:
        """Instantiate compliant zero-arg AlfredExtension classes when build_extension is absent."""
        ext_dir = self._paths.extensions_dir
        if not ext_dir.is_dir():
            return []
        found: list[AlfredExtension] = []
        for py_file in sorted(ext_dir.glob("*.py")):
            if py_file.name.startswith("_") or py_file.name.endswith(".example.py"):
                continue
            ext = self._instantiate_module_extension_class(py_file)
            if ext is not None and ext.extension_id not in skip_ids:
                skip_ids.add(ext.extension_id)
                found.append(ext)
        return found

    def _instantiate_module_extension_class(self, py_file: Path) -> AlfredExtension | None:
        """Import py_file and instantiate a zero-arg class satisfying AlfredExtension."""
        mod_name = f"alfred_ext_cls_{py_file.stem.replace('.', '_')}"
        spec = importlib.util.spec_from_file_location(mod_name, py_file)
        if spec is None or spec.loader is None:
            return None
        module: ModuleType = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if callable(getattr(module, "build_extension", None)):
            return None
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            try:
                inst = obj()
            except TypeError:
                continue
            if isinstance(inst, AlfredExtension):
                return inst
        return None

    def _record_job_runs(self, jobs: list[RoutineJobRun]) -> None:
        """Record latest execution telemetry per job in RoutineExecutionState."""
        by_name = {j.job_name: j for j in jobs}
        self._store.routines.update(
            lambda st: st.model_copy(update={"last_runs": {**st.last_runs, **by_name}})
        )

    def _run_systemctl(self, cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
        """Invoke systemctl --user via injected override or subprocess."""
        try:
            if self._systemctl_runner is not None:
                return self._systemctl_runner(cmd)
            return subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
