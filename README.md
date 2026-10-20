# Diabetic Follow-Up & Front-Desk Recall Workbench

A zero-dependency local tool (Python 3 standard library only) that audits the hospital export in `export/`, generates CSV call lists, and serves an interactive front-desk & clinician dashboard.

## Quick Start (1 Command)

Run the dashboard and automatically generate all CSV files:

```bash
python3 app.py
```

Then open **`http://localhost:8000`** in your browser.

### Headless CSV Export Only

To generate `overdue_call_list.csv`, `flagged_for_review.csv`, and `quarantined_patients.csv` without starting the web server:

```bash
python3 app.py --export
```

### Run Automated Audit & Safety Invariant Tests

```bash
python3 -m unittest discover -s tests -v
```

## Key Files

- **`PLAN.md`** — Initial breakdown and audit plan committed before writing code.
- **`DECISIONS.md`** — Definitions chosen (`2026-09-30` reference date, confirmed vs. borderline diabetes, diabetes-specific follow-up urgency), data defects handled, items flagged for clinical review, and limitations.
- **`reply.txt`** — Plain-English reply to Meenakshi (<200 words).
- **`app.py`** — Single-command CLI exporter and local web dashboard (`http://localhost:8000`).
- **`src/parser.py`** — CSV ingestion, multi-format date parsing, and HbA1c/glucose unit normalization.
- **`src/safety.py`** — Deceased/transferred patient suppression, duplicate MRN merging, and missing/shared phone detection.
- **`src/classifier.py`** — Cohort classification (T1DM, T2DM, GDM, unconfirmed lab hyperglycemia, PCOS exclusions, prediabetes).
- **`src/overdue.py`** — Priority 1 (missed appointment) & Priority 2 (routine overdue) evaluation and anomaly tracking.
- **`tests/test_audit.py`** — Unit tests verifying ingestion counts, safety exclusions, PCOS filtering, and duplicate merging.
