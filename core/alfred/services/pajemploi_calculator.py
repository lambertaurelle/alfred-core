"""Deterministic 0-token monthly Pajemploi calculator triggered at last_day - 3."""

from __future__ import annotations

import calendar
import math
from datetime import date, datetime, time, timedelta

from alfred.domain.models import CalendarEventItem, NounouContract, PajemploiReport, fold_accents
from alfred.services.nounou_service import compute_french_public_holidays


def pajemploi_closure_date(year: int, month: int) -> date:
    """Compute the monthly Pajemploi trigger date (last_day - 3, handling Feb 28/29 days).

    Args:
        year: Four-digit calendar year.
        month: Calendar month (1..12).

    Returns:
        Trigger date equal to last_day_of_month - 3 days.
    """
    _, last_day = calendar.monthrange(year, month)
    return date(year, month, last_day - 3)


def is_pajemploi_closure_day(today: date) -> bool:
    """Return True when today matches last_day - 3 of the current month.

    Args:
        today: Current calendar date.

    Returns:
        True if today is the Pajemploi monthly closure date.
    """
    return today == pajemploi_closure_date(today.year, today.month)


def _expand_inclusive_event_dates(ev: CalendarEventItem) -> list[date]:
    """Expand an event's date span inclusively, respecting RFC 5545 exclusive all-day ends."""
    is_all_day = "T" not in ev.start_iso and "T" not in ev.end_iso
    if is_all_day:
        start_d = date.fromisoformat(ev.start_iso[:10])
        raw_end_d = date.fromisoformat(ev.end_iso[:10])
        last_d = max(start_d, raw_end_d - timedelta(days=1))
    else:
        start_dt = datetime.fromisoformat(ev.start_iso)
        end_dt = datetime.fromisoformat(ev.end_iso)
        start_d = start_dt.date()
        if end_dt.date() > start_d and end_dt.time() == time(0, 0):
            last_d = end_dt.date() - timedelta(days=1)
        else:
            last_d = max(start_d, end_dt.date())
    days: list[date] = []
    cur = start_d
    while cur <= last_d:
        days.append(cur)
        cur += timedelta(days=1)
    return days


class PajemploiCalculator:
    """Pure deterministic calculator producing monthly Pajemploi figures and Gate plan."""

    def calculate_monthly_report(
        self,
        year: int,
        month: int,
        contract: NounouContract,
        calendar_events: list[CalendarEventItem],
    ) -> PajemploiReport:
        """Compute monthly Pajemploi declaration figures from Garde Nounou events.

        Args:
            year: Target declaration year.
            month: Target declaration month (1..12).
            contract: Active signed NounouContract.
            calendar_events: Events fetched from the Garde Nounou calendar for (year, month).

        Returns:
            Validated PajemploiReport including formatted implementation_plan_markdown.
        """
        sched_hours = {w.weekday.upper(): w.hours for w in contract.scheduled_workdays}
        expected_dates, theo_hours = self._compute_month_schedule(
            year, month, sched_hours, contract
        )
        worked_days, overtime_hours, meals_count, unpaid_hours, cp_days = (
            self._tally_month_by_date(year, month, calendar_events, sched_hours, expected_dates)
        )
        default_daily = next(iter(sched_hours.values()), 10.0)
        deduction = (
            round((contract.base_monthly_net_salary * unpaid_hours) / theo_hours, 2)
            if unpaid_hours > 0 and theo_hours > 0
            else 0.0
        )
        overtime_pay = round(
            overtime_hours * contract.hourly_rate_net * contract.overtime_multiplier, 2
        )
        net_salary = round(contract.base_monthly_net_salary - deduction + overtime_pay, 2)
        entretien = round(worked_days * contract.maintenance_allowance_daily, 2)
        repas = round(meals_count * contract.meal_allowance_daily, 2)
        total_virement = round(net_salary + entretien + repas, 2)
        deducted_equiv = (
            round(deduction / contract.hourly_rate_net) if contract.hourly_rate_net > 0 else 0
        )
        jours_abs = int(math.ceil(unpaid_hours / default_daily)) if unpaid_hours > 0 else 0
        jours_activite = max(0, contract.base_monthly_days - jours_abs)
        if net_salary < 0 or jours_activite > 31 or total_virement < net_salary:
            raise ValueError("Pajemploi sanity check failed on computed totals.")

        report = PajemploiReport(
            period_month=f"{year:04d}-{month:02d}",
            employee_name=contract.employee_name,
            heures_normales=max(0, contract.base_monthly_hours - deducted_equiv),
            heures_complementaires_majorees=int(round(overtime_hours)),
            jours_activite=jours_activite,
            jours_conges_payes=cp_days,
            salaire_net_total=net_salary,
            indemnites_entretien=entretien,
            indemnites_repas=repas,
            total_virement_eur=total_virement,
            theoretical_working_hours=theo_hours,
            actual_working_days=worked_days,
            absence_unpaid_hours=unpaid_hours,
            deduction_cour_cassation_eur=deduction,
            overtime_hours=overtime_hours,
        )
        return report.model_copy(update={"implementation_plan_markdown": self._render_plan(report)})

    def _compute_month_schedule(
        self,
        year: int,
        month: int,
        sched_hours: dict[str, float],
        contract: NounouContract,
    ) -> tuple[set[date], float]:
        """Compute expected active workdates (excluding paid holidays/vacations) and theo_hours."""
        holidays = compute_french_public_holidays(year)
        vac_ranges = [
            (date.fromisoformat(v.start_date), date.fromisoformat(v.end_date))
            for v in contract.vacations
        ]
        _, days_in_month = calendar.monthrange(year, month)
        expected_dates: set[date] = set()
        theo_hours = 0.0
        for day_num in range(1, days_in_month + 1):
            cur = date(year, month, day_num)
            weekday = cur.strftime("%A").upper()
            if weekday not in sched_hours:
                continue
            theo_hours += sched_hours[weekday]
            if cur not in holidays and not any(s <= cur <= e for s, e in vac_ranges):
                expected_dates.add(cur)
        return expected_dates, theo_hours

    def _tally_month_by_date(
        self,
        year: int,
        month: int,
        events: list[CalendarEventItem],
        sched_hours: dict[str, float],
        expected_dates: set[date],
    ) -> tuple[int, float, int, float, int]:
        """Aggregate worked hours per calendar date before comparing to daily expected hours."""
        worked_by_date: dict[date, float] = {}
        vacation_dates: set[date] = set()
        cp_dates: set[date] = set()
        no_meal_dates: set[date] = set()

        for ev in events:
            title = fold_accents(ev.summary.strip())
            if "vacances" in title or "conges" in title:
                for d in _expand_inclusive_event_dates(ev):
                    if d.year == year and d.month == month:
                        vacation_dates.add(d)
                        if "conges payes" in title:
                            cp_dates.add(d)
                continue
            if "absence" in title:
                continue
            start_dt = datetime.fromisoformat(ev.start_iso)
            end_dt = datetime.fromisoformat(ev.end_iso)
            ev_date = start_dt.date()
            if ev_date.year != year or ev_date.month != month:
                continue
            duration = max(0.0, (end_dt - start_dt).total_seconds() / 3600.0)
            worked_by_date[ev_date] = worked_by_date.get(ev_date, 0.0) + duration
            desc = fold_accents(ev.description)
            if "repas parents" in desc or "sans repas" in desc:
                no_meal_dates.add(ev_date)

        _, days_in_month = calendar.monthrange(year, month)
        overtime_hours = 0.0
        unpaid_hours = 0.0
        for day_num in range(1, days_in_month + 1):
            cur = date(year, month, day_num)
            is_expected = cur in expected_dates and cur not in vacation_dates
            expected_h = sched_hours[cur.strftime("%A").upper()] if is_expected else 0.0
            actual_h = worked_by_date.get(cur, 0.0)
            if actual_h > expected_h:
                overtime_hours += actual_h - expected_h
            elif actual_h < expected_h:
                unpaid_hours += expected_h - actual_h

        worked_dates = {d for d, hrs in worked_by_date.items() if hrs > 0.0}
        meals_count = sum(1 for d in worked_dates if d not in no_meal_dates)
        return len(worked_dates), overtime_hours, meals_count, unpaid_hours, len(cp_dates)

    def _render_plan(self, report: PajemploiReport) -> str:
        """Format the monthly Pajemploi declaration summary as an implementation_plan.md Gate."""
        return (
            f"# Bilan Mensuel Pajemploi (`{report.period_month}`)\n\n"
            f"- **Assistante Maternelle** : {report.employee_name}\n"
            f"- **Jours réels travaillés** : {report.actual_working_days} j "
            f"(potentiel théorique : {report.theoretical_working_hours:.1f} h)\n\n"
            f"## Valeurs à Déclarer sur Pajemploi\n\n"
            f"| Case Pajemploi | Valeur |\n"
            f"| :--- | :--- |\n"
            f"| Heures normales | `{report.heures_normales} h` |\n"
            f"| Heures complémentaires / majorées | "
            f"`{report.heures_complementaires_majorees} h` |\n"
            f"| Jours d'activité | `{report.jours_activite} j` |\n"
            f"| Jours de congés payés | `{report.jours_conges_payes} j` |\n"
            f"| Salaire net total | `{report.salaire_net_total:.2f} €` |\n"
            f"| Indemnités d'entretien | `{report.indemnites_entretien:.2f} €` |\n"
            f"| Indemnités de repas | `{report.indemnites_repas:.2f} €` |\n"
            f"| **Total virement à effectuer** | **`{report.total_virement_eur:.2f} €`** |\n"
        )
