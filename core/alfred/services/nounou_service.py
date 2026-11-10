"""alfred-calendar service for Garde Nounou contract initialization, 20th seeding, and vacations."""

from __future__ import annotations

import calendar
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from alfred.domain.models import (
    AlfredSettings,
    CalendarEventItem,
    FrontmatterSchema,
    NounouContract,
    VacationNoticeAlert,
    VacationPeriod,
)
from alfred.repositories.state_store import StateStore
from alfred.services.calendar_service import CalendarService
from alfred.services.knowledge_service import KnowledgeService


def _compute_easter_sunday(year: int) -> date:
    """Compute Gregorian Easter Sunday using the anonymous Meeus/Jones/Butcher algorithm."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = ((h + ell - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def compute_french_public_holidays(year: int) -> set[date]:
    """Compute the 11 statutory French public holidays for a given year.

    Args:
        year: Four-digit Gregorian calendar year.

    Returns:
        Set of statutory French holiday dates.
    """
    easter = _compute_easter_sunday(year)
    return {
        date(year, 1, 1),  # Jour de l'An
        easter + timedelta(days=1),  # Lundi de Pâques
        date(year, 5, 1),  # Fête du Travail
        date(year, 5, 8),  # Victoire 1945
        easter + timedelta(days=39),  # Ascension
        easter + timedelta(days=50),  # Lundi de Pentecôte
        date(year, 7, 14),  # Fête Nationale
        date(year, 8, 15),  # Assomption
        date(year, 11, 1),  # Toussaint
        date(year, 11, 11),  # Armistice 1918
        date(year, 12, 25),  # Noël
    }


class NounouService:
    """Manage Garde Nounou contract setup, M+1 seeding on the 20th, and vacation notices."""

    def __init__(
        self,
        state_store: StateStore,
        calendar_service: CalendarService,
        knowledge_service: KnowledgeService,
        settings: AlfredSettings,
    ) -> None:
        """Initialize the Nounou childcare service.

        Args:
            state_store: Local atomic state store.
            calendar_service: Google Calendar v3 connector.
            knowledge_service: 3-tier YAML frontmatter knowledge service.
            settings: Structural runtime settings.
        """
        self._state_store = state_store
        self._calendar = calendar_service
        self._knowledge = knowledge_service
        self._settings = settings

    def prepare_init_nounou_plan(self, contract: NounouContract) -> str:
        """Generate Stage-2 Gate implementation_plan.md from a signed Nounou contract.

        Args:
            contract: Parsed NounouContract entity.

        Returns:
            Markdown Gate plan for user review prior to execution.
        """
        schedule_lines = "\n".join(
            f"- **{w.weekday}** : `{w.start_time}` -> `{w.end_time}` ({w.hours:.1f} h)"
            for w in contract.scheduled_workdays
        )
        return (
            f"# Initialisation Contrat Assistante Maternelle (`init-nounou`)\n\n"
            f"- **Salariée** : {contract.employee_name}\n"
            f"- **Enfant** : {contract.child_name}\n"
            f"- **Calendrier Google à créer** : `{self._settings.nounou_calendar_name}`\n"
            f"- **Salaire net mensualisé** : {contract.base_monthly_net_salary:.2f} € "
            f"({contract.base_monthly_hours} h / {contract.base_monthly_days} j)\n"
            f"- **Taux horaire net** : {contract.hourly_rate_net:.2f} €\n"
            f"- **Indemnités** : Entretien {contract.maintenance_allowance_daily:.2f} €/j | "
            f"Repas {contract.meal_allowance_daily:.2f} €/j\n"
            f"- **Préavis congés** : {contract.vacation_notice_days} jours\n\n"
            f"## Planning Hebdomadaire Contractuel\n{schedule_lines}\n\n"
            f"## Actions Post-Validation (`Proceed`)\n"
            f"1. Créer/associer le calendrier Google `{self._settings.nounou_calendar_name}`.\n"
            f"2. Sauvegarder `nounou_contract.json` et la fiche `02_assistante_maternelle.md`.\n"
        )

    def execute_init_nounou(self, contract: NounouContract) -> NounouContract:
        """Execute Stage-3 initialization: create Garde Nounou calendar, state, and reference .md.

        Args:
            contract: Validated NounouContract to activate.

        Returns:
            Persisted NounouContract enriched with the Google Calendar ID.
        """
        cal_id = self._calendar.ensure_calendar(self._settings.nounou_calendar_name)
        enriched = contract.model_copy(update={"calendar_id": cal_id})
        self._state_store.nounou_contract.save(enriched)
        self._state_store.bookmarks.update(
            lambda b: b.model_copy(update={"nounou_calendar_id": cal_id})
        )
        self._sync_nounou_reference_markdown(enriched)
        return enriched

    def should_run_monthly_seed(self, today: date) -> bool:
        """Check whether today is the 20th of month M (triggering M+1 pre-fill).

        Args:
            today: Current calendar date.

        Returns:
            True exclusively on day 20 of the month.
        """
        return today.day == 20

    def seed_next_month_schedule(
        self,
        reference_date: date,
        apply_to_calendar: bool = False,
    ) -> tuple[list[CalendarEventItem], str]:
        """Build exact M+1 childcare events excluding French holidays and registered vacations.

        Args:
            reference_date: Date in month M (typically the 20th).
            apply_to_calendar: When True (post-Gate), insert events into Garde Nounou.

        Returns:
            Tuple of (planned CalendarEventItem list, implementation_plan.md string).
        """
        contract = self._state_store.nounou_contract.load()
        target_year = reference_date.year + 1 if reference_date.month == 12 else reference_date.year
        target_month = 1 if reference_date.month == 12 else reference_date.month + 1

        events = self._generate_month_events(target_year, target_month, contract)
        if apply_to_calendar and contract.calendar_id:
            _, last_day = calendar.monthrange(target_year, target_month)
            tz = ZoneInfo(self._settings.timezone)
            t_min = datetime(target_year, target_month, 1, 0, 0, 0, tzinfo=tz).isoformat()
            t_max = datetime(target_year, target_month, last_day, 23, 59, 59, tzinfo=tz).isoformat()
            existing = self._calendar.list_events(contract.calendar_id, t_min, t_max)
            existing_keys = {(e.summary, e.start_iso[:10]) for e in existing}
            for ev in events:
                if (ev.summary, ev.start_iso[:10]) not in existing_keys:
                    self._calendar.insert_event(contract.calendar_id, ev)

        plan_md = (
            f"# Pré-remplissage Planning `{self._settings.nounou_calendar_name}` "
            f"({target_year:04d}-{target_month:02d})\n\n"
            f"- **Créneaux contractuels générés** : {len(events)} jours\n"
            f"- **Jours fériés légaux et vacances déposées** : exclus automatiquement\n"
        )
        return events, plan_md

    def evaluate_vacation_notice(
        self,
        vacation: VacationPeriod,
        reference_date: date,
        persist: bool = False,
    ) -> VacationNoticeAlert:
        """Verify contractual ~1-month notice compliance for parent or nanny vacation dates.

        Args:
            vacation: Proposed or newly detected VacationPeriod.
            reference_date: Date on which the vacation notice is evaluated.
            persist: When True (post-Gate), register vacation in nounou_contract.json & .md.

        Returns:
            VacationNoticeAlert containing compliance status and implementation_plan.md.
        """
        contract = self._state_store.nounou_contract.load()
        start_dt = date.fromisoformat(vacation.start_date)
        end_dt = date.fromisoformat(vacation.end_date)
        check_dt = (
            date.fromisoformat(vacation.notified_at) if vacation.notified_at else reference_date
        )
        days_until = (start_dt - check_dt).days
        deadline = start_dt - timedelta(days=contract.vacation_notice_days)
        is_compliant = days_until >= contract.vacation_notice_days

        if persist:
            self._persist_vacation_and_prune_shifts(contract, vacation, check_dt, start_dt, end_dt)

        header = (
            "Préavis Contractuel Respecté"
            if is_compliant
            else "ALERTE PRÉAVIS CONTRACTUEL INSUFFISANT"
        )
        plan_md = (
            f"# {header} — Congés `{self._settings.nounou_calendar_name}`\n\n"
            f"- **Origine** : `{vacation.source}` ({vacation.label})\n"
            f"- **Période** : du `{vacation.start_date}` au `{vacation.end_date}`\n"
            f"- **Préavis observé** : {days_until} j "
            f"(requis : {contract.vacation_notice_days} j, "
            f"date limite : `{deadline.isoformat()}`)\n"
        )
        return VacationNoticeAlert(
            vacation=vacation,
            days_until_start=days_until,
            required_notice_days=contract.vacation_notice_days,
            is_compliant=is_compliant,
            notice_deadline_date=deadline.isoformat(),
            implementation_plan_markdown=plan_md,
        )

    def _persist_vacation_and_prune_shifts(
        self,
        contract: NounouContract,
        vacation: VacationPeriod,
        check_dt: date,
        start_dt: date,
        end_dt: date,
    ) -> None:
        """Idempotently persist vacation and prune conflicting Garde shifts on those dates."""
        stamped = vacation.model_copy(update={"notified_at": check_dt.isoformat()})
        existing_keys = {(v.start_date, v.end_date, v.source) for v in contract.vacations}
        key = (stamped.start_date, stamped.end_date, stamped.source)
        vacs = (
            list(contract.vacations) if key in existing_keys else [*contract.vacations, stamped]
        )
        updated = contract.model_copy(update={"vacations": vacs})
        self._state_store.nounou_contract.save(updated)
        self._sync_nounou_reference_markdown(updated)
        if contract.calendar_id:
            tz = ZoneInfo(self._settings.timezone)
            t_min = datetime.combine(start_dt, time(0, 0), tzinfo=tz).isoformat()
            t_max = datetime.combine(end_dt, time(23, 59, 59), tzinfo=tz).isoformat()
            for ev in self._calendar.list_events(contract.calendar_id, t_min, t_max):
                if ev.event_id and ev.summary.startswith("Garde "):
                    self._calendar.delete_event(contract.calendar_id, ev.event_id)

    def _generate_month_events(
        self,
        year: int,
        month: int,
        contract: NounouContract,
    ) -> list[CalendarEventItem]:
        """Generate scheduled CalendarEventItems for a given (year, month)."""
        holidays = compute_french_public_holidays(year)
        schedule_map = {w.weekday.upper(): w for w in contract.scheduled_workdays}
        vac_ranges = [
            (date.fromisoformat(v.start_date), date.fromisoformat(v.end_date))
            for v in contract.vacations
        ]
        tz = ZoneInfo(self._settings.timezone)
        _, days_in_month = calendar.monthrange(year, month)
        events: list[CalendarEventItem] = []
        for day_num in range(1, days_in_month + 1):
            cur = date(year, month, day_num)
            if cur in holidays or any(start <= cur <= end for start, end in vac_ranges):
                continue
            workday = schedule_map.get(cur.strftime("%A").upper())
            if workday is None:
                continue
            sh, sm = (int(p) for p in workday.start_time.split(":", maxsplit=1))
            eh, em = (int(p) for p in workday.end_time.split(":", maxsplit=1))
            start_dt = datetime.combine(cur, time(sh, sm), tzinfo=tz)
            end_dt = datetime.combine(cur, time(eh, em), tzinfo=tz)
            events.append(
                CalendarEventItem(
                    summary=f"Garde {contract.child_name}",
                    start_iso=start_dt.isoformat(),
                    end_iso=end_dt.isoformat(),
                    description=f"Contrat {contract.contract_id} ({workday.hours:.1f}h)",
                )
            )
        return events

    def _sync_nounou_reference_markdown(self, contract: NounouContract) -> None:
        """Upsert 02_assistante_maternelle.md with YAML frontmatter and concise synthesis."""
        source_ids = [contract.drive_contract_file_id] if contract.drive_contract_file_id else []
        fm = FrontmatterSchema(
            id="assistante_maternelle",
            title=f"Contrat Assistante Maternelle — {contract.employee_name}",
            domain="childcare",
            summary=(
                f"Contrat actif ({contract.base_monthly_hours}h/mois, "
                f"{contract.hourly_rate_net:.2f} EUR/h net, "
                f"calendrier {self._settings.nounou_calendar_name})."
            ),
            keywords=["nounou", "assistante maternelle", "garde", "pajemploi", "vacances"],
            drive_source_ids=source_ids,
        )
        sched = "\n".join(
            f"- {w.weekday} : {w.start_time} - {w.end_time} ({w.hours:.1f}h)"
            for w in contract.scheduled_workdays
        )
        vacs = (
            "\n".join(f"- {v.start_date} -> {v.end_date} ({v.source})" for v in contract.vacations)
            or "- Aucun congé enregistré."
        )
        body = (
            f"# Contrat Assistante Maternelle — {contract.employee_name}\n\n"
            f"## 1. Paramètres Clés\n"
            f"- **Calendrier** : `{contract.calendar_id}` "
            f"(`{self._settings.nounou_calendar_name}`)\n"
            f"- **Mensualisation** : {contract.base_monthly_net_salary:.2f} € net "
            f"({contract.base_monthly_hours}h / {contract.base_monthly_days}j)\n"
            f"- **Taux horaire net** : {contract.hourly_rate_net:.2f} € | "
            f"Entretien : {contract.maintenance_allowance_daily:.2f} €/j | "
            f"Repas : {contract.meal_allowance_daily:.2f} €/j\n"
            f"- **Préavis congés** : {contract.vacation_notice_days} jours\n\n"
            f"## 2. Horaires Hebdomadaires\n{sched}\n\n"
            f"## 3. Congés Enregistrés\n{vacs}\n"
        )
        self._knowledge.upsert_document("02_assistante_maternelle.md", fm, body)
