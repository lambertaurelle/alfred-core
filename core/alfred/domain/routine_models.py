"""Domain models for Alfred periodic routines, systemd timers, and workspace status."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


def _utc_now_iso() -> str:
    """Return the current UTC timestamp formatted as an ISO-8601 string."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class RoutinesConfig(BaseModel):
    """Configuration schema for Alfred's periodic inbox, Drive, and calendar routines."""

    model_config = ConfigDict(frozen=True)

    inbox_interval_sec: int = Field(default=120, ge=30)
    periodic_interval_sec: int = Field(default=1800, ge=60)
    spawn_agy: bool = True
    cognitive_agy: bool = True
    vacation_lookahead_days: int = Field(default=45, ge=7)

    @classmethod
    def from_dict(cls, raw: object) -> RoutinesConfig:
        """Build a validated RoutinesConfig from a YAML mapping or return defaults.

        Args:
            raw: Optional mapping extracted from settings YAML under 'routines'.

        Returns:
            Validated RoutinesConfig value object.
        """
        if not isinstance(raw, dict):
            return cls()
        return cls(
            inbox_interval_sec=int(raw.get("inbox_interval_sec", 120)),
            periodic_interval_sec=int(raw.get("periodic_interval_sec", 1800)),
            spawn_agy=bool(raw.get("spawn_agy", True)),
            cognitive_agy=bool(raw.get("cognitive_agy", True)),
            vacation_lookahead_days=int(raw.get("vacation_lookahead_days", 45)),
        )


class RoutineJobRun(BaseModel):
    """Single routine job execution outcome record."""

    job_name: str
    status: str
    ran_at: str = Field(default_factory=_utc_now_iso)
    details: str = ""
    items_processed: int = 0


class RoutineExecutionState(BaseModel):
    """Persistent idempotency ledger and last-run telemetry for periodic routines."""

    completed_periods: dict[str, str] = Field(default_factory=dict)
    last_runs: dict[str, RoutineJobRun] = Field(default_factory=dict)


class RoutineTickReport(BaseModel):
    """Aggregated report returned by a single alfred run-routines invocation."""

    scope: str
    triggered_at: str = Field(default_factory=_utc_now_iso)
    locked_skip: bool = False
    jobs: list[RoutineJobRun] = Field(default_factory=list)


class RoutinesStatusReport(BaseModel):
    """Live diagnostic snapshot of bookmarks, contract readiness, and systemd timers."""

    gmail_history_id: str | None = None
    gmail_last_polled_at: str = ""
    drive_page_token_initialized: bool = False
    drive_last_synced_at: str = ""
    nounou_contract_initialized: bool = False
    systemd_timers_installed: list[str] = Field(default_factory=list)
    systemd_timers_active: dict[str, bool] = Field(default_factory=dict)
    last_runs: dict[str, RoutineJobRun] = Field(default_factory=dict)
    completed_periods: dict[str, str] = Field(default_factory=dict)


class WorkspaceProjectStatus(BaseModel):
    """Provisioned Antigravity project descriptor for one Alfred workspace."""

    role: str
    project_name: str
    project_id: str = ""
    workspace_dir: str
    agents_discovered: list[str] = Field(default_factory=list)
    created: bool = False
