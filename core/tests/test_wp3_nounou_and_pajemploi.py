"""WP3 TDD test suite for Garde Nounou contract, 20th seeding, vacations, and Pajemploi."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

from alfred.domain.models import (
    AlfredPaths,
    AlfredSettings,
    CalendarEventItem,
    NounouContract,
    ScheduledWorkday,
    VacationPeriod,
)
from alfred.repositories.state_store import StateStore
from alfred.services.calendar_service import CalendarService
from alfred.services.knowledge_service import KnowledgeService
from alfred.services.nounou_service import NounouService, compute_french_public_holidays
from alfred.services.pajemploi_calculator import (
    PajemploiCalculator,
    is_pajemploi_closure_day,
    pajemploi_closure_date,
)


def _sample_contract() -> NounouContract:
    """Build a realistic neutral NounouContract fixture for deterministic tests."""
    return NounouContract(
        contract_id="ctr_2026_01",
        employee_name="Assistante Maternelle Exemple",
        child_name="Enfant",
        drive_contract_file_id="drive_pdf_ctr_01",
        hourly_rate_net=4.50,
        overtime_multiplier=1.10,
        base_monthly_net_salary=630.00,
        base_monthly_hours=140,
        base_monthly_days=14,
        maintenance_allowance_daily=3.80,
        meal_allowance_daily=4.50,
        vacation_notice_days=30,
        scheduled_workdays=[
            ScheduledWorkday(weekday="MONDAY", start_time="08:30", end_time="18:30", hours=10.0),
            ScheduledWorkday(weekday="TUESDAY", start_time="08:30", end_time="18:30", hours=10.0),
            ScheduledWorkday(weekday="THURSDAY", start_time="08:30", end_time="18:30", hours=10.0),
            ScheduledWorkday(weekday="FRIDAY", start_time="08:30", end_time="18:30", hours=10.0),
        ],
        vacations=[
            VacationPeriod(
                start_date="2026-05-14",
                end_date="2026-05-15",
                source="parents",
                notified_at="2026-04-01",
                label="Pont Ascension",
            )
        ],
    )


def _build_nounou_service(tmp_path: Path) -> tuple[NounouService, StateStore, MagicMock]:
    """Create NounouService with local KnowledgeService and mocked CalendarService."""
    paths = AlfredPaths.from_env(tmp_path)
    paths.templates_knowledge_dir.mkdir(parents=True, exist_ok=True)
    for tpl in Path("/root/Alfred/core/templates/knowledge").glob("*.md"):
        (paths.templates_knowledge_dir / tpl.name).write_text(
            tpl.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    settings = AlfredSettings.load(paths)
    store = StateStore(paths)
    cal_client = MagicMock()
    cal_client.calendarList.return_value.list.return_value.execute.return_value = {"items": []}
    cal_client.calendars.return_value.insert.return_value.execute.return_value = {
        "id": "cal_garde_nounou"
    }
    cal_client.events.return_value.list.return_value.execute.return_value = {"items": []}
    cal_client.events.return_value.insert.return_value.execute.return_value = {"id": "ev_new"}

    knowledge = KnowledgeService(paths=paths, settings=settings, state_store=store)
    knowledge.bootstrap_templates_if_empty()
    cal_svc = CalendarService(paths=paths, settings=settings, calendar_client=cal_client)
    return NounouService(store, cal_svc, knowledge, settings), store, cal_client


def test_init_nounou_and_20th_of_month_seeding_excludes_holidays_and_vacations(
    tmp_path: Path,
) -> None:
    """Verify init-nounou creates Garde Nounou + 02_assistante_maternelle.md, and 20th seeds M+1."""
    svc, store, cal_client = _build_nounou_service(tmp_path)
    contract = _sample_contract()

    gate_plan = svc.prepare_init_nounou_plan(contract)
    assert "Garde Nounou" in gate_plan
    assert "02_assistante_maternelle.md" in gate_plan

    initialized = svc.execute_init_nounou(contract)
    assert initialized.calendar_id == "cal_garde_nounou"
    assert store.nounou_contract.exists()

    holidays_2026 = compute_french_public_holidays(2026)
    assert date(2026, 5, 1) in holidays_2026  # Labour Day (Friday)
    assert date(2026, 5, 8) in holidays_2026  # Victory Day (Friday)
    assert date(2026, 5, 14) in holidays_2026  # Ascension Thursday
    assert date(2026, 5, 25) in holidays_2026  # Whit Monday

    assert svc.should_run_monthly_seed(date(2026, 4, 20)) is True
    assert svc.should_run_monthly_seed(date(2026, 4, 19)) is False

    # On April 20, 2026, seed May 2026 (excludes May 1, May 8, May 14, May 15 vacation, May 25)
    events, seed_plan = svc.seed_next_month_schedule(date(2026, 4, 20), apply_to_calendar=True)
    seeded_dates = {ev.start_iso[:10] for ev in events}
    assert "2026-05-01" not in seeded_dates
    assert "2026-05-08" not in seeded_dates
    assert "2026-05-14" not in seeded_dates
    assert "2026-05-15" not in seeded_dates
    assert "2026-05-25" not in seeded_dates
    assert "2026-05-04" in seeded_dates
    assert "2026-05" in seed_plan
    assert cal_client.events.return_value.insert.call_count == len(events)


def test_vacation_notice_compliance_and_late_warning(tmp_path: Path) -> None:
    """Verify ~1-month (30-day) contractual vacation notice compliance check and plan output."""
    svc, _, _ = _build_nounou_service(tmp_path)
    svc.execute_init_nounou(_sample_contract())

    compliant = svc.evaluate_vacation_notice(
        VacationPeriod(start_date="2026-08-01", end_date="2026-08-15", source="parents"),
        reference_date=date(2026, 6, 20),
    )
    assert compliant.is_compliant is True
    assert compliant.days_until_start == 42

    late = svc.evaluate_vacation_notice(
        VacationPeriod(start_date="2026-08-01", end_date="2026-08-15", source="nounou"),
        reference_date=date(2026, 7, 15),
        persist=True,
    )
    assert late.is_compliant is False
    assert late.days_until_start == 17
    assert "ALERTE PRÉAVIS" in late.implementation_plan_markdown


def test_pajemploi_last_day_minus_3_across_february_leap_and_monthly_math() -> None:
    """Verify last_day - 3 across 28/29/30/31-day months and deterministic Pajemploi math."""
    assert pajemploi_closure_date(2026, 2) == date(2026, 2, 25)  # 28 - 3 = 25 (Feb non-leap)
    assert pajemploi_closure_date(2028, 2) == date(2028, 2, 26)  # 29 - 3 = 26 (Feb leap year)
    assert pajemploi_closure_date(2026, 4) == date(2026, 4, 27)  # 30 - 3 = 27
    assert pajemploi_closure_date(2026, 10) == date(2026, 10, 28)  # 31 - 3 = 28
    assert is_pajemploi_closure_day(date(2026, 2, 25)) is True
    assert is_pajemploi_closure_day(date(2026, 2, 24)) is False

    contract = _sample_contract()
    calc = PajemploiCalculator()

    # February 2026 has 16 scheduled Tue/Thu/Mon/Fri days (4 full weeks * 4 days = 16 days, 160h)
    # Build 15 worked days (1 with 2h overtime, 1 with 'repas parents') + 1 unpaid absence
    events: list[CalendarEventItem] = []
    cur = date(2026, 2, 1)
    worked_idx = 0
    while cur.month == 2:
        if cur.strftime("%A").upper() in {"MONDAY", "TUESDAY", "THURSDAY", "FRIDAY"}:
            if worked_idx == 0:
                events.append(
                    CalendarEventItem(
                        summary="Absence enfant",
                        start_iso=f"{cur.isoformat()}T08:30:00+01:00",
                        end_iso=f"{cur.isoformat()}T18:30:00+01:00",
                    )
                )
            elif worked_idx == 1:
                events.append(
                    CalendarEventItem(
                        summary="Garde Enfant",
                        start_iso=f"{cur.isoformat()}T08:30:00+01:00",
                        end_iso=f"{cur.isoformat()}T20:30:00+01:00",  # 12h -> 2h overtime!
                        description="repas parents",
                    )
                )
            else:
                events.append(
                    CalendarEventItem(
                        summary="Garde Enfant",
                        start_iso=f"{cur.isoformat()}T08:30:00+01:00",
                        end_iso=f"{cur.isoformat()}T18:30:00+01:00",
                    )
                )
            worked_idx += 1
        cur = date.fromordinal(cur.toordinal() + 1)

    report = calc.calculate_monthly_report(2026, 2, contract, events)
    assert report.period_month == "2026-02"
    assert report.actual_working_days == 15
    assert report.overtime_hours == 2.0
    assert report.heures_complementaires_majorees == 2
    assert report.absence_unpaid_hours == 10.0
    assert report.deduction_cour_cassation_eur > 0
    assert report.indemnites_entretien == round(15 * 3.80, 2)
    assert report.indemnites_repas == round(14 * 4.50, 2)  # 1 day had 'repas parents'
    assert report.total_virement_eur == round(
        report.salaire_net_total + report.indemnites_entretien + report.indemnites_repas, 2
    )
    assert "Pajemploi" in report.implementation_plan_markdown


def test_seed_to_pajemploi_roundtrip_and_december_dst_rollover(tmp_path: Path) -> None:
    """Verify May holidays/vacations cause 0 unpaid deduction and Dec 20 rolls over to Jan Y+1."""
    svc, _, _ = _build_nounou_service(tmp_path)
    contract = svc.execute_init_nounou(_sample_contract())
    calc = PajemploiCalculator()

    may_events, _ = svc.seed_next_month_schedule(date(2026, 4, 20), apply_to_calendar=False)
    assert may_events[0].start_iso.endswith("+02:00")  # Summer CEST
    may_report = calc.calculate_monthly_report(2026, 5, contract, may_events)
    assert may_report.absence_unpaid_hours == 0.0
    assert may_report.salaire_net_total == contract.base_monthly_net_salary

    jan_events, _ = svc.seed_next_month_schedule(date(2026, 12, 20), apply_to_calendar=False)
    assert jan_events[0].start_iso[:7] == "2027-01"
    assert jan_events[0].start_iso.endswith("+01:00")  # Winter CET
    assert "2027-01-01" not in {e.start_iso[:10] for e in jan_events}


def test_vacation_validation_deduplication_and_shift_pruning(tmp_path: Path) -> None:
    """Verify reversed vacation dates fail, persistence deduplicates, and Garde shifts prune."""
    import pytest

    with pytest.raises(ValueError, match="precedes"):
        VacationPeriod(start_date="2026-08-15", end_date="2026-08-01")

    svc, store, cal_client = _build_nounou_service(tmp_path)
    svc.execute_init_nounou(_sample_contract())
    clash_item = {
        "id": "ev_clash",
        "summary": "Garde Enfant",
        "start": {"dateTime": "2026-08-03T08:30:00+02:00"},
        "end": {"dateTime": "2026-08-03T18:30:00+02:00"},
    }
    cal_client.events.return_value.list.side_effect = [
        MagicMock(execute=lambda: {"items": [clash_item]}),
        MagicMock(execute=lambda: {"items": []}),
    ]
    vac = VacationPeriod(start_date="2026-08-01", end_date="2026-08-10", source="parents")
    svc.evaluate_vacation_notice(vac, reference_date=date(2026, 6, 1), persist=True)
    svc.evaluate_vacation_notice(vac, reference_date=date(2026, 6, 1), persist=True)
    assert len(store.nounou_contract.load().vacations) == 2
    cal_client.events.return_value.delete.assert_called_once_with(
        calendarId="cal_garde_nounou", eventId="ev_clash"
    )


def test_pajemploi_split_shifts_extra_unscheduled_workday_and_all_day_vacations() -> None:
    """Verify split shifts sum per date, extra Wednesday = overtime, and exclusive all-day ends."""
    monday_only = NounouContract(
        contract_id="c_mon",
        employee_name="Nounou",
        child_name="Enfant",
        hourly_rate_net=5.0,
        base_monthly_net_salary=200.0,
        base_monthly_hours=40,
        base_monthly_days=4,
        maintenance_allowance_daily=4.0,
        meal_allowance_daily=5.0,
        scheduled_workdays=[
            ScheduledWorkday(weekday="MONDAY", start_time="08:30", end_time="18:30", hours=10.0)
        ],
    )
    calc = PajemploiCalculator()
    events = [
        CalendarEventItem(
            summary="Garde Enfant",
            start_iso="2026-02-02T08:30:00+01:00",
            end_iso="2026-02-02T12:30:00+01:00",
        ),
        CalendarEventItem(
            summary="Garde Enfant",
            start_iso="2026-02-02T14:30:00+01:00",
            end_iso="2026-02-02T20:30:00+01:00",
        ),
        CalendarEventItem(
            summary="Garde Enfant",
            start_iso="2026-02-04T08:30:00+01:00",
            end_iso="2026-02-04T13:30:00+01:00",
        ),
        CalendarEventItem(summary="Congés payés", start_iso="2026-02-09", end_iso="2026-02-10"),
        CalendarEventItem(
            summary="Garde Enfant",
            start_iso="2026-02-23T08:30:00+01:00",
            end_iso="2026-02-23T18:30:00+01:00",
        ),
    ]
    rep = calc.calculate_monthly_report(2026, 2, monday_only, events)
    assert rep.overtime_hours == 5.0 and rep.heures_complementaires_majorees == 5
    assert rep.absence_unpaid_hours == 10.0
    assert rep.jours_conges_payes == 1 and rep.actual_working_days == 3
    assert rep.indemnites_repas == 15.0
