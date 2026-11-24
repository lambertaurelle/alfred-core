"""Fictional example extension #1: monthly freelance consulting timesheet & billing reminder."""

from __future__ import annotations

import calendar
from datetime import date

from alfred.domain.extension_protocol import (
    AlfredExtension,
    ExtensionContext,
    ExtensionJobOutcome,
    ExtensionTaskDraft,
)


class FreelanceBillingTrackerExtension:
    """Trigger end-of-month client invoice preparation tasks for an independent consultant."""

    @property
    def extension_id(self) -> str:
        """Return unique identifier for the freelance billing tracker extension."""
        return "freelance_billing_tracker"

    @property
    def description(self) -> str:
        """Return one-line functional summary."""
        return "Generate client invoicing tasks two days before month-end from active engagements."

    def validate_config(self, raw_config: dict[str, object]) -> list[str]:
        """Validate daily_rate_eur and clients list in extensions.freelance_billing_tracker.

        Args:
            raw_config: Extension configuration mapping from settings.yaml.

        Returns:
            List of human-readable configuration error strings.
        """
        errors: list[str] = []
        rate = raw_config.get("default_daily_rate_eur", 650)
        if not isinstance(rate, (int, float)) or float(rate) <= 0:
            errors.append("default_daily_rate_eur must be a positive number.")
        clients = raw_config.get("active_clients", [])
        if not isinstance(clients, list):
            errors.append("active_clients must be a list of client code strings.")
        return errors

    def run_periodic(self, context: ExtensionContext) -> list[ExtensionJobOutcome]:
        """Emit billing tasks two days before month-end for each configured active client.

        Args:
            context: Immutable ExtensionContext providing today_iso and extension_settings.

        Returns:
            Single ExtensionJobOutcome (OK on D-2, NO_CHANGES on other days).
        """
        today = date.fromisoformat(context.today_iso)
        _, last_day = calendar.monthrange(today.year, today.month)
        if today.day != max(1, last_day - 2):
            return [
                ExtensionJobOutcome(
                    extension_id=self.extension_id,
                    job_name="monthly_invoice_prep",
                    status="NO_CHANGES",
                    summary=f"{context.today_iso} is not billing closure day ({last_day - 2}).",
                )
            ]
        raw_clients = context.extension_settings.get("active_clients", ["ACME_CORP"])
        clients = [str(c) for c in raw_clients] if isinstance(raw_clients, list) else ["ACME_CORP"]
        due_iso = date(today.year, today.month, last_day).isoformat()
        tasks = [
            ExtensionTaskDraft(
                title=f"Issue invoice {today.strftime('%Y-%m')} — {client}",
                notes="Verify [CLIENT] calendar slots and export the timesheet statement.",
                due_date_iso=due_iso,
            )
            for client in clients
        ]
        return [
            ExtensionJobOutcome(
                extension_id=self.extension_id,
                job_name="monthly_invoice_prep",
                status="OK",
                summary=f"Prepared {len(tasks)} invoice task(s) for {today.strftime('%Y-%m')}.",
                tasks_to_create=tasks,
            )
        ]


def build_extension() -> AlfredExtension:
    """Instantiate the fictional freelance billing tracker extension.

    Returns:
        Configured FreelanceBillingTrackerExtension instance.
    """
    return FreelanceBillingTrackerExtension()
