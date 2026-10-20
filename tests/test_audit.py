from __future__ import annotations

import unittest
from datetime import date

from src.classifier import classify_all_patients
from src.overdue import REFERENCE_DATE, evaluate_all_patients
from src.parser import load_export
from src.safety import build_unified_patients


class TestAuditInvariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_export("export")
        cls.unified = build_unified_patients(cls.dataset)
        cls.classified = classify_all_patients(cls.unified)
        cls.evaluated = evaluate_all_patients(cls.classified, reference_date=REFERENCE_DATE)

    def test_1_ingestion_counts_and_normalization(self) -> None:
        self.assertEqual(len(self.dataset.patients), 300)
        self.assertEqual(len(self.dataset.visits), 815)
        self.assertEqual(len(self.dataset.lab_results), 425)
        self.assertEqual(len(self.dataset.prescriptions), 540)

        # Verify unlabelled IFCC HbA1c values (65, 68) are converted to %
        inferred_a1c = [
            l for l in self.dataset.lab_results
            if l.canonical_test == "HbA1c" and l.unit_inferred
        ]
        self.assertEqual(len(inferred_a1c), 3)
        for lab in inferred_a1c:
            self.assertEqual(lab.normalized_unit, "%")
            self.assertIn(lab.normalized_value, (8.1, 8.4))

    def test_2_deceased_and_transferred_suppressed_from_call_list(self) -> None:
        deceased = [e for e in self.evaluated if e.status == "SUPPRESSED_DECEASED"]
        transferred = [e for e in self.evaluated if e.status == "SUPPRESSED_TRANSFERRED"]

        self.assertEqual(
            {e.classification.patient.primary_mrn for e in deceased},
            {"SVH025339", "SVH025698", "SVH026086"},
        )
        self.assertEqual(
            {e.classification.patient.primary_mrn for e in transferred},
            {"SVH024860", "SVH025512", "SVH028346"},
        )

        call_list_mrns = {
            mrn
            for e in self.evaluated
            if e.include_on_call_list
            for mrn in e.classification.patient.all_mrns
        }
        for e in deceased + transferred:
            self.assertFalse(e.include_on_call_list)
            for mrn in e.classification.patient.all_mrns:
                self.assertNotIn(mrn, call_list_mrns)

    def test_3_pcos_on_metformin_excluded(self) -> None:
        pcos_excluded = [
            c for c in self.classified if c.category == "EXCLUDED_PCOS"
        ]
        self.assertEqual(
            {c.patient.primary_mrn for c in pcos_excluded},
            {"SVH025191", "SVH028876", "SVH029693", "SVH030554"},
        )
        for c in pcos_excluded:
            self.assertFalse(c.is_confirmed_diabetic)
            self.assertFalse(c.is_borderline_review)
            self.assertLess(c.max_hba1c or 0.0, 5.7)

    def test_4_duplicate_mrn_pairs_merged(self) -> None:
        self.assertEqual(len(self.unified), 296)
        merged = [u for u in self.unified if u.is_duplicate_merged]
        self.assertEqual(len(merged), 4)
        self.assertEqual(
            {u.all_mrns for u in merged},
            {
                ("SVH025473", "SVH029873"),
                ("SVH027082", "SVH030772"),
                ("SVH027863", "SVH029322"),
                ("SVH027925", "SVH029107"),
            },
        )


if __name__ == "__main__":
    unittest.main()
