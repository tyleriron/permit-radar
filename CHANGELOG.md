# Version 4.0.0 — 2026-11-14

This release supersedes the behavior described in the older entries below.

- Correct Austin permit codes: commercial BP includes building/demolition scope; DS is driveway/sidewalk and is excluded.
- Preserve full scope before classification. Add per-trade evidence and explicit/inferred dumpster fit; exclude incidental roof locations, electrical-only scope, private events and dumpster enclosures.
- Keep a complete current-window inventory alongside a separate new/changed feed. Preserve group member provenance; prevent suite bridges and overlapping area inflation.
- Make optional AI advisory only, opt-in, and unable to change buyer facts or eligibility.
- Gate customer sheets on source identity, contractor contact, active status and timing; export a review queue.
- Validate schema, refresh age, source counts, pagination completion and source revision. Stop before publication on incomplete or inconsistent downloads.
- Reject corrupt checkpoints; fingerprint all important source fields; reset approval on changes with delivery history retained.
- Stage exports, roll back caught publication errors, commit state last, lock concurrent runs, and include an integrity manifest plus verify_reports.py.
- Persist reports and state together in a private GitHub repository, add workflow artifacts containing both, weekday scheduling and a Windows/Linux test matrix.
- Add real-world and failure-recovery regressions; see VALIDATION.md for the actual checks performed.

---

# Changelog

## 2026-11-14 (V2.1) — Synthetic Test Audit Fixes

### 1. Suite-Safe & Identity-Strict Deduplication
- Added `_extract_suite_or_unit_designator()` so distinct suites/units (in `address` or `raw_description_clean`) at the same building, contractor, and application date never merge.
- Removed heuristic same-address/application-date Case C merges; `_can_merge_records()` now merges only on exact permit numbers (or matching `project_id` without conflicting suites when `permit_number` is missing) or verified shared `masterpermitnum` within the same 30-day filing/issuance window and contractor/address.

### 2. Per-Permit Checkpointing, Change Tracking & 4-Stage Persistence
- Replaced shared `project_id` skip logic with per-permit key tracking (`get_permit_key()` defaulting to `permit:{permit_number}`), so a newly issued permit sharing a previously seen `project_id` is never discarded.
- Added SHA-256 permit content fingerprinting (`compute_permit_fingerprint()`) and explicit lifecycle tracking across `collected` -> `processed` -> `approved` -> `delivered` (`record_collected_permits()`, `record_processed_sites()`, `mark_permits_approved()`, `mark_permits_delivered()`, `get_persistence_summary()`). Updated records re-enter processing automatically and log change history.
- Added `--approve-permits` and `--deliver-permits` CLI commands.

### 3. Multi-Clause Negation & Positive Override Handling
- Updated `CLAUSE_SPLIT_PATTERN` and `classify_rule_based()` so negated clauses strip only the negated span without globally blocking explicit affirmative clauses for the same trade (e.g., `"No roof repairs. Install new TPO roof membrane"` classifies as `Commercial Roofing & Exterior`).
- Added administrative `"no construction"` / `"certificate of occupancy only"` guard to route non-construction filings into `Unclassified / Needs Review`.

### 4. Strict `--as-of` Historical Window Boundaries
- Updated `fetch_raw_permits()` and `normalize_record()` to enforce both lower (`issue_date >= cutoff_date`) and upper (`issue_date <= as_of_date`) boundaries.

### 5. Pagination Truncation Detection & Failure Mode
- Added `pages_fetched` and `pagination_truncated` telemetry plus explicit truncation warnings when `--max-pages` is reached with a full page; added `--fail-on-truncation` (exit code `3`) and explicit `TRUNCATED / INCOMPLETE DATASET` dataset status reporting.

### 6. Official Source URL Validation & Manual Review Flagging
- Removed silent URL synthesis when Socrata `link` is missing (`Link Unavailable`).
- Verified official URLs against city `allowed_link_hosts` (`abc.austintexas.gov`), expected detail path, and `t_selected_folderrsn == project_id`, flagging mismatched or untrusted URLs as `Unverified Link — Flagged for Manual Review`.

### 7. Top-20 Export & Automated Test Expansion
- Added `output/austin_top_20_sample_sheet.csv` alongside `output/austin_top_15_sample_sheet.csv` and detailed pilot metrics (`latest_source_issue_date`, link counts, persistence counts).
- Expanded `tests/test_fetch_permits.py` to 19 automated tests proving suite isolation, shared `project_id` / changed permit retention, 4-stage persistence, multi-clause TPO roofing override, `--as-of` bounds, pagination truncation failure, source link mismatch/untrusted detection, missing/invalid fields, and multi-provider AI failure fallback.

---

## 2026-11-14 (V2) — Repair & Hardening Pass

### 1. Freshness Window & Incremental Checkpointing
- Added `--days` (default `14`), `--as-of` (`YYYY-MM-DD`), `--page-size` (default `100`), and `--max-pages` (default `5`) with paged Socrata fetching (`fetch_raw_permits`) and strict `issue_date` window filtering so old permits are never labeled as new.
- Added incremental checkpoint persistence at `state/checkpoint.json` (`DEFAULT_CHECKPOINT_PATH`, configurable via `--checkpoint`, `--ignore-checkpoint`, and `--no-save-checkpoint`) to track already-processed permit IDs across weekly runs (`checkpoint_skipped`).

### 2. Project-Safe Deduplication & Non-Additive Square Footage
- Replaced `deduplicate_sites()` with `deduplicate_projects()` so unrelated permits sharing an address and contractor across different years or scopes are no longer merged.
- Groups permits using verified project identifiers and consistent periods (`project_id`, `masterpermitnum`), preserving `member_permit_numbers`, `member_project_ids`, and `permit_count_at_site`.
- Stops blind `sqft` summation across non-additive permits or paged duplicates: uses `max_single_permit_sqft` (`Max Single Permit (Non-Additive Group)`) unless square footage is verifiably additive across distinct buildings/units, and `Single Permit` for duplicate `project_id` rows.

### 3. Negation-Aware Rule Classification & Unclassified Outcome
- Added `UNCLASSIFIED_TRADE = "Unclassified / Needs Review"` instead of defaulting unknown or irrelevant permits (such as shelving-only scopes) to `Dumpster & Demolition` and `Post-Construction Cleaning`.
- Added clause-level negation handling in `classify_rule_based` for patterns including `'no roofing work'`, `'no demolition'`, `'does not require dumpsters'`, and mixed affirmative/negative clauses so trade tags require supporting positive evidence (`classification_evidence`).

### 4. Recency-First Ranking
- Updated `site_rank_key` to rank leads primarily by `issue_date` recency, freshness, and actionable trade classification rather than letting keywords, phone availability, or square footage outrank recent jobs.

### 5. URL Validation & Full Data Provenance
- Added URL validation for Socrata `link` values (emitting `Link Unavailable` when missing or invalid instead of passing bad links through).
- Expanded `MASTER_COLUMNS` to 32 columns preserving `applied_date`, `issue_date`, `timing_status`, `status_current`, `permit_number`, `member_permit_numbers`, `project_id`, `member_project_ids`, `masterpermitnum`, `jurisdiction`, `contact_provenance`, `source_url`, and `raw_description_clean`.
- Explicitly distinguishes contractor contact fields from applicant contact fallback via `contact_provenance`, and labels missing values explicitly (e.g., `"Not Listed"` / `"Not Provided"` / `"Link Unavailable"`) without fabricating fields.

### 6. Buyer-Facing Summaries Separated from Contractor Openers
- Replaced the contractor-addressed `sales_hook` field with a buyer-facing `buyer_brief` tailored to Permit Radar subscribers (suppliers, dumpster rentals, fencing, roofing, and cleaning vendors) plus a separate `optional_gc_call_opener` without claiming unverified project needs.

### 7. Verified Austin Socrata Schema (`3syk-w9eu`)
- Verified field mappings against the live City of Austin `Issued Construction Permits` Socrata metadata (`https://data.austintexas.gov/api/views/3syk-w9eu.json`, 78 BLDS-compliant columns).
- Preserved both `applieddate` (`applied_date`) and `issue_date` to surface multi-year application-to-issuance lags (`timing_status`) without filtering out valid permits over Austin's `total_job_valuation` (`0` or `5`) quirk.

### 8. Retry Handling, Diagnostics, Security & Automated Tests
- Added `fetch_with_retry` for API retries and explicit fallback/health telemetry via `RunStats` (`requests`, `retries`, `api_errors`, `ai_batches` ok/fallback, plus record pipeline counts).
- Added `sanitize_csv_value` to escape spreadsheet formula-injection prefixes across exported CSV files.
- Added `.gitignore` covering `.env`, `.env.*` (except `!.env.example`), Python cache artifacts, and `state/` checkpoint files.
- Added `tests/test_fetch_permits.py` with 10 unit tests covering multi-year same-address separation, `'no roofing work'`, irrelevant shelving, mixed demolition negations, blank contact/link provenance, duplicate project IDs / non-additive sqft, CSV formula injection, recency ranking, failed AI + API retry fallback, and repeat checkpointed weekly runs.

### 9. Zero Auto-Send & Zero Paid Dependencies
- Uses Python standard library only; generates local CSV and Markdown reports for human review with zero automated email or outreach sending.
