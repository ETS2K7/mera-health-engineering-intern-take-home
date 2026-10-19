from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional


DATE_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d %b %y",
    "%d %b %Y",
)

HBA1C_RAW_NAMES = {"A1C", "HbA1c", "Glycated Hb"}
FASTING_GLUCOSE_RAW_NAMES = {"FBS", "Fasting Glucose", "Glucose (F)"}
PP_GLUCOSE_RAW_NAMES = {"PPBS", "Post Prandial Glucose", "Glucose (PP)"}


@dataclass(frozen=True)
class PatientRecord:
    mrn: str
    name: str
    dob: date
    sex: str
    phone: Optional[str]
    registered_on: date


@dataclass(frozen=True)
class VisitRecord:
    visit_id: str
    mrn: str
    visit_date: date
    raw_visit_date: str
    department: str
    doctor: str
    notes: str
    next_appointment: Optional[date]
    raw_next_appointment: Optional[str]


@dataclass(frozen=True)
class LabRecord:
    mrn: str
    raw_test_name: str
    canonical_test: str
    raw_value: float
    raw_unit: Optional[str]
    normalized_value: float
    normalized_unit: str
    reference_range: Optional[str]
    collected_on: date
    raw_collected_on: str
    unit_inferred: bool = False


@dataclass(frozen=True)
class PrescriptionRecord:
    mrn: str
    visit_id: str
    raw_drug: str
    canonical_drug: str
    dose: str
    prescribed_on: date
    raw_prescribed_on: str


@dataclass(frozen=True)
class ExportDataset:
    patients: Dict[str, PatientRecord]
    visits: List[VisitRecord]
    lab_results: List[LabRecord]
    prescriptions: List[PrescriptionRecord]


def clean_str(val: Optional[str]) -> Optional[str]:
    if val is None:
        return None
    stripped = val.strip()
    return stripped if stripped else None


def parse_date(val: Optional[str]) -> Optional[date]:
    cleaned = clean_str(val)
    if cleaned is None:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date format: {val!r}")


def ifcc_mmol_mol_to_ngsp_pct(mmol_mol: float) -> float:
    """Convert IFCC HbA1c (mmol/mol) to NGSP HbA1c (%): NGSP = 0.09148 * IFCC + 2.152."""
    return round(0.09148 * mmol_mol + 2.152, 1)


def glucose_mmol_l_to_mg_dl(mmol_l: float) -> float:
    """Convert blood glucose from mmol/L to mg/dL: mg/dL = mmol/L * 18.0182."""
    return round(mmol_l * 18.0182, 1)


def normalize_lab_row(row: Dict[str, str]) -> LabRecord:
    mrn = clean_str(row.get("mrn")) or ""
    raw_test_name = clean_str(row.get("test_name")) or ""
    raw_val_str = clean_str(row.get("value"))
    if raw_val_str is None:
        raise ValueError(f"Missing lab value for MRN {mrn}, test {raw_test_name}")
    raw_value = float(raw_val_str)
    raw_unit = clean_str(row.get("unit"))
    reference_range = clean_str(row.get("reference_range"))
    raw_collected = clean_str(row.get("collected_on")) or ""
    collected_on = parse_date(raw_collected)
    if collected_on is None:
        raise ValueError(f"Missing collected_on date for MRN {mrn}")

    unit_inferred = False

    if raw_test_name in HBA1C_RAW_NAMES:
        canonical_test = "HbA1c"
        normalized_unit = "%"
        if raw_unit == "mmol/mol" or (raw_unit is None and raw_value >= 20.0):
            if raw_unit is None:
                unit_inferred = True
            normalized_value = ifcc_mmol_mol_to_ngsp_pct(raw_value)
        elif raw_unit == "%" or (raw_unit is None and raw_value < 20.0):
            if raw_unit is None:
                unit_inferred = True
            normalized_value = round(raw_value, 1)
        else:
            raise ValueError(f"Unexpected HbA1c unit {raw_unit!r} for value {raw_value}")

    elif raw_test_name in FASTING_GLUCOSE_RAW_NAMES:
        canonical_test = "FBS"
        normalized_unit = "mg/dL"
        if raw_unit == "mmol/L" or (raw_unit is None and raw_value < 35.0):
            if raw_unit is None:
                unit_inferred = True
            normalized_value = glucose_mmol_l_to_mg_dl(raw_value)
        else:
            if raw_unit is None:
                unit_inferred = True
            normalized_value = round(raw_value, 1)

    elif raw_test_name in PP_GLUCOSE_RAW_NAMES:
        canonical_test = "PPBS"
        normalized_unit = "mg/dL"
        if raw_unit == "mmol/L" or (raw_unit is None and raw_value < 35.0):
            if raw_unit is None:
                unit_inferred = True
            normalized_value = glucose_mmol_l_to_mg_dl(raw_value)
        else:
            if raw_unit is None:
                unit_inferred = True
            normalized_value = round(raw_value, 1)

    else:
        canonical_test = raw_test_name
        normalized_value = raw_value
        default_units = {
            "Creatinine": "mg/dL",
            "Hb": "g/dL",
            "LDL": "mg/dL",
            "Platelet Count": "lakh/cumm",
            "Urine Microalbumin": "mg/L",
            "Vitamin B12": "pg/mL",
        }
        if raw_unit is not None:
            normalized_unit = raw_unit
        else:
            unit_inferred = True
            normalized_unit = default_units.get(raw_test_name, "")

    return LabRecord(
        mrn=mrn,
        raw_test_name=raw_test_name,
        canonical_test=canonical_test,
        raw_value=raw_value,
        raw_unit=raw_unit,
        normalized_value=normalized_value,
        normalized_unit=normalized_unit,
        reference_range=reference_range,
        collected_on=collected_on,
        raw_collected_on=raw_collected,
        unit_inferred=unit_inferred,
    )


def normalize_drug_name(raw_drug: str) -> str:
    """Strip dosage-form prefixes (Tab., T., Inj., Syp) for consistent comparison."""
    cleaned = raw_drug.strip()
    prefixes = ("Tab. ", "T. ", "TAB ", "Inj. ", "Syp ")
    for prefix in prefixes:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
            break
    return cleaned


def load_export(export_dir: str | Path = "export") -> ExportDataset:
    base = Path(export_dir)

    patients: Dict[str, PatientRecord] = {}
    with open(base / "patients.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mrn = clean_str(row["mrn"]) or ""
            dob = parse_date(row["dob"])
            reg = parse_date(row["registered_on"])
            if dob is None or reg is None:
                raise ValueError(f"Missing dob or registered_on for patient {mrn}")
            patients[mrn] = PatientRecord(
                mrn=mrn,
                name=clean_str(row["name"]) or "",
                dob=dob,
                sex=clean_str(row["sex"]) or "",
                phone=clean_str(row.get("phone")),
                registered_on=reg,
            )

    visits: List[VisitRecord] = []
    with open(base / "visits.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw_vdate = clean_str(row["visit_date"]) or ""
            vdate = parse_date(raw_vdate)
            if vdate is None:
                raise ValueError(f"Missing visit_date for visit {row.get('visit_id')}")
            raw_next = clean_str(row.get("next_appointment"))
            next_appt = parse_date(raw_next)
            visits.append(
                VisitRecord(
                    visit_id=clean_str(row["visit_id"]) or "",
                    mrn=clean_str(row["mrn"]) or "",
                    visit_date=vdate,
                    raw_visit_date=raw_vdate,
                    department=clean_str(row["department"]) or "",
                    doctor=clean_str(row["doctor"]) or "",
                    notes=clean_str(row.get("notes")) or "",
                    next_appointment=next_appt,
                    raw_next_appointment=raw_next,
                )
            )

    lab_results: List[LabRecord] = []
    with open(base / "lab_results.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lab_results.append(normalize_lab_row(row))

    prescriptions: List[PrescriptionRecord] = []
    with open(base / "prescriptions.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw_pdate = clean_str(row["prescribed_on"]) or ""
            pdate = parse_date(raw_pdate)
            if pdate is None:
                raise ValueError(f"Missing prescribed_on for MRN {row.get('mrn')}")
            raw_drug = clean_str(row["drug"]) or ""
            prescriptions.append(
                PrescriptionRecord(
                    mrn=clean_str(row["mrn"]) or "",
                    visit_id=clean_str(row["visit_id"]) or "",
                    raw_drug=raw_drug,
                    canonical_drug=normalize_drug_name(raw_drug),
                    dose=clean_str(row.get("dose")) or "",
                    prescribed_on=pdate,
                    raw_prescribed_on=raw_pdate,
                )
            )

    return ExportDataset(
        patients=patients,
        visits=visits,
        lab_results=lab_results,
        prescriptions=prescriptions,
    )
