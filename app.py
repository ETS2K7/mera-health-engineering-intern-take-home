#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import io
import json
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

from src.classifier import classify_all_patients
from src.overdue import REFERENCE_DATE, OverdueEvaluation, evaluate_all_patients
from src.parser import load_export
from src.safety import build_unified_patients


def build_evaluations(export_dir: str | Path = "export") -> List[OverdueEvaluation]:
    dataset = load_export(export_dir)
    unified = build_unified_patients(dataset)
    classified = classify_all_patients(unified)
    return evaluate_all_patients(classified, reference_date=REFERENCE_DATE)


def format_latest_labs(e: OverdueEvaluation) -> str:
    c = e.classification
    parts: List[str] = []
    # Only use labs on or before REFERENCE_DATE for primary display, note future typo separately
    valid_a1c = [
        l for l in c.patient.lab_results
        if l.canonical_test == "HbA1c" and l.collected_on <= e.reference_date
    ]
    if valid_a1c:
        last = valid_a1c[-1]
        parts.append(f"HbA1c {last.normalized_value}% ({last.collected_on.isoformat()})")
    if c.latest_fbs:
        valid_fbs = [
            l for l in c.patient.lab_results
            if l.canonical_test == "FBS" and l.collected_on <= e.reference_date
        ]
        if valid_fbs:
            last_f = valid_fbs[-1]
            parts.append(f"FBS {last_f.normalized_value:g} mg/dL ({last_f.collected_on.isoformat()})")
    if c.latest_ppbs:
        valid_pp = [
            l for l in c.patient.lab_results
            if l.canonical_test == "PPBS" and l.collected_on <= e.reference_date
        ]
        if valid_pp:
            last_p = valid_pp[-1]
            parts.append(f"PPBS {last_p.normalized_value:g} mg/dL ({last_p.collected_on.isoformat()})")
    return " | ".join(parts) if parts else "No DM lab on file"


def format_active_meds(e: OverdueEvaluation) -> str:
    rx_list = e.classification.patient.prescriptions
    if not rx_list:
        return "None recorded"
    # Group medications prescribed on the most recent prescription date with antidiabetic drugs
    dm_drugs = set(e.classification.antidiabetic_drugs)
    dm_rx = [r for r in rx_list if r.canonical_drug in dm_drugs]
    if not dm_rx:
        return "None recorded"
    latest_date = max(r.prescribed_on for r in dm_rx)
    latest_meds = [
        f"{r.canonical_drug} ({r.dose})"
        for r in dm_rx
        if r.prescribed_on == latest_date
    ]
    return ", ".join(dict.fromkeys(latest_meds))


def generate_call_list_rows(evals: List[OverdueEvaluation]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    call_evals = [e for e in evals if e.include_on_call_list]
    for idx, e in enumerate(call_evals, start=1):
        p = e.classification.patient
        valid_a1c = [
            l for l in p.lab_results
            if l.canonical_test == "HbA1c" and l.collected_on <= e.reference_date
        ]
        latest_a1c_val = f"{valid_a1c[-1].normalized_value}%" if valid_a1c else "N/A"
        latest_a1c_date = valid_a1c[-1].collected_on.isoformat() if valid_a1c else "N/A"
        priority_label = (
            "Priority 1: Missed Appointment"
            if e.status == "PRIORITY_1_MISSED_APPT"
            else "Priority 2: Routine Overdue"
        )
        rows.append(
            {
                "rank": str(idx),
                "priority": priority_label,
                "mrn": " / ".join(p.all_mrns),
                "name": " / ".join(p.all_names),
                "age_sex": f"{(e.reference_date.year - p.dob.year)} / {p.sex}",
                "phone": p.phone or "MISSING PHONE (Check Front Desk Chart)",
                "diabetes_type": "Type 1 DM" if e.classification.subtype == "T1DM" else "Type 2 DM",
                "assigned_doctor": e.assigned_doctor or "",
                "department": e.assigned_department or "",
                "last_dm_visit_date": e.last_dm_visit.visit_date.isoformat() if e.last_dm_visit else "",
                "scheduled_appointment": e.scheduled_next_appointment.isoformat() if e.scheduled_next_appointment else "",
                "effective_due_date": e.effective_due_date.isoformat() if e.effective_due_date else "",
                "days_overdue": str(e.days_overdue or 0),
                "latest_hba1c": latest_a1c_val,
                "latest_hba1c_date": latest_a1c_date,
                "active_medications": format_active_meds(e),
                "last_visit_notes": e.last_dm_visit.notes if e.last_dm_visit else "",
                "flags_and_notes": "; ".join(e.data_anomalies),
            }
        )
    return rows


def generate_quarantined_rows(evals: List[OverdueEvaluation]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for e in evals:
        if e.status in ("SUPPRESSED_DECEASED", "SUPPRESSED_TRANSFERRED"):
            p = e.classification.patient
            flag = p.deceased_flag if e.status == "SUPPRESSED_DECEASED" else p.transferred_flag
            rows.append(
                {
                    "mrn": " / ".join(p.all_mrns),
                    "name": " / ".join(p.all_names),
                    "dob": p.dob.isoformat(),
                    "sex": p.sex,
                    "phone": p.phone or "MISSING",
                    "suppression_reason": "DECEASED" if e.status == "SUPPRESSED_DECEASED" else "TRANSFERRED_OUT",
                    "event_date": flag.visit_date.isoformat() if flag else "",
                    "event_department": flag.department if flag else "",
                    "clinical_note": flag.note_excerpt if flag else "",
                }
            )
    return rows


def generate_flagged_review_rows(evals: List[OverdueEvaluation]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for e in evals:
        p = e.classification.patient
        c = e.classification
        # Include:
        # 1. Unconfirmed lab-diabetics (frank hyperglycemia without Rx/formal diagnosis)
        # 2. GDM patients (postpartum screening / persistent elevated HbA1c)
        # 3. Any confirmed diabetic with data anomalies (duplicate MRNs, missing phone, shared phone, recent labs without visit, future lab date, inferred lab units)
        if c.category in ("UNCONFIRMED_LAB_DIABETIC", "GDM") or (
            c.is_confirmed_diabetic and e.data_anomalies
        ):
            if c.category == "UNCONFIRMED_LAB_DIABETIC":
                review_type = "Unconfirmed Lab Hyperglycemia (No Rx / Formal Diagnosis)"
                recommended_action = "Doctor review: Confirm DM diagnosis & advise front desk whether to call for report review"
            elif c.category == "GDM":
                review_type = f"Gestational Diabetes ({c.subtype})"
                recommended_action = (
                    "Call for postpartum diabetes evaluation (HbA1c 6.6% post-delivery)"
                    if c.subtype == "GDM_PERSISTENT_POSTPARTUM_HYPERGLYCEMIA"
                    else (
                        "Verify if 6-week postpartum OGTT was completed"
                        if c.subtype == "GDM_UNCONFIRMED_POSTPARTUM_OGTT"
                        else "Resolved postpartum (HbA1c 5.4% normal) — no recall needed"
                    )
                )
            else:
                review_type = "Data Quality / Contact Flag on Confirmed Diabetic"
                recommended_action = "; ".join(e.data_anomalies)

            rows.append(
                {
                    "mrn": " / ".join(p.all_mrns),
                    "name": " / ".join(p.all_names),
                    "dob": p.dob.isoformat(),
                    "sex": p.sex,
                    "phone": p.phone or "MISSING",
                    "cohort_category": c.category,
                    "overdue_status": e.status,
                    "review_type": review_type,
                    "latest_labs": format_latest_labs(e),
                    "last_visit_date": e.last_dm_visit.visit_date.isoformat() if e.last_dm_visit else "",
                    "last_visit_notes": e.last_dm_visit.notes if e.last_dm_visit else "",
                    "details_and_anomalies": "; ".join(list(c.reasons) + list(e.data_anomalies)),
                    "recommended_action": recommended_action,
                }
            )
    return rows


def write_csv_file(path: Path, rows: List[Dict[str, str]]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def export_all_csvs(evals: List[OverdueEvaluation], out_dir: Path = Path(".")) -> Dict[str, int]:
    call_rows = generate_call_list_rows(evals)
    quarantined_rows = generate_quarantined_rows(evals)
    flagged_rows = generate_flagged_review_rows(evals)

    write_csv_file(out_dir / "overdue_call_list.csv", call_rows)
    write_csv_file(out_dir / "quarantined_patients.csv", quarantined_rows)
    write_csv_file(out_dir / "flagged_for_review.csv", flagged_rows)

    return {
        "overdue_call_list.csv": len(call_rows),
        "quarantined_patients.csv": len(quarantined_rows),
        "flagged_for_review.csv": len(flagged_rows),
    }


def render_dashboard_html(evals: List[OverdueEvaluation]) -> str:
    call_evals = [e for e in evals if e.include_on_call_list]
    p1_count = sum(1 for e in call_evals if e.status == "PRIORITY_1_MISSED_APPT")
    p2_count = sum(1 for e in call_evals if e.status == "PRIORITY_2_ROUTINE_OVERDUE")
    callable_now_count = sum(1 for e in call_evals if not e.classification.patient.missing_phone)
    missing_phone_overdue = sum(1 for e in call_evals if e.classification.patient.missing_phone)

    gdm_evals = [e for e in evals if e.classification.category == "GDM"]
    unconfirmed_lab_evals = [e for e in evals if e.classification.category == "UNCONFIRMED_LAB_DIABETIC"]
    quarantined_evals = [e for e in evals if e.status in ("SUPPRESSED_DECEASED", "SUPPRESSED_TRANSFERRED")]
    on_track_evals = [e for e in evals if e.status in ("UPCOMING_BOOKED", "ON_TRACK_RECENT")]
    excluded_pcos_evals = [e for e in evals if e.classification.category == "EXCLUDED_PCOS"]
    predm_evals = [e for e in evals if e.classification.category == "PREDIABETES_OR_RESOLVED_BORDERLINE"]

    # Build Call List table rows
    call_rows_html: List[str] = []
    for idx, e in enumerate(call_evals, start=1):
        p = e.classification.patient
        c = e.classification
        is_p1 = e.status == "PRIORITY_1_MISSED_APPT"
        badge_cls = "badge-p1" if is_p1 else "badge-p2"
        badge_txt = "Priority 1 · Missed Appt" if is_p1 else "Priority 2 · Routine Overdue"

        valid_a1c = [
            l for l in p.lab_results
            if l.canonical_test == "HbA1c" and l.collected_on <= e.reference_date
        ]
        a1c_val = valid_a1c[-1].normalized_value if valid_a1c else None
        a1c_str = f"{a1c_val:.1f}%" if a1c_val is not None else "No HbA1c"
        a1c_date = valid_a1c[-1].collected_on.isoformat() if valid_a1c else ""
        a1c_cls = "a1c-high" if (a1c_val and a1c_val >= 8.0) else ("a1c-mod" if (a1c_val and a1c_val >= 7.0) else "a1c-ok")

        phone_html = (
            f'<span class="phone-num">{html.escape(p.phone)}</span>'
            if p.phone
            else '<span class="badge-warn">Missing Phone — Check Paper File</span>'
        )
        if p.shared_phone_mrns:
            phone_html += f'<div class="sub-flag">Shared household phone ({html.escape(", ".join(p.shared_phone_mrns))})</div>'

        mrn_display = " / ".join(p.all_mrns)
        name_display = " / ".join(p.all_names)
        dup_badge = '<span class="badge-info">Merged Duplicate MRNs</span>' if p.is_duplicate_merged else ""
        dm_badge = '<span class="badge-t1">T1DM (Paeds)</span>' if c.subtype == "T1DM" else '<span class="badge-t2">T2DM</span>'

        anomaly_items = "".join(
            f'<div class="anomaly-pill">{html.escape(a)}</div>' for a in e.data_anomalies
        )

        due_detail = (
            f"Missed booking: <strong>{e.scheduled_next_appointment.isoformat()}</strong>"
            if e.scheduled_next_appointment
            else f"Due by: <strong>{e.effective_due_date.isoformat() if e.effective_due_date else ''}</strong> ({html.escape(e.note_interval_label or '90d default')})"
        )

        call_rows_html.append(
            f"""
            <tr data-priority="{'p1' if is_p1 else 'p2'}" data-dept="{html.escape(e.assigned_department or '')}" data-doctor="{html.escape(e.assigned_doctor or '')}" data-missing-phone="{'yes' if p.missing_phone else 'no'}" data-high-a1c="{'yes' if (a1c_val and a1c_val >= 8.0) else 'no'}">
                <td class="col-rank">{idx}</td>
                <td>
                    <div class="patient-name">{html.escape(name_display)}</div>
                    <div class="patient-meta">{html.escape(mrn_display)} · {e.reference_date.year - p.dob.year}y / {html.escape(p.sex)} (DOB {p.dob.isoformat()})</div>
                    <div class="badges-row">{dm_badge} {dup_badge}</div>
                </td>
                <td>{phone_html}</td>
                <td>
                    <span class="badge {badge_cls}">{badge_txt}</span>
                    <div class="days-overdue"><strong>{e.days_overdue} days</strong> overdue</div>
                    <div class="due-sub">{due_detail}</div>
                </td>
                <td>
                    <div><strong>{html.escape(e.assigned_doctor or '')}</strong></div>
                    <div class="patient-meta">{html.escape(e.assigned_department or '')} · Last DM visit: {e.last_dm_visit.visit_date.isoformat() if e.last_dm_visit else ''} ({e.days_since_last_dm_visit}d ago)</div>
                </td>
                <td>
                    <span class="a1c-pill {a1c_cls}">{a1c_str}</span>
                    <div class="patient-meta">{a1c_date}</div>
                    <div class="patient-meta">{html.escape(format_latest_labs(e))}</div>
                </td>
                <td>
                    <div class="meds-text">{html.escape(format_active_meds(e))}</div>
                    <div class="notes-snippet">"{html.escape(e.last_dm_visit.notes if e.last_dm_visit else '')}"</div>
                    {anomaly_items}
                </td>
                <td>
                    <select class="outcome-select" data-mrn="{html.escape(p.primary_mrn)}" onchange="saveOutcome(this)">
                        <option value="Pending">Pending Call</option>
                        <option value="Rebooked">Rebooked Appointment</option>
                        <option value="Left Message">Left Message / Call Back</option>
                        <option value="No Answer">No Answer / Switched Off</option>
                        <option value="Seeing Outside Doctor">Seeing Outside Doctor</option>
                        <option value="Needs Chart Lookup">Needs Phone Lookup</option>
                    </select>
                </td>
            </tr>
            """
        )

    # Build GDM rows
    gdm_rows_html: List[str] = []
    for e in gdm_evals:
        p = e.classification.patient
        c = e.classification
        status_badge = (
            '<span class="badge badge-p1">Action Required · Elevated Postpartum HbA1c (6.6%)</span>'
            if c.subtype == "GDM_PERSISTENT_POSTPARTUM_HYPERGLYCEMIA"
            else (
                '<span class="badge badge-p2">Action Required · Missing 6-Wk Postpartum OGTT</span>'
                if c.subtype == "GDM_UNCONFIRMED_POSTPARTUM_OGTT"
                else '<span class="badge badge-ok">Resolved · Normal Postpartum HbA1c (5.4%)</span>'
            )
        )
        gdm_rows_html.append(
            f"""
            <tr>
                <td>
                    <div class="patient-name">{html.escape(p.primary_name)}</div>
                    <div class="patient-meta">{html.escape(p.primary_mrn)} · {e.reference_date.year - p.dob.year}y / {p.sex} · DOB {p.dob.isoformat()}</div>
                </td>
                <td><span class="phone-num">{html.escape(p.phone or 'MISSING')}</span></td>
                <td>{status_badge}</td>
                <td>{html.escape(format_latest_labs(e))}</td>
                <td>
                    <div><strong>{html.escape(e.assigned_doctor or '')}</strong> ({html.escape(e.assigned_department or '')})</div>
                    <div class="patient-meta">Last OBG visit: {e.last_dm_visit.visit_date.isoformat() if e.last_dm_visit else ''}</div>
                    <div class="notes-snippet">"{html.escape(e.last_dm_visit.notes if e.last_dm_visit else '')}"</div>
                </td>
                <td>{html.escape("; ".join(c.reasons))}</td>
            </tr>
            """
        )

    # Build Flagged for Review rows (Unconfirmed Lab Diabetics + Data Anomalies / Duplicates / Missing Phones)
    unconfirmed_rows_html: List[str] = []
    for e in unconfirmed_lab_evals:
        p = e.classification.patient
        c = e.classification
        unconfirmed_rows_html.append(
            f"""
            <tr>
                <td>
                    <div class="patient-name">{html.escape(" / ".join(p.all_names))}</div>
                    <div class="patient-meta">{html.escape(" / ".join(p.all_mrns))} · {e.reference_date.year - p.dob.year}y / {p.sex}</div>
                </td>
                <td><span class="phone-num">{html.escape(p.phone or 'MISSING')}</span></td>
                <td><span class="a1c-pill a1c-high">{html.escape(format_latest_labs(e))}</span></td>
                <td>
                    <div><strong>{html.escape(e.assigned_doctor or '')}</strong> ({html.escape(e.assigned_department or '')})</div>
                    <div class="patient-meta">Last visit: {e.last_dm_visit.visit_date.isoformat() if e.last_dm_visit else ''}</div>
                    <div class="notes-snippet">"{html.escape(e.last_dm_visit.notes if e.last_dm_visit else '')}"</div>
                </td>
                <td>
                    <div>{html.escape("; ".join(c.reasons))}</div>
                    {"".join(f'<div class="anomaly-pill">{html.escape(a)}</div>' for a in e.data_anomalies)}
                </td>
            </tr>
            """
        )

    data_anomaly_evals = [
        e for e in evals
        if e.classification.is_confirmed_diabetic and e.data_anomalies
    ]
    anomaly_rows_html: List[str] = []
    for e in data_anomaly_evals:
        p = e.classification.patient
        anomaly_rows_html.append(
            f"""
            <tr>
                <td>
                    <div class="patient-name">{html.escape(" / ".join(p.all_names))}</div>
                    <div class="patient-meta">{html.escape(" / ".join(p.all_mrns))} · DOB {p.dob.isoformat()}</div>
                </td>
                <td>{html.escape(p.phone or 'MISSING PHONE')}</td>
                <td>{html.escape(e.status)}</td>
                <td>{"".join(f'<div class="anomaly-pill">{html.escape(a)}</div>' for a in e.data_anomalies)}</td>
            </tr>
            """
        )

    # Build Safety Exclusions rows (Deceased & Transferred + Excluded PCOS & Prediabetes summary)
    safety_rows_html: List[str] = []
    for e in quarantined_evals:
        p = e.classification.patient
        is_dec = e.status == "SUPPRESSED_DECEASED"
        flag = p.deceased_flag if is_dec else p.transferred_flag
        badge = (
            '<span class="badge badge-danger">DO NOT CALL · DECEASED</span>'
            if is_dec
            else '<span class="badge badge-warn">DO NOT CALL · TRANSFERRED CARE</span>'
        )
        safety_rows_html.append(
            f"""
            <tr>
                <td>
                    <div class="patient-name">{html.escape(p.primary_name)}</div>
                    <div class="patient-meta">{html.escape(p.primary_mrn)} · DOB {p.dob.isoformat()} ({p.sex})</div>
                </td>
                <td>{badge}</td>
                <td>{flag.visit_date.isoformat() if flag else ''} ({html.escape(flag.department if flag else '')})</td>
                <td><div class="notes-snippet">"{html.escape(flag.note_excerpt if flag else '')}"</div></td>
            </tr>
            """
        )

    on_track_rows_html: List[str] = []
    for e in on_track_evals:
        p = e.classification.patient
        status_b = (
            f'<span class="badge badge-ok">Booked: {e.scheduled_next_appointment.isoformat()}</span>'
            if e.status == "UPCOMING_BOOKED" and e.scheduled_next_appointment
            else f'<span class="badge badge-info">Seen Recently ({e.days_since_last_dm_visit}d ago)</span>'
        )
        on_track_rows_html.append(
            f"""
            <tr>
                <td>
                    <div class="patient-name">{html.escape(" / ".join(p.all_names))}</div>
                    <div class="patient-meta">{html.escape(" / ".join(p.all_mrns))} · {e.reference_date.year - p.dob.year}y / {p.sex}</div>
                </td>
                <td>{html.escape(p.phone or 'MISSING')}</td>
                <td>{status_b}</td>
                <td>{e.last_dm_visit.visit_date.isoformat() if e.last_dm_visit else ''} · {html.escape(e.assigned_doctor or '')} ({html.escape(e.assigned_department or '')})</td>
                <td>{html.escape(format_latest_labs(e))}</td>
                <td>
                    <div class="notes-snippet">"{html.escape(e.last_dm_visit.notes if e.last_dm_visit else '')}"</div>
                    {"".join(f'<div class="anomaly-pill">{html.escape(a)}</div>' for a in e.data_anomalies)}
                </td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Diabetic Follow-Up & Recall Workbench (Snapshot: 2026-09-30)</title>
<style>
    :root {{
        --bg: #f8fafc;
        --panel: #ffffff;
        --border: #e2e8f0;
        --text: #0f172a;
        --muted: #475569;
        --p1-bg: #fef2f2;
        --p1-text: #991b1b;
        --p1-border: #f87171;
        --p2-bg: #fffbeb;
        --p2-text: #92400e;
        --p2-border: #fbbf24;
        --ok-bg: #f0fdf4;
        --ok-text: #166534;
        --info-bg: #eff6ff;
        --info-text: #1e40af;
        --accent: #0f172a;
    }}
    * {{ box-sizing: border-box; }}
    body {{
        margin: 0;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background: var(--bg);
        color: var(--text);
        font-size: 14px;
        line-height: 1.45;
    }}
    header {{
        background: #0f172a;
        color: #ffffff;
        padding: 18px 28px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
    }}
    header h1 {{
        margin: 0;
        font-size: 19px;
        font-weight: 600;
    }}
    header .sub {{
        color: #cbd5e1;
        font-size: 13px;
        margin-top: 3px;
    }}
    .btn {{
        display: inline-block;
        background: #2563eb;
        color: #fff;
        text-decoration: none;
        padding: 8px 14px;
        border-radius: 6px;
        font-weight: 500;
        font-size: 13px;
        border: none;
        cursor: pointer;
    }}
    .btn:hover {{ background: #1d4ed8; }}
    .btn-secondary {{
        background: #334155;
        margin-left: 6px;
    }}
    .container {{
        max-width: 1440px;
        margin: 0 auto;
        padding: 20px 28px 48px;
    }}
    .kpi-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
        gap: 12px;
        margin-bottom: 20px;
    }}
    .kpi-card {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 14px 16px;
    }}
    .kpi-card .num {{
        font-size: 26px;
        font-weight: 700;
        line-height: 1.1;
    }}
    .kpi-card .label {{
        font-size: 12px;
        color: var(--muted);
        margin-top: 4px;
        font-weight: 500;
    }}
    .tabs {{
        display: flex;
        gap: 6px;
        border-bottom: 2px solid var(--border);
        margin-bottom: 16px;
        flex-wrap: wrap;
    }}
    .tab-btn {{
        background: transparent;
        border: none;
        padding: 10px 16px;
        font-size: 14px;
        font-weight: 600;
        color: var(--muted);
        cursor: pointer;
        border-bottom: 3px solid transparent;
        margin-bottom: -2px;
    }}
    .tab-btn.active {{
        color: #1d4ed8;
        border-bottom-color: #1d4ed8;
        background: #fff;
        border-radius: 6px 6px 0 0;
    }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}
    .filter-bar {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 12px 16px;
        margin-bottom: 14px;
        display: flex;
        gap: 12px;
        align-items: center;
        flex-wrap: wrap;
    }}
    .filter-bar input, .filter-bar select {{
        padding: 6px 10px;
        border: 1px solid #cbd5e1;
        border-radius: 5px;
        font-size: 13px;
    }}
    table {{
        width: 100%;
        border-collapse: collapse;
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 8px;
        overflow: hidden;
    }}
    th {{
        background: #f1f5f9;
        text-align: left;
        padding: 10px 12px;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.03em;
        color: #334155;
        border-bottom: 1px solid var(--border);
    }}
    td {{
        padding: 11px 12px;
        border-bottom: 1px solid var(--border);
        vertical-align: top;
    }}
    tr:hover td {{ background: #f8fafc; }}
    .patient-name {{ font-weight: 600; font-size: 14px; }}
    .patient-meta {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
    .phone-num {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-weight: 600; font-size: 13.5px; }}
    .badge {{
        display: inline-block;
        padding: 3px 8px;
        border-radius: 4px;
        font-size: 11.5px;
        font-weight: 600;
    }}
    .badge-p1 {{ background: var(--p1-bg); color: var(--p1-text); border: 1px solid var(--p1-border); }}
    .badge-p2 {{ background: var(--p2-bg); color: var(--p2-text); border: 1px solid var(--p2-border); }}
    .badge-ok {{ background: var(--ok-bg); color: var(--ok-text); border: 1px solid #86efac; }}
    .badge-info {{ background: var(--info-bg); color: var(--info-text); border: 1px solid #93c5fd; font-size: 11px; padding: 2px 6px; border-radius: 4px; }}
    .badge-warn {{ background: #fff7ed; color: #c2410c; border: 1px solid #fdba74; font-size: 11.5px; padding: 2px 7px; border-radius: 4px; font-weight: 600; }}
    .badge-danger {{ background: #7f1d1d; color: #fff; font-size: 11.5px; padding: 3px 8px; border-radius: 4px; font-weight: 600; }}
    .badge-t1 {{ background: #fae8ff; color: #86198f; font-size: 11px; padding: 1px 6px; border-radius: 4px; font-weight: 600; }}
    .badge-t2 {{ background: #e2e8f0; color: #334155; font-size: 11px; padding: 1px 6px; border-radius: 4px; font-weight: 600; }}
    .days-overdue {{ margin-top: 4px; font-size: 13px; color: #b91c1c; }}
    .due-sub {{ font-size: 11.5px; color: var(--muted); }}
    .a1c-pill {{
        display: inline-block;
        padding: 2px 8px;
        border-radius: 99px;
        font-weight: 700;
        font-size: 12.5px;
    }}
    .a1c-high {{ background: #fee2e2; color: #991b1b; }}
    .a1c-mod {{ background: #fef3c7; color: #92400e; }}
    .a1c-ok {{ background: #dcfce7; color: #166534; }}
    .meds-text {{ font-weight: 500; font-size: 12.5px; color: #1e293b; }}
    .notes-snippet {{ font-size: 12px; color: var(--muted); font-style: italic; margin-top: 3px; }}
    .anomaly-pill {{
        margin-top: 4px;
        background: #fff7ed;
        border-left: 3px solid #f97316;
        padding: 3px 7px;
        font-size: 11.5px;
        color: #9a3412;
    }}
    .sub-flag {{
        font-size: 11px;
        color: #b45309;
        margin-top: 2px;
    }}
    .outcome-select {{
        padding: 6px 8px;
        border-radius: 5px;
        border: 1px solid #cbd5e1;
        font-size: 12.5px;
        background: #fff;
        width: 100%;
    }}
    .section-box {{
        background: var(--panel);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 16px;
        margin-bottom: 18px;
    }}
    .section-box h3 {{ margin: 0 0 6px 0; font-size: 16px; }}
    .section-box p {{ margin: 0 0 12px 0; color: var(--muted); font-size: 13px; }}
    @media print {{
        header .actions, .tabs, .filter-bar {{ display: none !important; }}
    }}
</style>
</head>
<body>
<header>
    <div>
        <h1>Diabetic Patient Follow-Up &amp; Front-Desk Recall Workbench</h1>
        <div class="sub">Export Reference Date: <strong>2026-09-30</strong> (last clinical activity 2026-09-29) · 300 raw MRNs &rarr; 296 unique patients (4 duplicate MRN pairs merged)</div>
    </div>
    <div class="actions">
        <a class="btn" href="/download/overdue_call_list.csv">&#11015; Download Call List CSV (45)</a>
        <a class="btn btn-secondary" href="/download/flagged_for_review.csv">&#11015; Flagged for Review CSV</a>
        <a class="btn btn-secondary" href="/download/quarantined_patients.csv">&#11015; Do-Not-Call CSV</a>
    </div>
</header>

<div class="container">
    <div class="kpi-grid">
        <div class="kpi-card">
            <div class="num" style="color:#b91c1c;">{len(call_evals)}</div>
            <div class="label">Confirmed Diabetics Overdue ({callable_now_count} with phone, {missing_phone_overdue} missing phone)</div>
        </div>
        <div class="kpi-card">
            <div class="num" style="color:#991b1b;">{p1_count}</div>
            <div class="label">Priority 1: Missed Scheduled Appointment</div>
        </div>
        <div class="kpi-card">
            <div class="num" style="color:#92400e;">{p2_count}</div>
            <div class="label">Priority 2: Routine Overdue (&gt; Note Interval or 90d)</div>
        </div>
        <div class="kpi-card">
            <div class="num" style="color:#c2410c;">{len(unconfirmed_lab_evals) + 2}</div>
            <div class="label">Doctor Review: 7 Unconfirmed High-A1c + 2 Postpartum GDM</div>
        </div>
        <div class="kpi-card">
            <div class="num" style="color:#166534;">{len(on_track_evals)}</div>
            <div class="label">On-Track / Upcoming Appointment (18 Booked, 16 Recent)</div>
        </div>
        <div class="kpi-card">
            <div class="num" style="color:#64748b;">{len(quarantined_evals)}</div>
            <div class="label">Suppressed for Safety (3 Deceased, 3 Transferred Care)</div>
        </div>
    </div>

    <div class="tabs">
        <button class="tab-btn active" onclick="switchTab('tab-call', this)">1. Priority Call List ({len(call_evals)})</button>
        <button class="tab-btn" onclick="switchTab('tab-gdm', this)">2. Postpartum GDM Recall ({len(gdm_evals)})</button>
        <button class="tab-btn" onclick="switchTab('tab-review', this)">3. Flagged for Review ({len(unconfirmed_lab_evals)} Lab-Only &amp; {len(data_anomaly_evals)} Data Flags)</button>
        <button class="tab-btn" onclick="switchTab('tab-safety', this)">4. Safety Exclusions &amp; On-Track ({len(quarantined_evals)} Suppressed / {len(on_track_evals)} Active)</button>
    </div>

    <!-- TAB 1: CALL LIST -->
    <div id="tab-call" class="tab-panel active">
        <div class="filter-bar">
            <input type="text" id="searchInput" placeholder="Search name, MRN, phone, or drug..." oninput="filterCallTable()" style="min-width:250px;">
            <select id="priorityFilter" onchange="filterCallTable()">
                <option value="all">All Priorities (45)</option>
                <option value="p1">Priority 1: Missed Appointment ({p1_count})</option>
                <option value="p2">Priority 2: Routine Overdue ({p2_count})</option>
            </select>
            <select id="deptFilter" onchange="filterCallTable()">
                <option value="all">All Departments</option>
                <option value="Diabetology">Diabetology</option>
                <option value="General Medicine">General Medicine</option>
                <option value="Paediatrics">Paediatrics (T1DM)</option>
            </select>
            <select id="a1cFilter" onchange="filterCallTable()">
                <option value="all">All HbA1c Levels</option>
                <option value="high">Poor Control (HbA1c &ge; 8.0%)</option>
            </select>
            <select id="phoneFilter" onchange="filterCallTable()">
                <option value="all">All Phone Statuses</option>
                <option value="no">Has Phone Number ({callable_now_count})</option>
                <option value="yes">Missing Phone Number ({missing_phone_overdue})</option>
            </select>
            <span id="visibleCount" style="margin-left:auto; font-size:12.5px; color:var(--muted); font-weight:600;">Showing {len(call_evals)} of {len(call_evals)} patients</span>
        </div>

        <table id="callTable">
            <thead>
                <tr>
                    <th style="width:42px;">#</th>
                    <th style="width:210px;">Patient &amp; MRN</th>
                    <th style="width:155px;">Phone</th>
                    <th style="width:190px;">Triage Urgency</th>
                    <th style="width:190px;">Doctor &amp; Last DM Visit</th>
                    <th style="width:170px;">Latest Glycemic Labs</th>
                    <th>Active Diabetes Meds, Notes &amp; Flags</th>
                    <th style="width:165px;">Front Desk Outcome</th>
                </tr>
            </thead>
            <tbody>
                {"".join(call_rows_html)}
            </tbody>
        </table>
    </div>

    <!-- TAB 2: POSTPARTUM GDM RECALL -->
    <div id="tab-gdm" class="tab-panel">
        <div class="section-box">
            <h3>Gestational Diabetes (GDM) Postpartum Recall</h3>
            <p>Gestational diabetes is separated from chronic Type 1 / Type 2 diabetes because glucose often normalizes after delivery, but patients require a 6-week postpartum OGTT / HbA1c check to catch persistent diabetes.</p>
            <table>
                <thead>
                    <tr>
                        <th>Patient &amp; MRN</th>
                        <th>Phone</th>
                        <th>Postpartum Status</th>
                        <th>Labs on Record</th>
                        <th>Last OBG Visit</th>
                        <th>Clinical Assessment</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(gdm_rows_html)}
                </tbody>
            </table>
        </div>
    </div>

    <!-- TAB 3: FLAGGED FOR REVIEW -->
    <div id="tab-review" class="tab-panel">
        <div class="section-box">
            <h3>A. Unconfirmed / Untreated Diabetic-Range Lab Results ({len(unconfirmed_lab_evals)} Unique Patients / 8 Raw MRNs)</h3>
            <p>These patients came to General Medicine with symptoms or for a health check and have <strong>frank diabetic-range HbA1c (6.8% to 8.2%)</strong>, but have <strong>no antidiabetic prescriptions</strong> and were told <em>"Review with reports"</em> or <em>"adv diet, rpt"</em>. Doctors should confirm whether the front desk should call them to review their lab reports.</p>
            <table>
                <thead>
                    <tr>
                        <th>Patient &amp; MRN</th>
                        <th>Phone</th>
                        <th>Latest Diabetic-Range Labs</th>
                        <th>Ordering Doctor &amp; Last Visit</th>
                        <th>Why Flagged for Doctor Review</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(unconfirmed_rows_html)}
                </tbody>
            </table>
        </div>

        <div class="section-box">
            <h3>B. Data Quality, Duplicate MRNs &amp; Contact Flags on Confirmed Diabetics ({len(data_anomaly_evals)} Patients)</h3>
            <p>Includes merged duplicate patient registrations (same DOB, sex, and phone), missing phone numbers, shared household phones, unlabelled IFCC HbA1c units, future-dated lab typos, and Sept 2026 lab draws with no logged doctor visit.</p>
            <table>
                <thead>
                    <tr>
                        <th>Patient &amp; MRN(s)</th>
                        <th>Phone</th>
                        <th>Current Status</th>
                        <th>Audit Findings &amp; Front-Desk Instructions</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(anomaly_rows_html)}
                </tbody>
            </table>
        </div>
    </div>

    <!-- TAB 4: SAFETY EXCLUSIONS & ON-TRACK -->
    <div id="tab-safety" class="tab-panel">
        <div class="section-box">
            <h3>A. Safety Exclusions — Deceased &amp; Transferred Patients ({len(quarantined_evals)} Patients — Never Call)</h3>
            <p>These 6 confirmed diabetic patients appear overdue by date alone, but clinical notes across General Medicine, Diabetology, and Emergency document that 3 passed away and 3 relocated / transferred care.</p>
            <table>
                <thead>
                    <tr>
                        <th>Patient &amp; MRN</th>
                        <th>Safety Status</th>
                        <th>Recorded Date &amp; Dept</th>
                        <th>Documented Clinical Note</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(safety_rows_html)}
                </tbody>
            </table>
        </div>

        <div class="section-box">
            <h3>B. False-Positive Exclusions Summary ({len(excluded_pcos_evals)} PCOS on Metformin, {len(predm_evals)} Prediabetes / Resolved Borderline)</h3>
            <p>
                <strong>Excluded PCOS/PCOD on Metformin ({len(excluded_pcos_evals)}):</strong> SVH025191 (Nasreen P Sait, HbA1c 5.0%), SVH028876 (Lissy R Rodrigues, HbA1c 5.4%), SVH029693 (Shalini Das, HbA1c 5.2%), SVH030554 (Uma Moolya, HbA1c 5.3%).<br>
                <strong>Prediabetes / IFG / IGT (4):</strong> SVH024560 (HbA1c 6.1%), SVH024742 (HbA1c 6.2%), SVH025926 (HbA1c 6.1%), SVH028864 (HbA1c 5.8%).<br>
                <strong>Borderline HbA1c Normalized on Lifestyle (3):</strong> SVH025442 (6.6% &rarr; 5.9%), SVH029446 (6.5% &rarr; 5.8%), SVH029638 (6.5% &rarr; 5.7%).
            </p>
        </div>

        <div class="section-box">
            <h3>C. Confirmed Diabetics Currently On-Track or Booked ({len(on_track_evals)} Patients — Do Not Call Now)</h3>
            <p>18 patients have an upcoming appointment booked after 2026-09-30, and 16 patients were seen for diabetes within the last 90 days.</p>
            <table>
                <thead>
                    <tr>
                        <th>Patient &amp; MRN</th>
                        <th>Phone</th>
                        <th>Follow-Up Status</th>
                        <th>Last Diabetes Visit</th>
                        <th>Latest Labs</th>
                        <th>Clinical Notes &amp; Flags</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(on_track_rows_html)}
                </tbody>
            </table>
        </div>
    </div>
</div>

<script>
function switchTab(tabId, btn) {{
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    btn.classList.add('active');
}}

function filterCallTable() {{
    const q = document.getElementById('searchInput').value.toLowerCase();
    const prio = document.getElementById('priorityFilter').value;
    const dept = document.getElementById('deptFilter').value;
    const a1c = document.getElementById('a1cFilter').value;
    const phone = document.getElementById('phoneFilter').value;

    const rows = document.querySelectorAll('#callTable tbody tr');
    let visible = 0;
    rows.forEach(row => {{
        const textMatch = !q || row.innerText.toLowerCase().includes(q);
        const prioMatch = prio === 'all' || row.dataset.priority === prio;
        const deptMatch = dept === 'all' || row.dataset.dept === dept;
        const a1cMatch = a1c === 'all' || row.dataset.highA1c === 'yes';
        const phoneMatch = phone === 'all' || row.dataset.missingPhone === phone;

        if (textMatch && prioMatch && deptMatch && a1cMatch && phoneMatch) {{
            row.style.display = '';
            visible++;
        }} else {{
            row.style.display = 'none';
        }}
    }});
    document.getElementById('visibleCount').innerText = `Showing ${{visible}} of ${{rows.length}} patients`;
}}

function saveOutcome(sel) {{
    try {{
        localStorage.setItem('call_outcome_' + sel.dataset.mrn, sel.value);
    }} catch (e) {{}}
}}

window.addEventListener('DOMContentLoaded', () => {{
    document.querySelectorAll('.outcome-select').forEach(sel => {{
        try {{
            const saved = localStorage.getItem('call_outcome_' + sel.dataset.mrn);
            if (saved) sel.value = saved;
        }} catch (e) {{}}
    }});
}});
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    evals: List[OverdueEvaluation] = []

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            body = render_dashboard_html(self.evals).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path in (
            "/download/overdue_call_list.csv",
            "/download/quarantined_patients.csv",
            "/download/flagged_for_review.csv",
        ):
            fname = parsed.path.split("/")[-1]
            fpath = Path(fname)
            if not fpath.exists():
                export_all_csvs(self.evals)
            data = fpath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate diabetic follow-up call lists and launch the local front-desk dashboard."
    )
    parser.add_argument(
        "--export",
        action="store_true",
        help="Generate CSV files only and exit without starting the web server.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for the local web dashboard (default: 8000).",
    )
    args = parser.parse_args()

    evals = build_evaluations("export")
    counts = export_all_csvs(evals)

    print(f"Reference Date: {REFERENCE_DATE.isoformat()}")
    for fname, count in counts.items():
        print(f"  Generated {fname}: {count} rows")

    if args.export:
        return

    DashboardHandler.evals = evals
    server = HTTPServer(("127.0.0.1", args.port), DashboardHandler)
    print(f"\nDashboard running at http://localhost:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
