"""Standardized protocol for user-specific instance/extensions plugins enforced by alfred-evolve."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class ExtensionContext(BaseModel):
    """Immutable execution context passed to personal instance extensions during periodic ticks."""

    model_config = ConfigDict(frozen=True)

    today_iso: str
    timezone: str = "Europe/Paris"
    extension_settings: dict[str, object] = Field(default_factory=dict)


class ExtensionTaskDraft(BaseModel):
    """Task creation request emitted by a personal extension for alfred-tasks."""

    model_config = ConfigDict(frozen=True)

    title: str
    notes: str = ""
    due_date_iso: str = ""


class ExtensionJobOutcome(BaseModel):
    """Structured result returned by an AlfredExtension periodic check."""

    model_config = ConfigDict(frozen=True)

    extension_id: str
    job_name: str
    status: str = "NO_CHANGES"
    summary: str = ""
    proposed_gate_markdown: str = ""
    tasks_to_create: list[ExtensionTaskDraft] = Field(default_factory=list)


@runtime_checkable
class AlfredExtension(Protocol):
    """Mandatory structural contract implemented by every plugin in instance/extensions/."""

    @property
    def extension_id(self) -> str:
        """Return unique snake_case identifier for this personal extension."""
        ...

    @property
    def description(self) -> str:
        """Return concise one-line summary of the extension's purpose."""
        ...

    def validate_config(self, raw_config: dict[str, object]) -> list[str]:
        """Validate extension-specific settings and return a list of error strings (empty if valid).

        Args:
            raw_config: Dictionary loaded from settings.yaml under extensions.<extension_id>.

        Returns:
            List of validation error messages, or empty list when valid.
        """
        ...

    def run_periodic(self, context: ExtensionContext) -> list[ExtensionJobOutcome]:
        """Execute deterministic periodic checks for the given reference date.

        Args:
            context: Immutable ExtensionContext with today_iso and extension_settings.

        Returns:
            Ordered list of ExtensionJobOutcome items.
        """
        ...
