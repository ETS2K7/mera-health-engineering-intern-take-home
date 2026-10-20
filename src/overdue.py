from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Tuple

from src.classifier import ANTIDIABETIC_DRUGS, DiabetesClassification
from src.parser import LabRecord, VisitRecord


REFERENCE_DATE = date(2026, 9, 30)
DEFAULT_FOLLOWUP_DAYS = 90

DM_VISIT_NOTE_PATTERN = re.compile(
    r"\b(dm|dm2|diabet\w*|t2dm|t1dm|niddm|iddm|gdm|hba1c|a1c|fbs|ppbs|grbs|oha|insulin|metformin|sugar\w*|f\s+\d+\s*/\s*pp|polyuria|increased\s+thirst|burning\s+feet|burning\s+soles|nocturia|fundus|urine\s+microalbumin|dose\s+stepped\s+up|executive\s+health\s+check)\b",
    re.IGNORECASE,
)

REVIEW_INTERVAL_PATTERN = re.compile(
    r"\b(?:review|r/v|rpt(?:\s+\w+){0,3})\s*(?:after\s+|in\s+|at\s+)?(\d+)\s*(day|days|wk|wks|week|weeks|month|months|yr|yrs|/12|/52|/7)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OverdueEvaluation:
    classification: DiabetesClassification
    reference_date: date
    status: str
    # Status values:
    # - "PRIORITY_1_MISSED_APPT": Missed explicit scheduled next_appointment in the past with no subsequent visit
    # - "PRIORITY_2_ROUTINE_OVERDUE": No upcoming booking, and elapsed time since last diabetes visit exceeds note interval or 90 days
    # - "BORDERLINE_NEEDS_REVIEW": Unconfirmed diabetic-range lab or GDM needing postpartum screening/follow-up
    # - "UPCOMING_BOOKED": Has a scheduled appointment on or after reference_date
    # - "ON_TRACK_RECENT": Seen within expected window (<= note interval / 90 days) with no missed booking
    # - "SUPPRESSED_DECEASED": Patient is deceased (never call)
    # - "SUPPRESSED_TRANSFERRED": Patient transferred care / relocated (never call)
    # - "NOT_IN_COHORT": Non-diabetic, PCOS exclusion, prediabetes, etc.
    priority_rank: Optional[int]  # 1, 2, 3 (borderline review), or None
    include_on_call_list: bool
    last_dm_visit: Optional[VisitRecord]
    last_any_visit: Optional[VisitRecord]
    unrelated_recent_visit: Optional[VisitRecord]
    days_since_last_dm_visit: Optional[int]
    scheduled_next_appointment: Optional[date]
    note_interval_label: Optional[str]
    note_interval_days: Optional[int]
    effective_due_date: Optional[date]
    days_overdue: Optional[int]
    assigned_doctor: Optional[str]
    assigned_department: Optional[str]
    data_anomalies: Tuple[str, ...]
    status_explanation: str


def parse_note_interval(notes: str) -> Tuple[Optional[int], Optional[str]]:
    match = REVIEW_INTERVAL_PATTERN.search(notes)
    if not match:
        return None, None
    val = int(match.group(1))
    unit = match.group(2).lower()
    if unit in ("day", "days", "/7"):
        return val, f"{val} days"
    if unit in ("wk", "wks", "week", "weeks", "/52"):
        return val * 7, f"{val} wks"
    if unit in ("month", "months", "/12"):
        return val * 30, f"{val} months"
    if unit in ("yr", "yrs"):
        return val * 365, f"{val} yr"
    return None, None


def get_diabetes_relevant_visits(classification: DiabetesClassification) -> List[VisitRecord]:
    patient = classification.patient
    rx_visit_ids = {
        r.visit_id
        for r in patient.prescriptions
        if r.canonical_drug in ANTIDIABETIC_DRUGS
    }
    relevant: List[VisitRecord] = []
    for v in patient.visits:
        if v.department in ("Diabetology", "Paediatrics"):
            relevant.append(v)
        elif classification.category == "GDM" and v.department == "OBG":
            relevant.append(v)
        elif v.visit_id in rx_visit_ids:
            relevant.append(v)
        elif v.department == "General Medicine" and DM_VISIT_NOTE_PATTERN.search(v.notes):
            relevant.append(v)
    return relevant


def find_patient_anomalies(
    classification: DiabetesClassification,
    reference_date: date,
    unrelated_recent_visit: Optional[VisitRecord],
) -> Tuple[str, ...]:
    anomalies: List[str] = []
    p = classification.patient

    if p.is_duplicate_merged:
        mrn_str = " + ".join(p.all_mrns)
        name_str = " / ".join(p.all_names)
        anomalies.append(f"Merged duplicate MRNs ({mrn_str}: {name_str})")

    if p.missing_phone:
        anomalies.append("Missing phone number in patients.csv")

    if p.shared_phone_mrns:
        anomalies.append(
            f"Phone {p.phone} is shared with other patient record(s): {', '.join(p.shared_phone_mrns)}"
        )

    for l in p.lab_results:
        if l.collected_on > reference_date:
            anomalies.append(
                f"Future/typo lab date {l.raw_collected_on} ({l.collected_on.isoformat()}) for {l.canonical_test}={l.normalized_value}{l.normalized_unit}"
            )
        if l.unit_inferred and l.canonical_test == "HbA1c":
            anomalies.append(
                f"Lab {l.raw_test_name}={l.raw_value:g} on {l.collected_on.isoformat()} had blank unit; converted from IFCC mmol/mol to {l.normalized_value}%"
            )

    # Check for duplicate identical lab rows on the same date
    seen_labs = set()
    for l in p.lab_results:
        key = (l.canonical_test, l.collected_on, l.normalized_value)
        if key in seen_labs:
            anomalies.append(
                f"Duplicate {l.canonical_test} lab entry ({l.normalized_value}{l.normalized_unit}) on {l.collected_on.isoformat()}"
            )
        seen_labs.add(key)

    # Check for labs collected in Sept 2026 with no corresponding clinic visit logged
    last_visit_date = p.visits[-1].visit_date if p.visits else None
    for l in p.lab_results:
        if l.collected_on <= reference_date and last_visit_date and (l.collected_on - last_visit_date).days > 14:
            anomalies.append(
                f"Lab collected on {l.collected_on.isoformat()} ({l.canonical_test}={l.normalized_value}{l.normalized_unit}) has no subsequent doctor visit logged"
            )

    if unrelated_recent_visit is not None:
        anomalies.append(
            f"Visited {unrelated_recent_visit.department} on {unrelated_recent_visit.visit_date.isoformat()} (non-diabetes visit; did not reset diabetes follow-up)"
        )

    return tuple(dict.fromkeys(anomalies))


def evaluate_patient_overdue(
    classification: DiabetesClassification,
    reference_date: date = REFERENCE_DATE,
) -> OverdueEvaluation:
    patient = classification.patient
    last_any_visit = patient.visits[-1] if patient.visits else None

    if not (classification.is_confirmed_diabetic or classification.is_borderline_review):
        return OverdueEvaluation(
            classification=classification,
            reference_date=reference_date,
            status="NOT_IN_COHORT",
            priority_rank=None,
            include_on_call_list=False,
            last_dm_visit=None,
            last_any_visit=last_any_visit,
            unrelated_recent_visit=None,
            days_since_last_dm_visit=None,
            scheduled_next_appointment=None,
            note_interval_label=None,
            note_interval_days=None,
            effective_due_date=None,
            days_overdue=None,
            assigned_doctor=None,
            assigned_department=None,
            data_anomalies=(),
            status_explanation="Not in confirmed or borderline diabetic cohort.",
        )

    dm_visits = get_diabetes_relevant_visits(classification)
    last_dm_visit = dm_visits[-1] if dm_visits else last_any_visit

    unrelated_recent_visit: Optional[VisitRecord] = None
    if last_any_visit and last_dm_visit and last_any_visit.visit_date > last_dm_visit.visit_date:
        unrelated_recent_visit = last_any_visit

    days_since_last_dm = (
        (reference_date - last_dm_visit.visit_date).days if last_dm_visit else None
    )
    scheduled_next = last_dm_visit.next_appointment if last_dm_visit else None
    note_days, note_label = (
        parse_note_interval(last_dm_visit.notes) if last_dm_visit else (None, None)
    )
    assigned_doctor = last_dm_visit.doctor if last_dm_visit else None
    assigned_department = last_dm_visit.department if last_dm_visit else None

    anomalies = find_patient_anomalies(
        classification=classification,
        reference_date=reference_date,
        unrelated_recent_visit=unrelated_recent_visit,
    )

    # 1. Suppress deceased and transferred patients first
    if patient.deceased_flag is not None:
        df = patient.deceased_flag
        return OverdueEvaluation(
            classification=classification,
            reference_date=reference_date,
            status="SUPPRESSED_DECEASED",
            priority_rank=None,
            include_on_call_list=False,
            last_dm_visit=last_dm_visit,
            last_any_visit=last_any_visit,
            unrelated_recent_visit=unrelated_recent_visit,
            days_since_last_dm_visit=days_since_last_dm,
            scheduled_next_appointment=scheduled_next,
            note_interval_label=note_label,
            note_interval_days=note_days,
            effective_due_date=None,
            days_overdue=None,
            assigned_doctor=assigned_doctor,
            assigned_department=assigned_department,
            data_anomalies=anomalies,
            status_explanation=(
                f"DO NOT CALL — Patient documented as deceased on {df.visit_date.isoformat()} "
                f"({df.department}): \"{df.note_excerpt}\""
            ),
        )

    if patient.transferred_flag is not None:
        tf = patient.transferred_flag
        return OverdueEvaluation(
            classification=classification,
            reference_date=reference_date,
            status="SUPPRESSED_TRANSFERRED",
            priority_rank=None,
            include_on_call_list=False,
            last_dm_visit=last_dm_visit,
            last_any_visit=last_any_visit,
            unrelated_recent_visit=unrelated_recent_visit,
            days_since_last_dm_visit=days_since_last_dm,
            scheduled_next_appointment=scheduled_next,
            note_interval_label=note_label,
            note_interval_days=note_days,
            effective_due_date=None,
            days_overdue=None,
            assigned_doctor=assigned_doctor,
            assigned_department=assigned_department,
            data_anomalies=anomalies,
            status_explanation=(
                f"DO NOT CALL — Patient transferred care / relocated on {tf.visit_date.isoformat()} "
                f"({tf.department}): \"{tf.note_excerpt}\""
            ),
        )

    # 2. Borderline / Unconfirmed / GDM cases separated for clinical verification
    if classification.is_borderline_review and not classification.is_confirmed_diabetic:
        interval_to_use = note_days if note_days is not None else DEFAULT_FOLLOWUP_DAYS
        due_date = (
            scheduled_next
            if scheduled_next is not None
            else (
                last_dm_visit.visit_date + timedelta(days=interval_to_use)
                if last_dm_visit
                else None
            )
        )
        days_over = (reference_date - due_date).days if due_date else None
        return OverdueEvaluation(
            classification=classification,
            reference_date=reference_date,
            status="BORDERLINE_NEEDS_REVIEW",
            priority_rank=3,
            include_on_call_list=False,
            last_dm_visit=last_dm_visit,
            last_any_visit=last_any_visit,
            unrelated_recent_visit=unrelated_recent_visit,
            days_since_last_dm_visit=days_since_last_dm,
            scheduled_next_appointment=scheduled_next,
            note_interval_label=note_label,
            note_interval_days=note_days,
            effective_due_date=due_date,
            days_overdue=days_over,
            assigned_doctor=assigned_doctor,
            assigned_department=assigned_department,
            data_anomalies=anomalies,
            status_explanation="; ".join(classification.reasons),
        )

    # 3. Confirmed Diabetics:
    assert last_dm_visit is not None
    assert days_since_last_dm is not None

    # Priority 1: Explicit next_appointment in the past with no subsequent diabetes visit
    if scheduled_next is not None and scheduled_next < reference_date:
        days_over = (reference_date - scheduled_next).days
        return OverdueEvaluation(
            classification=classification,
            reference_date=reference_date,
            status="PRIORITY_1_MISSED_APPT",
            priority_rank=1,
            include_on_call_list=True,
            last_dm_visit=last_dm_visit,
            last_any_visit=last_any_visit,
            unrelated_recent_visit=unrelated_recent_visit,
            days_since_last_dm_visit=days_since_last_dm,
            scheduled_next_appointment=scheduled_next,
            note_interval_label=note_label,
            note_interval_days=note_days,
            effective_due_date=scheduled_next,
            days_overdue=days_over,
            assigned_doctor=assigned_doctor,
            assigned_department=assigned_department,
            data_anomalies=anomalies,
            status_explanation=(
                f"Missed scheduled appointment on {scheduled_next.isoformat()} "
                f"({days_over} days overdue; last diabetes visit {last_dm_visit.visit_date.isoformat()})"
            ),
        )

    # Upcoming booking on or after reference_date
    if scheduled_next is not None and scheduled_next >= reference_date:
        days_until = (scheduled_next - reference_date).days
        return OverdueEvaluation(
            classification=classification,
            reference_date=reference_date,
            status="UPCOMING_BOOKED",
            priority_rank=None,
            include_on_call_list=False,
            last_dm_visit=last_dm_visit,
            last_any_visit=last_any_visit,
            unrelated_recent_visit=unrelated_recent_visit,
            days_since_last_dm_visit=days_since_last_dm,
            scheduled_next_appointment=scheduled_next,
            note_interval_label=note_label,
            note_interval_days=note_days,
            effective_due_date=scheduled_next,
            days_overdue=-days_until,
            assigned_doctor=assigned_doctor,
            assigned_department=assigned_department,
            data_anomalies=anomalies,
            status_explanation=(
                f"Upcoming appointment booked for {scheduled_next.isoformat()} "
                f"(in {days_until} days)"
            ),
        )

    # No explicit next_appointment: compare elapsed days against doctor's note interval or 90-day clinical threshold
    threshold_days = (
        min(note_days, DEFAULT_FOLLOWUP_DAYS)
        if note_days is not None
        else DEFAULT_FOLLOWUP_DAYS
    )
    due_date = last_dm_visit.visit_date + timedelta(days=threshold_days)

    if days_since_last_dm > threshold_days:
        days_over = (reference_date - due_date).days
        basis = (
            f"doctor note '{note_label}' ({note_days}d)"
            if note_days is not None and note_days <= DEFAULT_FOLLOWUP_DAYS
            else f"{DEFAULT_FOLLOWUP_DAYS}-day routine diabetes follow-up window"
        )
        return OverdueEvaluation(
            classification=classification,
            reference_date=reference_date,
            status="PRIORITY_2_ROUTINE_OVERDUE",
            priority_rank=2,
            include_on_call_list=True,
            last_dm_visit=last_dm_visit,
            last_any_visit=last_any_visit,
            unrelated_recent_visit=unrelated_recent_visit,
            days_since_last_dm_visit=days_since_last_dm,
            scheduled_next_appointment=None,
            note_interval_label=note_label,
            note_interval_days=note_days,
            effective_due_date=due_date,
            days_overdue=days_over,
            assigned_doctor=assigned_doctor,
            assigned_department=assigned_department,
            data_anomalies=anomalies,
            status_explanation=(
                f"No appointment booked; last diabetes visit {last_dm_visit.visit_date.isoformat()} "
                f"({days_since_last_dm} days ago, {days_over} days past {basis})"
            ),
        )

    # Seen within 90 days (and within note interval)
    days_remaining = (due_date - reference_date).days
    return OverdueEvaluation(
        classification=classification,
        reference_date=reference_date,
        status="ON_TRACK_RECENT",
        priority_rank=None,
        include_on_call_list=False,
        last_dm_visit=last_dm_visit,
        last_any_visit=last_any_visit,
        unrelated_recent_visit=unrelated_recent_visit,
        days_since_last_dm_visit=days_since_last_dm,
        scheduled_next_appointment=None,
        note_interval_label=note_label,
        note_interval_days=note_days,
        effective_due_date=due_date,
        days_overdue=-days_remaining,
        assigned_doctor=assigned_doctor,
        assigned_department=assigned_department,
        data_anomalies=anomalies,
        status_explanation=(
            f"Seen recently on {last_dm_visit.visit_date.isoformat()} "
            f"({days_since_last_dm} days ago; next review due around {due_date.isoformat()})"
        ),
    )


def evaluate_all_patients(
    classifications: List[DiabetesClassification],
    reference_date: date = REFERENCE_DATE,
) -> List[OverdueEvaluation]:
    evals = [evaluate_patient_overdue(c, reference_date=reference_date) for c in classifications]
    evals.sort(
        key=lambda e: (
            e.priority_rank if e.priority_rank is not None else 99,
            -(e.days_overdue if e.days_overdue is not None else -9999),
            e.classification.patient.primary_mrn,
        )
    )
    return evals
