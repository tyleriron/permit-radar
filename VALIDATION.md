# Validation — Permit Radar 4.0.0

Checked on November 14, 2026.

## Automated checks actually run

- **51 tests passed on Python 3.10 and Python 3.14 on Windows.**
- Coverage includes repeat-run suppression, full report retention, changed contacts/links/dates, source closures, corrupt state, exact-full pagination, true truncation, duplicate/invalid API pages, schema changes, stale data, count mismatches and source revision changes.
- Failure-injection checks confirm that staging errors do not advance state and that a failed checkpoint replacement restores previously replaced reports.
- Classification regressions cover DS code confusion, electrical-only scope, dumpster enclosures, roof-mounted equipment, interior demolition, suffix/list negations, long descriptions, suite bridges and overlapping area fields.
- AI output cannot change buyer facts or eligibility. Invalid AI response shapes are ignored.
- Integrity verification checks manifest hashes, customer feed eligibility/counts and current/change feed membership. Altered, missing or replayed files are rejected.

## Live source check actually run

Source: https://data.austintexas.gov/resource/3syk-w9eu.json

Window: **October 31–November 14, 2026, both dates inclusive**. Commercial building permits, all statuses fetched; active records qualify for output.

- Source metadata and required column names validated.
- Latest issued date: **November 13, 2026**.
- Published source refresh: **2026-11-14T15:23:05+00:00**.
- Independent API count: **130 records**, matched by the download.
- Pagination exercised at 20 rows/page: **7 nonempty pages**. Source revision unchanged before/after download. No network retry was needed in that run.
- **113 grouped active sites** after filtering and consolidation.
- **69 sites passed the automatic opportunity checks; 44 held for review.**
- Dumpster/debris feed: **43 potential opportunities — 21 explicit scope, 22 inferred.**
- A second live run with the same checkpoint kept all 113 current sites and produced **0 new/updated sites** and **0 new/updated trade opportunities**.
- The actual project output was then refreshed successfully using its existing checkpoint; a backup of the previous local state/reports was retained during the review.
- Final project run ID: `20261114T204124Z-c9589ed1`.

## Limits of this validation

The GitHub workflows have been prepared and reviewed but **have not been executed in your GitHub account**. Repository permissions and branch rules must be checked in the first manual run. Linux execution is configured in CI but was not run locally.

AI provider success paths were tested with simulated responses; no live AI calls or paid API use were needed. Contractor phone numbers, portal-page availability, construction activity and unfilled vendor demand were not independently verified. These tests establish software behavior and source consistency, not sales conversion or exhaustive classification accuracy.

The release package includes source, tests, workflows and instructions. It intentionally omits credentials, the live checkpoint and generated customer reports, so a new deployment begins with its own initial collection.

Fetcher SHA-256: `7c8d313d2e87e64c83a66af7be4e991d7b7d57560afde8f7867140b8164d6ac5`
