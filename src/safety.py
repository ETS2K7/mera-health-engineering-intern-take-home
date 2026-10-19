from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from src.parser import (
    ExportDataset,
    LabRecord,
    PatientRecord,
    PrescriptionRecord,
    VisitRecord,
)


DECEASED_PATTERN = re.compile(
    r"\b(expired|deceased|death summary|declared expired|passed away)\b",
    re.IGNORECASE,
)

TRANSFERRED_PATTERN = re.compile(
    r"\b(transfer of care|transferred care|transfer summary|relocated to|relocating to)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SafetyFlag:
    status: str  # "DECEASED" or "TRANSFERRED"
    mrn: str
    visit_id: str
    visit_date: date
    department: str
    note_excerpt: str


@dataclass(frozen=True)
class UnifiedPatient:
    primary_mrn: str
    all_mrns: Tuple[str, ...]
    primary_name: str
    all_names: Tuple[str, ...]
    dob: date
    sex: str
    phone: Optional[str]
    missing_phone: bool
    shared_phone_mrns: Tuple[str, ...]
    earliest_registered_on: date
    latest_registered_on: date
    is_duplicate_merged: bool
    visits: Tuple[VisitRecord, ...]
    lab_results: Tuple[LabRecord, ...]
    prescriptions: Tuple[PrescriptionRecord, ...]
    deceased_flag: Optional[SafetyFlag]
    transferred_flag: Optional[SafetyFlag]

    @property
    def do_not_call(self) -> bool:
        return self.deceased_flag is not None or self.transferred_flag is not None

    @property
    def safety_status(self) -> str:
        if self.deceased_flag is not None:
            return "DECEASED"
        if self.transferred_flag is not None:
            return "TRANSFERRED"
        return "ACTIVE"


def detect_visit_safety_flags(visits: List[VisitRecord]) -> Tuple[Optional[SafetyFlag], Optional[SafetyFlag]]:
    deceased_flag: Optional[SafetyFlag] = None
    transferred_flag: Optional[SafetyFlag] = None

    for v in sorted(visits, key=lambda x: (x.visit_date, x.visit_id)):
        if DECEASED_PATTERN.search(v.notes):
            deceased_flag = SafetyFlag(
                status="DECEASED",
                mrn=v.mrn,
                visit_id=v.visit_id,
                visit_date=v.visit_date,
                department=v.department,
                note_excerpt=v.notes,
            )
        if TRANSFERRED_PATTERN.search(v.notes):
            transferred_flag = SafetyFlag(
                status="TRANSFERRED",
                mrn=v.mrn,
                visit_id=v.visit_id,
                visit_date=v.visit_date,
                department=v.department,
                note_excerpt=v.notes,
            )

    return deceased_flag, transferred_flag


def build_unified_patients(dataset: ExportDataset) -> List[UnifiedPatient]:
    """
    Group duplicate patient records that share (dob, sex, non-empty phone) into a
    single UnifiedPatient with a merged clinical timeline, while flagging deceased/
    transferred status, missing phone numbers, and household-shared phone numbers.
    """
    visits_by_mrn: Dict[str, List[VisitRecord]] = defaultdict(list)
    for v in dataset.visits:
        visits_by_mrn[v.mrn].append(v)

    labs_by_mrn: Dict[str, List[LabRecord]] = defaultdict(list)
    for l in dataset.lab_results:
        labs_by_mrn[l.mrn].append(l)

    rx_by_mrn: Dict[str, List[PrescriptionRecord]] = defaultdict(list)
    for r in dataset.prescriptions:
        rx_by_mrn[r.mrn].append(r)

    # Group MRNs that represent the same person: identical (dob, sex, phone) when phone is present.
    groups: Dict[Tuple[str, ...], List[PatientRecord]] = defaultdict(list)
    for mrn, pat in sorted(dataset.patients.items()):
        if pat.phone is not None:
            key = ("DUP_KEY", pat.dob.isoformat(), pat.sex, pat.phone)
        else:
            key = ("SINGLE_MRN", mrn)
        groups[key].append(pat)

    # Identify phone numbers shared across DIFFERENT people (different DOB/sex groups)
    phone_to_group_keys: Dict[str, List[Tuple[str, ...]]] = defaultdict(list)
    for key, pat_list in groups.items():
        phone = pat_list[0].phone
        if phone is not None:
            phone_to_group_keys[phone].append(key)

    unified_list: List[UnifiedPatient] = []

    for key, pat_list in groups.items():
        pat_list_sorted = sorted(pat_list, key=lambda p: (p.registered_on, p.mrn))
        primary = pat_list_sorted[0]
        all_mrns = tuple(p.mrn for p in pat_list_sorted)
        all_names = tuple(dict.fromkeys(p.name for p in pat_list_sorted))

        merged_visits: List[VisitRecord] = []
        merged_labs: List[LabRecord] = []
        merged_rx: List[PrescriptionRecord] = []

        for m in all_mrns:
            merged_visits.extend(visits_by_mrn.get(m, []))
            merged_labs.extend(labs_by_mrn.get(m, []))
            merged_rx.extend(rx_by_mrn.get(m, []))

        merged_visits.sort(key=lambda v: (v.visit_date, v.visit_id))
        merged_labs.sort(key=lambda l: (l.collected_on, l.canonical_test))
        merged_rx.sort(key=lambda r: (r.prescribed_on, r.visit_id, r.canonical_drug))

        deceased_flag, transferred_flag = detect_visit_safety_flags(merged_visits)

        shared_phone_mrns: Tuple[str, ...] = ()
        if primary.phone is not None and len(phone_to_group_keys[primary.phone]) > 1:
            other_mrns: List[str] = []
            for other_key in phone_to_group_keys[primary.phone]:
                if other_key != key:
                    other_mrns.extend(p.mrn for p in groups[other_key])
            shared_phone_mrns = tuple(sorted(other_mrns))

        unified_list.append(
            UnifiedPatient(
                primary_mrn=primary.mrn,
                all_mrns=all_mrns,
                primary_name=primary.name,
                all_names=all_names,
                dob=primary.dob,
                sex=primary.sex,
                phone=primary.phone,
                missing_phone=primary.phone is None,
                shared_phone_mrns=shared_phone_mrns,
                earliest_registered_on=min(p.registered_on for p in pat_list_sorted),
                latest_registered_on=max(p.registered_on for p in pat_list_sorted),
                is_duplicate_merged=len(all_mrns) > 1,
                visits=tuple(merged_visits),
                lab_results=tuple(merged_labs),
                prescriptions=tuple(merged_rx),
                deceased_flag=deceased_flag,
                transferred_flag=transferred_flag,
            )
        )

    unified_list.sort(key=lambda u: u.primary_mrn)
    return unified_list
