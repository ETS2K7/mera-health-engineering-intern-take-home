# Decisions & Data Audit (`DECISIONS.md`)

## 1. Definitions Chosen for Terms in the Email

### A. Reference Date ("Today"): `2026-09-30`
- **Definition:** We anchor all "overdue" and "upcoming" calculations to **`2026-09-30`**, the day after the last clinical activity across `patients.csv`, `visits.csv`, and `prescriptions.csv` (`2026-09-29`).
- **Why:** Measuring against a laptop's wall-clock date would distort follow-up windows on a static export.

### B. "Diabetic Patient"
There is no diagnosis table in the export. Across 296 deduplicated patients (300 raw MRNs), we separate patients with diabetes signals into explicit tiers rather than lumping borderline cases into the front-desk call list:
1. **Confirmed Type 1 / Type 2 Diabetes (`85` unique patients / `88` MRNs) — Eligible for Primary Call List:**
   - **Included:** Patients seen in `Diabetology` (`54` MRNs), `General Medicine` with documented T2DM/NIDDM/DM2 notes and antidiabetic prescriptions (`31` MRNs), or `Paediatrics` with Type 1 / juvenile diabetes on basal-bolus insulin (`3` MRNs: `SVH025870`, `SVH028258`, `SVH029838`).
   - **Excluded False Positives:**
     - **PCOS/PCOD on Metformin (`4` patients):** `SVH025191`, `SVH028876`, `SVH029693`, and `SVH030554` are young women in `OBG` prescribed Metformin (`500 mg`) for PCOS with normal HbA1c (`5.0%–5.4%`). A drug-only rule would wrongly tell them they are diabetic.
     - **Negated Notes & Family History Only (`8` patients):** Notes stating `"No h/o diabetes"`, `"Denies diabetes"`, `"Non-diabetic"`, or `"Father/Mother diabetic"` with normal glucose (`SVH025407`, `SVH028557`, `SVH030471`, `SVH026328`, `SVH025828`, `SVH026131`, `SVH030117`, `SVH025378`).
     - **Prediabetes / IFG / IGT (`4` patients) & Resolved Borderline HbA1c (`3` patients):** `SVH024560`, `SVH024742`, `SVH025926`, `SVH028864` (`HbA1c 5.8%–6.2%`), plus `SVH025442`, `SVH029446`, `SVH029638` whose initial borderline `HbA1c` (`6.5%–6.6%`) normalized to `5.7%–5.9%` on lifestyle modification without medication.
2. **Gestational Diabetes (`3` patients in `OBG`) — Tracked in Dedicated Postpartum Recall Tab:**
   - Separated from chronic T1DM/T2DM because pregnancy-induced hyperglycemia often resolves at delivery (`SVH028326`, postpartum `HbA1c 5.4%`), whereas `SVH027966` (`HbA1c 6.6%` post-delivery) and `SVH028068` (advised 6-week postpartum OGTT, never done) need postpartum follow-up.
3. **Unconfirmed / Untreated Lab-Diabetic (`7` unique patients / `8` MRNs) — Flagged for Doctor Review:**
   - Patients with frank diabetic-range `HbA1c` (`6.8%–8.2%`) who were told `"Review with reports"` or `"adv diet, rpt"` and have no formal diagnosis or prescription recorded yet.

### C. "Follow-Up" and "Overdue"
- **What counts as a diabetes follow-up visit:** Visits to `Diabetology`, `Paediatrics` (for T1DM), or `General Medicine` where diabetes/sugars/OHAs/insulin were reviewed. Visits to **unrelated departments** (`Orthopaedics` knee/back pain, `Dermatology` scabies, `ENT` ear wax, `Dental` scaling) or acute non-diabetes `General Medicine` visits (URTI, gastroenteritis, gout, UTI, hypothyroidism) **do not reset the diabetes follow-up clock** (`5` overdue patients had recent unrelated visits that would hide them under a naive "last visit" rule: `SVH026153`, `SVH026193`, `SVH027082`/`SVH030772`, `SVH027261`, `SVH027925`).
- **When a confirmed diabetic is "Overdue" (`45` active patients):**
  - **Priority 1 — Missed Scheduled Appointment (`26` patients):** The patient's last diabetes visit recorded an explicit `next_appointment` before `2026-09-30` (`10` to `685` days ago) and they have not returned for diabetes care since.
  - **Priority 2 — Routine Overdue (`19` patients):** `next_appointment` was left blank, and time elapsed since the last diabetes visit exceeds the doctor's written review interval in `notes` (e.g., `"Review 6 wks"`, `"Review 2 months"`) or the **90-day (3-month) standard diabetes follow-up threshold** (per ICMR / ADA guidelines for quarterly HbA1c/glycemic review).
- **Not Overdue (`34` active patients):** `18` have a future `next_appointment` booked after `2026-09-30`, and `16` were seen within the last 90 days (and within their doctor's note interval).

*(External references consulted: ADA Standards of Care & ICMR Guidelines for Management of Type 2 Diabetes for diagnostic thresholds—HbA1c $\ge 6.5\%$, FBS $\ge 126\text{ mg/dL}$, PPBS $\ge 200\text{ mg/dL}$, routine 3-month follow-up—and the NGSP/IFCC master equation $\text{NGSP \%} = 0.09148 \times \text{IFCC mmol/mol} + 2.152$.)*

---

## 2. Data Problems Found and How We Handled Them

1. **Deceased (`3`) and Transferred (`3`) Patients Still in Export:**
   - **Problem:** Clinical notes record that **3 diabetic patients died** (`SVH025339` D. Rosy expired at home; `SVH026086` Narayana Moolya expired in ICU with sepsis/DKA; `SVH025698` Joseph Philip expired in `Emergency` from STEMI) and **3 transferred care / relocated** (`SVH024860` to Hyderabad, `SVH025512` to native place, `SVH028346` to Pune).
   - **Action:** Scanned notes across **all** departments (catching `SVH025698`'s death in `Emergency`), suppressed all 6 from the call list, and isolated them in `quarantined_patients.csv` and Tab 4.
2. **Duplicate Patient Registrations Across MRNs (`4` pairs = `8` MRNs):**
   - **Problem:** Four patients were registered twice under slightly different name spellings with the exact same `dob`, `sex`, and `phone`:
     - `SVH025473` (`Md. Rafiq`) & `SVH029873` (`Mohammed Rafiq`) — DOB `1976-08-02`, phone `9000037901`
     - `SVH027082` (`Padmavathi Reddy`) & `SVH030772` (`Padma Reddy`) — DOB `1959-05-25`, phone `9000040001`
     - `SVH027863` (`Joseph Dsouza`) & `SVH029322` (`Joseph D'Souza`) — DOB `1977-08-08`, phone `9000098302`
     - `SVH027925` (`Shivakumar`) & `SVH029107` (`Shivakumar H M`) — DOB `1967-08-18`, phone `9000053186`
   - **Action:** Linked records sharing `(dob, sex, phone)` into a unified timeline so nobody is called twice or evaluated on half a chart. Conversely, we preserved homonyms with different DOBs/phones (`Ravi Kumar`, `Lakshmi Devi`, `Sunitha Rao`) as separate people.
3. **Mixed Lab Names, Mixed Units (IFCC vs. NGSP, mmol/L vs. mg/dL), and Missing Units:**
   - **Problem:** `lab_results.csv` logs HbA1c as `A1C`, `HbA1c`, or `Glycated Hb` in `%` or `mmol/mol` (`17` rows), and leaves `unit` blank on `3` HbA1c rows with values `65`, `65`, `68` (`SVH029659`, `SVH027596`, `SVH029625`). Glucose is logged in both `mg/dL` and `mmol/L` (`10` rows). `SVH027221` also has an exact duplicate `HbA1c = 6.8%` row on `11/12/2025`.
   - **Action:** Standardized test names and converted IFCC `mmol/mol` (including unlabelled values $\ge 20$) to NGSP `%` ($65 \rightarrow 8.1\%$, $68 \rightarrow 8.4\%$) and glucose `mmol/L` to `mg/dL` ($\times 18.0182$), while flagging inferred units and duplicate rows.
4. **Inconsistent Date Formats & Impossible / Typo Dates:**
   - **Problem:** `lab_results.csv` mixes `D Mon YY` (`2 Oct 23`) and `DD/MM/YYYY` (`17/11/2025`). Two diabetic lab rows have dates after the export (`19/12/2026` FBS on `SVH027355` and `27/02/2027` HbA1c on `SVH030138`), and one non-diabetic visit is dated `2015-03-28` (`V042486` on `SVH029560`).
   - **Action:** Normalized all date formats in `src/parser.py`, excluded future-dated lab typos from current lab summaries, and flagged them for chart verification.
5. **Missing Phone Numbers (`8` total, `3` on Overdue Call List) & Shared Household Phones (`3` numbers):**
   - **Problem:** `SVH028319` (`Geetha Gowda`), `SVH028414` (`Mohan Hiremath`), and `SVH025063` (`Ganesh Iyengar`) are overdue confirmed diabetics with blank phone numbers. Additionally, phone `9000043929` is shared by two different diabetic patients (`SVH026167` Revathi Pandian and `SVH029084` Meena P) plus `SVH030711`; `9000090725` (`SVH030327`) and `9000032917` (`SVH028463`) are also shared family numbers.
   - **Action:** Kept the 3 missing-phone patients on the overdue list with an explicit `"MISSING PHONE (Check Front Desk Chart)"` warning, and labeled shared household numbers so staff ask for the specific patient by name and age.

---

## 3. Where We Flagged Items for Human Check Rather Than Deciding Silently

1. **7 Unconfirmed Lab-Diabetic Patients (`8` MRNs in Tab 3 / `flagged_for_review.csv`):** `SVH025473`+`SVH029873` (`Md. Rafiq`, HbA1c `8.1%`), `SVH026798` (`7.9%`), `SVH027221` (`6.8%`), `SVH027596` (`8.1%`), `SVH029597` (`8.1%`), `SVH029659` (`8.1%`), and `SVH029830` (`7.8%`). Calling someone "for their diabetes follow-up" when a doctor has not yet communicated a diagnosis could cause distress, yet leaving HbA1c ~8% unfollowed is unsafe. We separated them for doctor sign-off on calling them to review pending lab reports.
2. **2 Postpartum GDM Patients (`Tab 2`):** `SVH027966` (`Yashoda Naidu`, Metformin stopped at delivery, later `HbA1c 6.6%` with no visit) and `SVH028068` (`Gowramma Acharya`, advised 6-week postpartum OGTT, no follow-up).
3. **3 Overdue Patients with Mid-September 2026 Labs but No Logged Visit:** `SVH025030` (`HbA1c 9.4%` on `2026-09-03`), `SVH026535` (`HbA1c 8.9%` on `2026-09-16`), and `SVH026465` (`HbA1c 8.8%` on `2026-09-17`) had blood drawn in Sept 2026 with no corresponding clinic visit in `visits.csv`. We kept them on the call list but flagged that the front desk should verify whether they recently saw the doctor without a visit entry being logged, or if they need to come in to review those high September results.

---

## 4. What Our Result Cannot Be Trusted For

- **Unrecorded visits, outside care, or unlogged deaths:** As seen with the 3 patients who had lab draws in September 2026 without a matching row in `visits.csv`, if a consultation was only written on paper or if a patient passed away / moved without a hospital note, the export cannot reflect it.
- **Medication adherence or current prescriptions:** `prescriptions.csv` only logs when a drug was newly written or changed (many `"Cont same Rx"` visits have no new prescription rows), so the active medication column reflects the last recorded prescription, not verified pharmacy dispensing.
- **Duplicate patients without matching phone/DOB:** Our deduplication links patients sharing `(dob, sex, phone)`. If a patient re-registered with a new phone number or a mistyped DOB, they cannot be automatically merged with certainty.
- **Clinical triage of complications:** While the tool surfaces HbA1c and symptom notes (e.g., `"burning soles"`, `"fundus check due"`, high microalbumin), it is an operational scheduling aid and cannot replace physician review.
