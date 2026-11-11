"""Unified periodic routine runner and systemd --user timer installer (0-token idle)."""

from __future__ import annotations

import calendar as pycal
import subprocess
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    CalendarEventItem,
    VacationPeriod,
    fold_accents,
    utc_now_iso,
)
from alfred.domain.routine_models import (
    RoutineJobRun,
    RoutinesStatusReport,
    RoutineTickReport,
)
from alfred.repositories.json_repository import atomic_write_text
from alfred.repositories.state_store import StateStore
from alfred.services.agy_session_manager import AgySessionManager
from alfred.services.calendar_service import CalendarService
from alfred.services.drive_service import DriveService
from alfred.services.inbox_triage_service import InboxTriageService
from alfred.services.nounou_service import NounouService
from alfred.services.pajemploi_calculator import PajemploiCalculator, is_pajemploi_closure_day

SYSTEMD_TIMER_NAMES: tuple[str, ...] = ("alfred-inbox.timer", "alfred-routines.timer")
VACATION_KEYWORDS: tuple[str, ...] = ("vacances", "conges")


def _extract_vacation_dates(ev: CalendarEventItem) -> tuple[str, str]:
    """Convert a CalendarEventItem into inclusive ISO (start_date, end_date) strings."""
    start_d = date.fromisoformat(ev.start_iso[:10])
    raw_end = date.fromisoformat(ev.end_iso[:10])
    is_all_day = "T" not in ev.start_iso and "T" not in ev.end_iso
    end_d = max(start_d, raw_end - timedelta(days=1)) if is_all_day else max(start_d, raw_end)
    return (start_d.isoformat(), end_d.isoformat())


class RoutineService:
    """Orchestrate 0-token periodic Gmail, Drive, and Garde Nounou routines + systemd timers."""

    def __init__(
        self,
        paths: AlfredPaths,
        settings: AlfredSettings,
        state_store: StateStore,
        inbox_service: InboxTriageService,
        drive_service: DriveService,
        nounou_service: NounouService,
        pajemploi_calculator: PajemploiCalculator,
        calendar_service: CalendarService,
        agy_manager: AgySessionManager | None = None,
        systemctl_runner: Callable[[list[str]], subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        """Initialize the periodic routine orchestrator.

        Args:
            paths: Resolved AlfredPaths layout.
            settings: Structural runtime configuration including routines block.
            state_store: Local atomic state facade.
            inbox_service: Gmail incremental triage service.
            drive_service: Google Drive incremental delta sync service.
            nounou_service: Childcare M+1 seeding and vacation notice service.
            pajemploi_calculator: Deterministic monthly Pajemploi calculator.
            calendar_service: Google Calendar v3 adapter.
            agy_manager: Optional Antigravity Web session manager for live Gate spawning.
            systemctl_runner: Optional subprocess override for systemctl --user invocations.
        """
        self._paths = paths
        self._settings = settings
        self._store = state_store
        self._inbox = inbox_service
        self._drive = drive_service
        self._nounou = nounou_service
        self._pajemploi = pajemploi_calculator
        self._calendar = calendar_service
        self._agy = agy_manager
        self._systemctl_runner = systemctl_runner

    def run_routines(
        self,
        scope: str = "all",
        today: date | None = None,
    ) -> RoutineTickReport:
        """Execute inbox and/or periodic routines under a non-blocking flock.

        Args:
            scope: Execution scope ('all', 'inbox', or 'periodic').
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
            if scope in {"all", "periodic"}:
                jobs.append(self._run_guarded("drive_delta_sync", self._job_drive_delta_sync))
                jobs.append(
                    self._run_guarded(
                        "nounou_monthly_seed", lambda: self._job_nounou_seed(ref_day)
                    )
                )
                jobs.append(
                    self._run_guarded(
                        "pajemploi_monthly_close", lambda: self._job_pajemploi_close(ref_day)
                    )
                )
                jobs.append(
                    self._run_guarded(
                        "nounou_vacation_notice", lambda: self._job_vacation_notice(ref_day)
                    )
                )
            self._record_job_runs(jobs)
            return RoutineTickReport(scope=scope, jobs=jobs)

    def render_systemd_units(self) -> dict[str, str]:
        """Render the 4 systemd --user unit files (alfred-inbox & alfred-routines).

        Returns:
            Mapping from unit file name to UTF-8 systemd unit text.
        """
        cfg = self._settings.routines
        alfred_bin = self._paths.root_dir / ".venv" / "bin" / "alfred"
        local_bin = Path.home() / ".local" / "bin"
        env_path = f"{local_bin}:/usr/local/bin:/usr/bin:/bin"
        common_svc = (
            f"Type=oneshot\n"
            f"WorkingDirectory={self._paths.root_dir}\n"
            f'Environment="ALFRED_ROOT_DIR={self._paths.root_dir}"\n'
            f'Environment="PATH={env_path}"\n'
        )
        return {
            "alfred-inbox.service": (
                "[Unit]\nDescription=Alfred V7.1 Lean — Incremental Gmail Inbox Routine\n\n"
                f"[Service]\n{common_svc}"
                f"ExecStart={alfred_bin} run-routines --only inbox\n"
            ),
            "alfred-inbox.timer": (
                "[Unit]\nDescription=Alfred V7.1 Lean — Poll Gmail Inbox Every 2 Minutes\n\n"
                f"[Timer]\nOnBootSec=45s\nOnUnitActiveSec={cfg.inbox_interval_sec}s\n"
                "Persistent=true\nUnit=alfred-inbox.service\n\n"
                "[Install]\nWantedBy=timers.target\n"
            ),
            "alfred-routines.service": (
                "[Unit]\nDescription=Alfred V7.1 Lean — Periodic Drive & Calendar Routines\n\n"
                f"[Service]\n{common_svc}"
                f"ExecStart={alfred_bin} run-routines --only periodic\n"
            ),
            "alfred-routines.timer": (
                "[Unit]\nDescription=Alfred V7.1 Lean — Periodic Drive & Calendar Timer\n\n"
                f"[Timer]\nOnBootSec=90s\nOnUnitActiveSec={cfg.periodic_interval_sec}s\n"
                "Persistent=true\nUnit=alfred-routines.service\n\n"
                "[Install]\nWantedBy=timers.target\n"
            ),
        }

    def install_systemd_timers(
        self,
        systemd_user_dir: Path | None = None,
        enable_now: bool = True,
    ) -> list[Path]:
        """Write systemd --user units and optionally daemon-reload + enable --now timers.

        Args:
            systemd_user_dir: Optional target directory (defaults to ~/.config/systemd/user).
            enable_now: When True, invoke systemctl --user daemon-reload and enable --now.

        Returns:
            Ordered list of written unit file Paths.
        """
        target_dir = systemd_user_dir or (Path.home() / ".config" / "systemd" / "user")
        target_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for name, content in self.render_systemd_units().items():
            unit_path = target_dir / name
            atomic_write_text(unit_path, content)
            written.append(unit_path)
        if enable_now:
            self._run_systemctl(["systemctl", "--user", "daemon-reload"])
            for timer_name in SYSTEMD_TIMER_NAMES:
                self._run_systemctl(["systemctl", "--user", "enable", "--now", timer_name])
        return written

    def get_status(self, systemd_user_dir: Path | None = None) -> RoutinesStatusReport:
        """Build a live status report of bookmarks, contract readiness, and systemd timers.

        Args:
            systemd_user_dir: Optional directory override for systemd --user unit inspection.

        Returns:
            Populated RoutinesStatusReport value object.
        """
        unit_dir = systemd_user_dir or (Path.home() / ".config" / "systemd" / "user")
        bookmarks = self._store.bookmarks.load()
        drive_token = self._store.drive_index.load().start_page_token or (
            bookmarks.drive_page_token or ""
        )
        installed = [t for t in SYSTEMD_TIMER_NAMES if (unit_dir / t).is_file()]
        active_map: dict[str, bool] = {}
        for timer_name in SYSTEMD_TIMER_NAMES:
            res = self._run_systemctl(["systemctl", "--user", "is-active", timer_name])
            active_map[timer_name] = bool(
                res is not None and res.returncode == 0 and res.stdout.strip() == "active"
            )
        r_state = self._store.routines.load()
        return RoutinesStatusReport(
            gmail_history_id=bookmarks.gmail_history_id,
            gmail_last_polled_at=bookmarks.gmail_last_polled_at,
            drive_page_token_initialized=bool(drive_token),
            drive_last_synced_at=bookmarks.drive_last_synced_at,
            nounou_contract_initialized=self._store.nounou_contract.exists(),
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
        """Poll incremental Gmail history and route any new messages."""
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

    def _job_drive_delta_sync(self) -> RoutineJobRun:
        """Run incremental 0-token Google Drive metadata delta sync when initialized."""
        token = self._store.drive_index.load().start_page_token or (
            self._store.bookmarks.load().drive_page_token or ""
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

    def _job_nounou_seed(self, ref_day: date) -> RoutineJobRun:
        """Trigger M+1 childcare schedule Gate once on the 20th of month M."""
        if not self._store.nounou_contract.exists():
            return RoutineJobRun(
                job_name="nounou_monthly_seed",
                status="SKIPPED_NO_CONTRACT",
                details="nounou_contract.json not initialized yet.",
            )
        if not self._nounou.should_run_monthly_seed(ref_day):
            return RoutineJobRun(
                job_name="nounou_monthly_seed",
                status="NO_CHANGES",
                details=f"{ref_day.isoformat()} is not day 20.",
            )
        period_key = f"nounou_seed:{ref_day.year:04d}-{ref_day.month:02d}"
        if period_key in self._store.routines.load().completed_periods:
            return RoutineJobRun(
                job_name="nounou_monthly_seed",
                status="ALREADY_COMPLETED",
                details=period_key,
            )
        events, plan_md = self._nounou.seed_next_month_schedule(ref_day, apply_to_calendar=False)
        self._spawn_calendar_gate(period_key, plan_md)
        self._mark_completed(period_key)
        return RoutineJobRun(
            job_name="nounou_monthly_seed",
            status="OK",
            items_processed=len(events),
            details=period_key,
        )

    def _job_pajemploi_close(self, ref_day: date) -> RoutineJobRun:
        """Trigger deterministic Pajemploi monthly closure Gate once at last_day - 3."""
        if not self._store.nounou_contract.exists():
            return RoutineJobRun(
                job_name="pajemploi_monthly_close",
                status="SKIPPED_NO_CONTRACT",
                details="nounou_contract.json not initialized yet.",
            )
        if not is_pajemploi_closure_day(ref_day):
            return RoutineJobRun(
                job_name="pajemploi_monthly_close",
                status="NO_CHANGES",
                details=f"{ref_day.isoformat()} is not last_day - 3.",
            )
        period_key = f"pajemploi_close:{ref_day.year:04d}-{ref_day.month:02d}"
        if period_key in self._store.routines.load().completed_periods:
            return RoutineJobRun(
                job_name="pajemploi_monthly_close",
                status="ALREADY_COMPLETED",
                details=period_key,
            )
        contract = self._store.nounou_contract.load()
        _, last_day = pycal.monthrange(ref_day.year, ref_day.month)
        tz = ZoneInfo(self._settings.timezone)
        t_min = datetime(ref_day.year, ref_day.month, 1, 0, 0, 0, tzinfo=tz).isoformat()
        t_max = datetime(ref_day.year, ref_day.month, last_day, 23, 59, 59, tzinfo=tz).isoformat()
        events = (
            self._calendar.list_events(contract.calendar_id, t_min, t_max)
            if contract.calendar_id
            else []
        )
        report = self._pajemploi.calculate_monthly_report(
            ref_day.year, ref_day.month, contract, events
        )
        self._spawn_calendar_gate(period_key, report.implementation_plan_markdown)
        self._mark_completed(period_key)
        return RoutineJobRun(
            job_name="pajemploi_monthly_close",
            status="OK",
            items_processed=1,
            details=period_key,
        )

    def _job_vacation_notice(self, ref_day: date) -> RoutineJobRun:
        """Inspect upcoming Garde Nounou vacation events and emit notice Gate alerts once."""
        if not self._store.nounou_contract.exists():
            return RoutineJobRun(
                job_name="nounou_vacation_notice",
                status="SKIPPED_NO_CONTRACT",
                details="nounou_contract.json not initialized yet.",
            )
        contract = self._store.nounou_contract.load()
        if not contract.calendar_id:
            return RoutineJobRun(job_name="nounou_vacation_notice", status="NO_CHANGES")
        tz = ZoneInfo(self._settings.timezone)
        end_day = ref_day + timedelta(days=self._settings.routines.vacation_lookahead_days)
        t_min = datetime(ref_day.year, ref_day.month, ref_day.day, 0, 0, 0, tzinfo=tz).isoformat()
        t_max = datetime(
            end_day.year, end_day.month, end_day.day, 23, 59, 59, tzinfo=tz
        ).isoformat()
        events = self._calendar.list_events(contract.calendar_id, t_min, t_max)
        vac_events = [
            ev
            for ev in events
            if any(kw in fold_accents(ev.summary) for kw in VACATION_KEYWORDS)
        ]
        if not vac_events:
            return RoutineJobRun(job_name="nounou_vacation_notice", status="NO_CHANGES")
        done = self._store.routines.load().completed_periods
        triggered = 0
        for ev in vac_events:
            key = f"vacation_notice:{ev.event_id or ev.start_iso}"
            if key in done:
                continue
            start_s, end_s = _extract_vacation_dates(ev)
            source = "nounou" if "nounou" in fold_accents(ev.summary) else "parents"
            alert = self._nounou.evaluate_vacation_notice(
                VacationPeriod(start_date=start_s, end_date=end_s, source=source, label=ev.summary),
                reference_date=ref_day,
                persist=False,
            )
            self._spawn_calendar_gate(key, alert.implementation_plan_markdown)
            self._mark_completed(key)
            triggered += 1
        if triggered == 0:
            return RoutineJobRun(
                job_name="nounou_vacation_notice",
                status="ALREADY_COMPLETED",
                details=f"{len(vac_events)} vacation event(s) already notified.",
            )
        return RoutineJobRun(
            job_name="nounou_vacation_notice",
            status="OK",
            items_processed=triggered,
            details=f"{triggered} vacation notice Gate(s) created.",
        )

    def _spawn_calendar_gate(self, source_key: str, plan_md: str) -> None:
        """Create a Gate session in alfred-calendar according to routines.spawn_agy."""
        if self._agy is not None:
            self._agy.create_session(
                workspace="alfred-calendar",
                source_message_id=source_key,
                implementation_plan_md=plan_md,
                spawn_live=self._settings.routines.spawn_agy,
            )

    def _mark_completed(self, period_key: str) -> None:
        """Persist an idempotency period_key in RoutineExecutionState."""
        stamp = utc_now_iso()
        self._store.routines.update(
            lambda st: st.model_copy(
                update={"completed_periods": {**st.completed_periods, period_key: stamp}}
            )
        )

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
