from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from src.safety import UnifiedPatient


ANTIDIABETIC_DRUGS = {
    "Metformin",
    "Metformin SR",
    "Glimepiride",
    "Glimepiride + Metformin",
    "Gliclazide MR",
    "Dapagliflozin",
    "Sitagliptin",
    "Vildagliptin",
    "Voglibose",
    "Human Mixtard 30/70",
    "Insulin Aspart",
    "Insulin Degludec",
    "Insulin Glargine",
    "Insulin Lispro",
}

T1DM_NOTE_PATTERN = re.compile(
    r"\b(t1dm|type\s*1\s*dm|type\s*1\s*diabetes|juvenile\s*diabetes|iddm)\b",
    re.IGNORECASE,
)

T2DM_CONFIRM_NOTE_PATTERN = re.compile(
    r"\b(known\s+diabetic|k/c/o\s+dm|t2dm|dm2|niddm|type\s*2\s*dm|type\s*2\s*diabetes|diabetes\s+mellitus|diabetic\s+since|diabetic\s*-\s*\d+\s*yrs|diabetic\s+x\s*\d+\s*yrs|\bdiabetic;)\b",
    re.IGNORECASE,
)

GDM_NOTE_PATTERN = re.compile(
    r"\b(gdm|gestational\s+diabetes)\b",
    re.IGNORECASE,
)

PCOS_NOTE_PATTERN = re.compile(
    r"\b(pcos|pcod|polycystic\s+ovar\w*)\b",
    re.IGNORECASE,
)

PREDIABETES_NOTE_PATTERN = re.compile(
    r"\b(prediabet\w*|pre-diabet\w*|impaired\s+fasting\s+glucose|\bifg\b|\bigt\b|sugars\s+borderline)\b",
    re.IGNORECASE,
)

NEGATED_OR_FAMILY_DM_PATTERN = re.compile(
    r"\b(no\s+h/o\s+diabetes|denies\s+diabetes|non-diabetic|father\s+diabetic|mother\s+diabetic|family\s+history\s+of\s+diabetes)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DiabetesClassification:
    patient: UnifiedPatient
    category: str
    # Categories:
    # - "CONFIRMED_T1DM": Confirmed Type 1 / Juvenile diabetes
    # - "CONFIRMED_T2DM": Confirmed Type 2 diabetes (diagnosed/treated in Diabetology or General Medicine)
    # - "UNCONFIRMED_LAB_DIABETIC": Repeat or frank diabetic-range labs (HbA1c >= 6.8% / FBS >= 126) without recorded diagnosis/Rx yet
    # - "GDM": Gestational diabetes (postpartum screening / persistent elevated HbA1c tracked separately)
    # - "PREDIABETES_OR_RESOLVED_BORDERLINE": Prediabetes / IFG / IGT or transient borderline HbA1c (6.5-6.6%) that normalized on lifestyle
    # - "EXCLUDED_PCOS": PCOS/PCOD on Metformin with normal HbA1c
    # - "EXCLUDED_NEGATED_OR_FAMILY_HX": Explicit negation ("no h/o diabetes") or family history only
    # - "NON_DIABETIC": No diabetes signal or normal screening labs only
    subtype: Optional[str]
    is_confirmed_diabetic: bool
    is_borderline_review: bool
    has_diabetology_visit: bool
    has_diagnostic_note: bool
    has_antidiabetic_rx: bool
    has_diagnostic_lab: bool
    latest_hba1c: Optional[Tuple[str, float]]
    max_hba1c: Optional[float]
    latest_fbs: Optional[Tuple[str, float]]
    latest_ppbs: Optional[Tuple[str, float]]
    antidiabetic_drugs: Tuple[str, ...]
    reasons: Tuple[str, ...]


def classify_patient(patient: UnifiedPatient) -> DiabetesClassification:
    has_diabetology_visit = any(v.department == "Diabetology" for v in patient.visits)

    all_notes = [v.notes for v in patient.visits]
    has_t1dm_note = any(T1DM_NOTE_PATTERN.search(n) for n in all_notes)
    has_t2dm_note = any(T2DM_CONFIRM_NOTE_PATTERN.search(n) for n in all_notes)
    has_gdm_note = any(GDM_NOTE_PATTERN.search(n) for n in all_notes)
    has_pcos_note = any(PCOS_NOTE_PATTERN.search(n) for n in all_notes)
    has_predm_note = any(PREDIABETES_NOTE_PATTERN.search(n) for n in all_notes)
    has_neg_or_fhx_note = any(NEGATED_OR_FAMILY_DM_PATTERN.search(n) for n in all_notes)

    dm_drugs = tuple(
         dict.fromkeys(
            r.canonical_drug
            for r in patient.prescriptions
            if r.canonical_drug in ANTIDIABETIC_DRUGS
        )
    )
    has_antidiabetic_rx = len(dm_drugs) > 0

    hba1c_labs = [l for l in patient.lab_results if l.canonical_test == "HbA1c"]
    fbs_labs = [l for l in patient.lab_results if l.canonical_test == "FBS"]
    ppbs_labs = [l for l in patient.lab_results if l.canonical_test == "PPBS"]

    latest_hba1c = (
        (hba1c_labs[-1].collected_on.isoformat(), hba1c_labs[-1].normalized_value)
        if hba1c_labs
        else None
    )
    max_hba1c = max((l.normalized_value for l in hba1c_labs), default=None)
    latest_fbs = (
        (fbs_labs[-1].collected_on.isoformat(), fbs_labs[-1].normalized_value)
        if fbs_labs
        else None
    )
    latest_ppbs = (
        (ppbs_labs[-1].collected_on.isoformat(), ppbs_labs[-1].normalized_value)
        if ppbs_labs
        else None
    )

    has_diagnostic_lab = (
        any(l.normalized_value >= 6.5 for l in hba1c_labs)
        or any(l.normalized_value >= 126.0 for l in fbs_labs)
        or any(l.normalized_value >= 200.0 for l in ppbs_labs)
    )

    reasons: List[str] = []

    # 1. Exclude PCOS/PCOD patients on Metformin with normal HbA1c (< 6.5%)
    if has_pcos_note and not has_diabetology_visit and not has_t2dm_note and not has_t1dm_note and not has_diagnostic_lab:
        reasons.append("PCOS/PCOD managed in OBG on Metformin with normal HbA1c (<5.7%)")
        return DiabetesClassification(
            patient=patient,
            category="EXCLUDED_PCOS",
            subtype="PCOS_METFORMIN",
            is_confirmed_diabetic=False,
            is_borderline_review=False,
            has_diabetology_visit=has_diabetology_visit,
            has_diagnostic_note=False,
            has_antidiabetic_rx=has_antidiabetic_rx,
            has_diagnostic_lab=has_diagnostic_lab,
            latest_hba1c=latest_hba1c,
            max_hba1c=max_hba1c,
            latest_fbs=latest_fbs,
            latest_ppbs=latest_ppbs,
            antidiabetic_drugs=dm_drugs,
            reasons=tuple(reasons),
        )

    # 2. Separate Gestational Diabetes (GDM) for distinct postpartum tracking
    if has_gdm_note and not has_diabetology_visit and not has_t2dm_note and not has_t1dm_note:
        if latest_hba1c and latest_hba1c[1] >= 6.5:
            subtype = "GDM_PERSISTENT_POSTPARTUM_HYPERGLYCEMIA"
            is_borderline = True
            reasons.append(
                f"Gestational diabetes in OBG; medications stopped at delivery without OGTT, but later HbA1c elevated at {latest_hba1c[1]}% ({latest_hba1c[0]})"
            )
        elif latest_hba1c and latest_hba1c[1] < 5.7:
            subtype = "GDM_RESOLVED_NORMAL_POSTPARTUM_A1C"
            is_borderline = False
            reasons.append(
                f"Gestational diabetes in OBG; insulin stopped post-delivery and subsequent HbA1c normalized ({latest_hba1c[1]}% on {latest_hba1c[0]})"
            )
        else:
            subtype = "GDM_UNCONFIRMED_POSTPARTUM_OGTT"
            is_borderline = True
            reasons.append(
                "Gestational diabetes in OBG; advised 6-week postpartum OGTT at last PNC visit with no subsequent glucose/HbA1c on record"
            )
        return DiabetesClassification(
            patient=patient,
            category="GDM",
            subtype=subtype,
            is_confirmed_diabetic=False,
            is_borderline_review=is_borderline,
            has_diabetology_visit=has_diabetology_visit,
            has_diagnostic_note=True,
            has_antidiabetic_rx=has_antidiabetic_rx,
            has_diagnostic_lab=has_diagnostic_lab,
            latest_hba1c=latest_hba1c,
            max_hba1c=max_hba1c,
            latest_fbs=latest_fbs,
            latest_ppbs=latest_ppbs,
            antidiabetic_drugs=dm_drugs,
            reasons=tuple(reasons),
        )

    # 3. Confirmed Type 1 or Type 2 Diabetes
    if has_t1dm_note:
        reasons.append("Documented Type 1 / juvenile diabetes in clinical notes")
        if has_antidiabetic_rx:
            reasons.append(f"Prescribed insulin regimen ({', '.join(dm_drugs)})")
        if latest_hba1c:
            reasons.append(f"Latest HbA1c {latest_hba1c[1]}% ({latest_hba1c[0]})")
        return DiabetesClassification(
            patient=patient,
            category="CONFIRMED_T1DM",
            subtype="T1DM",
            is_confirmed_diabetic=True,
            is_borderline_review=False,
            has_diabetology_visit=has_diabetology_visit,
            has_diagnostic_note=True,
            has_antidiabetic_rx=has_antidiabetic_rx,
            has_diagnostic_lab=has_diagnostic_lab,
            latest_hba1c=latest_hba1c,
            max_hba1c=max_hba1c,
            latest_fbs=latest_fbs,
            latest_ppbs=latest_ppbs,
            antidiabetic_drugs=dm_drugs,
            reasons=tuple(reasons),
        )

    if has_diabetology_visit or has_t2dm_note or has_antidiabetic_rx:
        if has_diabetology_visit:
            reasons.append("Seen in Diabetology department")
        if has_t2dm_note:
            reasons.append("Documented diabetes diagnosis in clinical notes")
        if has_antidiabetic_rx:
            reasons.append(f"Prescribed antidiabetic medication ({', '.join(dm_drugs)})")
        if latest_hba1c:
            reasons.append(f"Latest HbA1c {latest_hba1c[1]}% ({latest_hba1c[0]})")
        return DiabetesClassification(
            patient=patient,
            category="CONFIRMED_T2DM",
            subtype="T2DM",
            is_confirmed_diabetic=True,
            is_borderline_review=False,
            has_diabetology_visit=has_diabetology_visit,
            has_diagnostic_note=has_t2dm_note or has_diabetology_visit,
            has_antidiabetic_rx=has_antidiabetic_rx,
            has_diagnostic_lab=has_diagnostic_lab,
            latest_hba1c=latest_hba1c,
            max_hba1c=max_hba1c,
            latest_fbs=latest_fbs,
            latest_ppbs=latest_ppbs,
            antidiabetic_drugs=dm_drugs,
            reasons=tuple(reasons),
        )

    # 4. Unconfirmed / Unmedicated Lab-Diabetic vs. Resolved Borderline
    if has_diagnostic_lab:
        # Check if latest HbA1c remains in the diabetic range (>= 6.5%)
        if latest_hba1c and latest_hba1c[1] >= 6.5:
            reasons.append(
                f"Diabetic-range lab result (latest HbA1c {latest_hba1c[1]}% on {latest_hba1c[0]}) without formal diagnosis or prescription recorded yet"
            )
            return DiabetesClassification(
                patient=patient,
                category="UNCONFIRMED_LAB_DIABETIC",
                subtype="UNTREATED_FRANK_HYPERGLYCEMIA",
                is_confirmed_diabetic=False,
                is_borderline_review=True,
                has_diabetology_visit=False,
                has_diagnostic_note=False,
                has_antidiabetic_rx=False,
                has_diagnostic_lab=True,
                latest_hba1c=latest_hba1c,
                max_hba1c=max_hba1c,
                latest_fbs=latest_fbs,
                latest_ppbs=latest_ppbs,
                antidiabetic_drugs=dm_drugs,
                reasons=tuple(reasons),
            )
        else:
            # Had an initial borderline HbA1c (6.5-6.6%) with normal FBS, advised lifestyle modification, and repeat HbA1c normalized (< 6.0%)
            reasons.append(
                f"Initial borderline HbA1c ({max_hba1c}%) normalized to {latest_hba1c[1] if latest_hba1c else 'N/A'}% on lifestyle modification without medication"
            )
            return DiabetesClassification(
                patient=patient,
                category="PREDIABETES_OR_RESOLVED_BORDERLINE",
                subtype="RESOLVED_BORDERLINE_A1C",
                is_confirmed_diabetic=False,
                is_borderline_review=False,
                has_diabetology_visit=False,
                has_diagnostic_note=False,
                has_antidiabetic_rx=False,
                has_diagnostic_lab=True,
                latest_hba1c=latest_hba1c,
                max_hba1c=max_hba1c,
                latest_fbs=latest_fbs,
                latest_ppbs=latest_ppbs,
                antidiabetic_drugs=dm_drugs,
                reasons=tuple(reasons),
            )

    # 5. Prediabetes / IFG / IGT (HbA1c 5.7 - 6.4%)
    if has_predm_note or (max_hba1c is not None and 5.7 <= max_hba1c < 6.5):
        reasons.append(
            f"Prediabetes / IFG / IGT (latest HbA1c {latest_hba1c[1] if latest_hba1c else 'N/A'}%)"
        )
        return DiabetesClassification(
            patient=patient,
            category="PREDIABETES_OR_RESOLVED_BORDERLINE",
            subtype="PREDIABETES",
            is_confirmed_diabetic=False,
            is_borderline_review=False,
            has_diabetology_visit=False,
            has_diagnostic_note=False,
            has_antidiabetic_rx=False,
            has_diagnostic_lab=False,
            latest_hba1c=latest_hba1c,
            max_hba1c=max_hba1c,
            latest_fbs=latest_fbs,
            latest_ppbs=latest_ppbs,
            antidiabetic_drugs=dm_drugs,
            reasons=tuple(reasons),
        )

    # 6. Negated notes or Family history only
    if has_neg_or_fhx_note:
        reasons.append("Clinical note mentions family history of diabetes or explicitly denies diabetes")
        return DiabetesClassification(
            patient=patient,
            category="EXCLUDED_NEGATED_OR_FAMILY_HX",
            subtype="NEGATED_OR_FAMILY_HX",
            is_confirmed_diabetic=False,
            is_borderline_review=False,
            has_diabetology_visit=False,
            has_diagnostic_note=False,
            has_antidiabetic_rx=False,
            has_diagnostic_lab=False,
            latest_hba1c=latest_hba1c,
            max_hba1c=max_hba1c,
            latest_fbs=latest_fbs,
            latest_ppbs=latest_ppbs,
            antidiabetic_drugs=dm_drugs,
            reasons=tuple(reasons),
        )

    # 7. Non-diabetic
    return DiabetesClassification(
        patient=patient,
        category="NON_DIABETIC",
        subtype=None,
        is_confirmed_diabetic=False,
        is_borderline_review=False,
        has_diabetology_visit=False,
        has_diagnostic_note=False,
        has_antidiabetic_rx=False,
        has_diagnostic_lab=False,
        latest_hba1c=latest_hba1c,
        max_hba1c=max_hba1c,
        latest_fbs=latest_fbs,
        latest_ppbs=latest_ppbs,
        antidiabetic_drugs=dm_drugs,
        reasons=(),
    )


def classify_all_patients(patients: List[UnifiedPatient]) -> List[DiabetesClassification]:
    return [classify_patient(p) for p in patients]
