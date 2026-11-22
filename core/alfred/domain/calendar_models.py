"""Domain models for generic multi-calendar HOLDs, FreeBusy windows, invitations, and PRs."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


def _utc_now_iso() -> str:
    """Return the current UTC timestamp formatted as an ISO-8601 string."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class CalendarConfig(BaseModel):
    """Structural configuration for Google Calendar HOLD allowlist behavior."""

    model_config = ConfigDict(frozen=True)

    include_authenticated_owners_in_hold_allowlist: bool = False

    @classmethod
    def from_dict(cls, raw: object) -> CalendarConfig:
        """Build a validated CalendarConfig from a YAML mapping or return defaults.

        Args:
            raw: Optional mapping extracted from settings YAML under 'calendar'.

        Returns:
            Validated CalendarConfig value object.
        """
        if not isinstance(raw, dict):
            return cls()
        return cls(
            include_authenticated_owners_in_hold_allowlist=bool(
                raw.get("include_authenticated_owners_in_hold_allowlist", False)
            )
        )


class HoldAllowlistState(BaseModel):
    """Persistent list of email addresses allowed to receive immediate [HOLD] invites."""

    emails: list[str] = Field(default_factory=list)


class CalendarListEntry(BaseModel):
    """Normalized Google Calendar list entry returned by calendarList.list."""

    model_config = ConfigDict(frozen=True)

    calendar_id: str
    summary: str
    primary: bool = False
    access_role: str = ""
    time_zone: str = ""


class FreeBusyInterval(BaseModel):
    """Busy time window returned by Google Calendar freebusy.query."""

    model_config = ConfigDict(frozen=True)

    calendar_id: str
    start_iso: str
    end_iso: str


class CalendarActionIntent(StrEnum):
    """Calendar scheduling intent extracted during cognitive email triage."""

    NONE = "NONE"
    CREATE_HOLD = "CREATE_HOLD"
    CONFIRM_EVENT = "CONFIRM_EVENT"


class CalendarSlotProposal(BaseModel):
    """Proposed or confirmed calendar slot with candidate/invited attendees."""

    summary: str
    start_iso: str
    end_iso: str
    attendees: list[str] = Field(default_factory=list)
    send_invites: bool = True
    location: str = ""
    description: str = ""
    recurrence: list[str] = Field(default_factory=list)


class CalendarTriageAction(BaseModel):
    """Structured calendar scheduling decision attached to an email triage pass."""

    intent: CalendarActionIntent = CalendarActionIntent.NONE
    slots: list[CalendarSlotProposal] = Field(default_factory=list)


class PullRequestArtifact(BaseModel):
    """Generated Pull Request descriptor for the Alfred mono-repo."""

    repo_name: str
    branch_name: str
    base_branch: str
    pr_url_or_ref: str
    status: str
    changed_files: list[str] = Field(default_factory=list)


class EvolutionProposalRecord(BaseModel):
    """Evolution lifecycle record persisted in evolution_proposals_ledger.json."""

    proposal_id: str
    title: str
    description: str
    status: str = "PENDING_GATE"
    mode: str = "upstream-pr"
    is_breaking: bool = False
    worktree_path: str = ""
    pull_requests: list[PullRequestArtifact] = Field(default_factory=list)
    created_at: str = Field(default_factory=_utc_now_iso)


class EvolutionProposalsLedger(BaseModel):
    """Persistent ledger of system evolution proposals and mono-repo PRs."""

    proposals: dict[str, EvolutionProposalRecord] = Field(default_factory=dict)
