"""Fictional example extension #2: preventive home & equipment maintenance scheduler."""

from __future__ import annotations

from datetime import date

from alfred.domain.extension_protocol import (
    AlfredExtension,
    ExtensionContext,
    ExtensionJobOutcome,
    ExtensionTaskDraft,
)


class HomePreventiveMaintenanceExtension:
    """Create Google Tasks 30 days ahead of annual home & equipment maintenance deadlines."""

    @property
    def extension_id(self) -> str:
        """Return unique identifier for the preventive maintenance extension."""
        return "home_preventive_maintenance"

    @property
    def description(self) -> str:
        """Return one-line functional summary."""
        return "Schedule annual preventive maintenance 30 days ahead (boiler, roof, vehicle)."

    def validate_config(self, raw_config: dict[str, object]) -> list[str]:
        """Validate deadlines mapping in extensions.home_preventive_maintenance.

        Args:
            raw_config: Extension configuration mapping from settings.yaml.

        Returns:
            List of human-readable validation errors.
        """
        errors: list[str] = []
        items = raw_config.get("deadlines", [])
        if not isinstance(items, list):
            return ["deadlines must be a list of {label, due_date} mappings."]
        for entry in items:
            if not isinstance(entry, dict) or "label" not in entry or "due_date" not in entry:
                errors.append("Each deadline entry requires 'label' and ISO 'due_date'.")
                continue
            try:
                date.fromisoformat(str(entry["due_date"]))
            except ValueError:
                errors.append(f"Invalid ISO due_date: {entry['due_date']}")
        return errors

    def run_periodic(self, context: ExtensionContext) -> list[ExtensionJobOutcome]:
        """Emit reminder tasks for maintenance items whose due_date is exactly 30 days away.

        Args:
            context: Immutable ExtensionContext providing today_iso and extension_settings.

        Returns:
            List with one ExtensionJobOutcome containing any triggered maintenance tasks.
        """
        today = date.fromisoformat(context.today_iso)
        lead_days_raw = context.extension_settings.get("lead_days", 30)
        lead_days = int(lead_days_raw) if isinstance(lead_days_raw, int) else 30
        raw_items = context.extension_settings.get("deadlines", [])
        items = raw_items if isinstance(raw_items, list) else []
        tasks: list[ExtensionTaskDraft] = []
        for entry in items:
            if not isinstance(entry, dict):
                continue
            try:
                due_dt = date.fromisoformat(str(entry.get("due_date", "")))
            except ValueError:
                continue
            if (due_dt - today).days == lead_days:
                label = str(entry.get("label", "Annual maintenance"))
                tasks.append(
                    ExtensionTaskDraft(
                        title=f"[Maintenance D-{lead_days}] {label}",
                        notes=f"Target due date: {due_dt.isoformat()}",
                        due_date_iso=due_dt.isoformat(),
                    )
                )
        status = "OK" if tasks else "NO_CHANGES"
        return [
            ExtensionJobOutcome(
                extension_id=self.extension_id,
                job_name="preventive_maintenance_check",
                status=status,
                summary=f"{len(tasks)} preventive maintenance reminder(s) due.",
                tasks_to_create=tasks,
            )
        ]


def build_extension() -> AlfredExtension:
    """Instantiate the fictional home preventive maintenance extension.

    Returns:
        Configured HomePreventiveMaintenanceExtension instance.
    """
    return HomePreventiveMaintenanceExtension()
