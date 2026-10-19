# Plan

## 1. What We Need to Define
Meenakshi’s email asks for *"diabetic patients who are overdue for follow-up so the front desk can call them."* None of those terms are explicitly encoded in the database, so before building anything we need to establish:
- **Reference date ("today"):** Regular export activity ends on `2026-09-29`, so we must anchor "overdue" to the export date rather than wall-clock time.
- **"Diabetic patient":** There is no diagnosis table. Signals live across `visits` (`Diabetology` department, free-text notes like `k/c/o DM`), `prescriptions` (OHAs, insulin), and `lab_results` (`A1C`, fasting/PP glucose). We need to see how these overlap and identify edge cases (e.g., prediabetes, gestational diabetes, Metformin for PCOS, negated notes like "no h/o DM", or isolated high lab results).
- **"Follow-up" and "Overdue":** Does a visit to Orthopaedics or Dermatology count as follow-up, or only diabetes-related visits/labs? And how is the due date set when `next_appointment` is blank on ~71% of visits (even when notes say *"review 1 month"*)?

## 2. What We Need to Find Out First (Data Audit)
Before writing application code, we will audit the 4 CSVs for:
1. **Cohort boundaries:** Map every patient with any diabetes-related visit, note, drug, or lab result, and inspect the borderline cases by hand.
2. **Follow-up patterns:** Compare explicit `next_appointment` dates, follow-up intervals written in `visits.notes`, and clinical defaults (e.g., 3–6 month HbA1c/visit window).
3. **Data quality & safety hazards:**
   - Duplicate patients across different MRNs (same phone/name/DOB).
   - Impossible or inconsistent dates (`2015` visit date, `2027` lab date, mixed date formats in `lab_results.csv`).
   - Patients who should **not** be called (deceased, transferred, admitted, or already booked for a future visit) or cannot be called (missing/invalid phone numbers).

## 3. What We're Unsure About
- Whether the doctors want borderline patients (unconfirmed high HbA1c/glucose, prediabetes, GDM) included on the call list or separated for doctor review first.
- What grace period past a missed appointment date (or default interval when no date was recorded) the hospital considers "overdue" enough to call.
- Whether `General Medicine` doctors manage diabetes follow-ups alongside `Diabetology`.

## 4. Approach & Deliverable
1. Run an exploratory audit of the CSVs and document every definition, edge case, and data defect in `DECISIONS.md`.
2. Separate confident "Overdue — Ready to Call" patients from "Needs Review / Data Issue" cases rather than guessing silently.
3. Build a lightweight, zero-dependency local tool (one command to run) that gives the front desk a clear call list and gives doctors the clinical context behind every row, plus draft `reply.txt`.
