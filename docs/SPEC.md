# Preflight: functional specification

This document turns the business need into testable rules and links each requirement to the code and tests that implement it.

## 1. Business problem

A regulatory reporting provider (managed service or in-house team) files periodic fund returns for many clients. Each client sends its fund data in its own spreadsheet: different column names, languages, date formats, units (euros or thousands) and percentage conventions. Today analysts translate every file by hand, check it against the reporting rules, chase the client by email and keep evidence of who checked what.

**Goal:** translate each client's format once and reuse it, check every value automatically, keep the client conversation and the corrections traceable, and require a second person's sign-off before filing.

## 2. Actors

| Role | Can | Cannot |
|---|---|---|
| Reporting analyst (maker) | Upload files, confirm mappings, correct values with a reason, justify warnings, record client queries, submit | Approve or return a filing |
| Reviewer (checker) | Approve or return filings in review | Prepare filings; review a filing they took part in |
| Admin | Everything | Review a filing they took part in |

## 3. Requirements

### R1. Intake
- R1.1 Accept `.xlsx` and `.csv` (comma, semicolon or tab, UTF-8 or Windows-1252), up to 5 MB and 2,000 rows.
- R1.2 Store every file version with its SHA-256 fingerprint; refuse a version identical to the current one.
- R1.3 One filing per client, period and template.

Code: `app/services/ingest.py`, `workflow.create_filing / add_version`. Tests: `test_workflow.py`, `test_api_and_pages.py`.

### R2. Column mapping
- R2.1 Columns confirmed earlier for the same client are reused automatically. If every column is known, the file goes straight to checking.
- R2.2 Unknown columns get a proposal (field, unit conversion, confidence, reason) from Claude, or from the offline matcher (aliases in English and French, fuzzy matching) when no API key is set or the call fails.
- R2.3 Proposals are never applied without an analyst's confirmation. Each template field can be fed by one column only.
- R2.4 Mappings are scoped to one client.

Code: `app/services/mapping.py`. Tests: `test_mapping.py`.

### R3. Normalisation
- R3.1 Numbers in European and English formats; dates in common European, ISO and written formats; currency names to ISO codes; strategy synonyms to the allowed values.
- R3.2 Unit conversions: thousands, millions, fraction to percent.
- R3.3 Unreadable values are reported (rule FMT-VALUE), never dropped.

Code: `app/services/normalize.py`. Tests: `test_template_and_normalize.py`.

### R4. Validation rules (demo template)
| Rule | Severity | Check |
|---|---|---|
| REQ-FIELD | Error | Mandatory field present |
| FMT-VALUE | Error | Value readable for its type |
| LEI-CHECKSUM | Error | ISO 17442 check digits; suggests the LEI from the fund register |
| LEI-REGISTER | Warning | LEI known in the client's fund register |
| LEI-UNIQUE | Error | One row per fund |
| PERIOD-MATCH | Error | Period end equals the filing period |
| CCY-ISO | Error | ISO 4217 currency |
| STRATEGY-ALLOWED | Error | Strategy in the allowed list |
| NAV-POSITIVE | Error | NAV > 0 |
| PCT-RANGE, LEV-MIN | Error | Percentages within range |
| LIQ-SUM | Error | Liquidity buckets total 100% (±0.5) |
| INV-COUNT | Error | At least one investor |
| INV-CONCENTRATION | Error | Largest investor ≤ top 5 investors |
| LEV-ORDER | Warning | Commitment leverage ≤ gross leverage |
| LEV-HIGH | Warning | Gross leverage ≤ 300% |
| NAV-JUMP | Warning | NAV change vs last approved period (or the register's last NAV) ≤ 25% |

Rules are configuration in `app/template_defs/fund_return_v1.yaml`, each with a reviewer explanation and a client-facing question. The template is a demonstration slice, not an official regulatory template.

Code: `app/services/validation.py`. Tests: `test_validation.py`.

### R5. Resolution
- R5.1 Corrections require a reason, are applied on top of the client's data (the original value is kept) and re-run every check.
- R5.2 A correction carries over to a new version only if the client still sends the same original value.
- R5.3 Only warnings can be justified; justifications carry over to a new version when the value is unchanged.
- R5.4 The client query groups all open points by fund, written as questions; recording it marks those points as asked.
- R5.5 New versions show which values changed.

### R6. Four-eyes control
- R6.1 Submission requires zero errors and every warning justified.
- R6.2 Anyone who took part in preparing a filing (upload, mapping, correction, justification, query, submission) cannot approve or return it, whatever their role.
- R6.3 Returning requires a comment. The filing is locked while in review and after approval.

Code: `app/services/workflow.py`. Tests: `test_workflow.py`.

### R6b. Client sign-off and filing
- R6b.1 Only an internally approved filing can be sent to the client; the analyst records the message as sent.
- R6b.2 The analyst records the client's answer with the name of the person who gave it. Approval moves the filing to "client approved". A change request requires a description, sends the filing back to the analyst, and the filing must pass the four-eyes review again.
- R6b.3 A filing can be recorded as filed with the regulator only after client approval, with the submission reference. The application records the submission; it does not perform it.
- R6b.4 Content is locked from internal approval onwards; the filing file can be downloaded from internal approval onwards.

Code: `workflow.send_to_client / record_client_decision / record_filed`. Tests: `test_signoff.py`.

### R7. Outputs and evidence
- R7.1 After approval: the filing file (CSV, template order, normalised values).
- R7.2 Validation record: files and fingerprints, checks and resolutions, corrections, client queries, sign-off and event log.
- R7.3 Hash-chained audit log; any edit of a past entry is detected.
- R7.4 Data-quality indicators: issues in clients' first files, first-time-right rate, most frequent issue per client.

## 4. Acceptance scenario (demo)
1. Alice uploads `northstar_fund_return_2026-06-30.xlsx`. 16 of 17 columns are proposed correctly (the internal comment is not needed); "(EUR k)" is detected as thousands.
2. After confirmation: 3 errors (invalid LEI, liquidity at 98%, largest investor above top 5) and 2 warnings (commitment above gross leverage, NAV +45.2%).
3. Alice applies the LEI from the fund register, records a client query, and uploads the corrected `..._v2.xlsx`: 2 changed values, 0 errors, LEI correction kept.
4. Alice justifies the 2 warnings and submits. She cannot approve.
5. Claire approves. The CSV and validation record are available. The audit chain is intact.
6. Alice sends it to Northstar for sign-off, records Laura Weiss's approval, then records the CSSF submission reference. The filing is complete.
7. Alice uploads `northstar_fund_return_2026-09-30.xlsx`: no mapping step, no findings.
