# Permit Radar — Austin commercial permit intelligence

Version 4.0.0. Python 3.10 or newer. Standard library only; no packages or AI keys required.

Collects City of Austin **commercial building permits (`BP`)**, including demolition recorded under building permits. Austin `DS` means **Driveway / Sidewalks**, not demolition, and is excluded. The output identifies potential service opportunities. It does not establish that a supplier is still needed.

## Run locally

Open a terminal in this folder:

```powershell
python -m unittest discover -s tests -v
python fetch_permits.py --no-ai
python verify_reports.py output
```

The first run builds the current report and an initial changes feed. Subsequent runs retain the complete current-window inventory and put only newly observed or changed sites into the changes feed. A successful collection is **not a delivery to a customer**.

For dumpster customers, review `output/changes/by_trade/dumpster_demolition_leads.csv`. For a new subscriber's initial sample, use `output/by_trade/dumpster_demolition_leads.csv`. Open the source links and confirm the service is still relevant before sending.

## GitHub setup

1. Create a **private repository**. Extract the release ZIP and upload the folder's **contents at the repository root**, including `.github/workflows/`, `.gitignore`, the Python files, and `tests/`. Do not upload the ZIP itself as the application. If using the website uploader, enable showing hidden files so `.github` is included.
2. In **Settings → Actions → General → Workflow permissions**, allow **Read and write permissions**. The daily workflow commits the reports and checkpoint together. Repository or branch rules must allow the Actions bot to push; a blocked push is reported as a failed run.
3. Under **Actions**, run **Permit Radar Tests**, then **Daily Commercial Permit Radar → Run workflow** on the default branch.
4. Open the completed run and download the `austin-permit-radar-...` artifact. Check `run_manifest.json` for the date range, counts and source refresh time. The same reports and checkpoint are committed to the private repository.

The production workflow runs weekdays at **12:17 UTC**. GitHub schedules may be delayed and are not a guaranteed delivery time. No email is sent. The private-repository check prevents this workflow from publishing your reports and collection state in a public repository.

The checkpoint is explicitly committed despite being ignored for ordinary local development. It is not stored in an evictable cache. Runs are serialized. Tests and report verification run before a successful collection is persisted.

A push failure can leave a downloadable artifact without persisted state. The artifact includes both `output/` and `state/checkpoint.json`; after fixing push permissions, restore and commit them together before the next collection, or rerun and check overlap against the previous artifact before delivery. Do not interpret a failed workflow as a successful daily update.

## Report files

| File under `output/` | Purpose |
|---|---|
| `austin_commercial_permits_full.csv` | All current-window active, non-noise sites, including review flags. This is not an all-time archive. |
| `austin_new_or_updated.csv` | New/changed sites since the last successful run, including sites needing review. |
| `austin_needs_review.csv` | Unsupported scope, missing contacts/links, or uncertain timing. |
| `by_trade/dumpster_demolition_leads.csv` | Qualified potential dumpster/debris opportunities in the full window. |
| `changes/by_trade/dumpster_demolition_leads.csv` | Qualified potential dumpster/debris opportunities newly observed or changed. |
| Other files in `by_trade/` and `changes/by_trade/` | Fencing/site services, post-construction cleaning, roofing/exterior opportunities. |
| `austin_top_15_sample_sheet.csv`, `austin_top_20_sample_sheet.csv` | Current-window samples across all trades, not dumpster-only rankings. |
| `austin_inactive_in_window.csv` | Recently issued records currently Final/Expired/etc., excluded from active leads. |
| `monday_email_snippets.md` | Draft digest of changed opportunities for manual use. Nothing is sent. |
| `source_records.json` | Original records from the query, preserving all published fields. |
| `run_manifest.json` | Source dates, counts, filters, warnings, version and hashes for every report. |

Use `python verify_reports.py output` again if files were copied or a process stopped unexpectedly. It rejects incomplete bundles, mixed generations, altered files and offline replays. It is an integrity check, not independent verification that each project needs a vendor.

## What qualification means

- **High dumpster fit:** explicit demolition, removal, tear-off, or dumpster/debris scope. This is a rules-based fit label, not a measured conversion probability.
- **Medium dumpster fit:** substantial construction scope suggests potential debris. It is explicitly labeled **inferred**.
- **Qualified opportunity:** active, supported trade scope, usable-looking contractor phone, matching official source URL and no flagged timing/address problem. Contact details are city-published and have not been called or independently validated.
- **Needs Review:** stays in the master/review CSV and is excluded from customer trade sheets. Applicant fallback details remain labeled. Separate applicant columns preserve useful research contacts.

A record is not made relevant just because it has a large square footage or the work class says Remodel/New. Electrical-only work, parties, shelving, and dumpster enclosure construction do not by themselves establish a debris opportunity. Interior demolition does not automatically imply temporary fencing or final cleaning. Rules handle common negations and work attributed to a separate permit, but unusual source language still warrants review.

Full descriptions are retained before classification. Explicit and inferred evidence is exported per trade, along with member permit numbers and member source URLs. Related permits are grouped only with compatible identity, address/suite, contractor and dates. Unknown suites cannot bridge different units.

Square footage uses the **maximum** of reported remodel/new-add area because these can overlap. Grouped areas are added only for explicitly distinct buildings; fire areas alone do not prove additivity. Original area fields for the representative permit and the raw source records are preserved. Grouped issue dates/areas describe the site; consult individual raw records and member links for permit-specific facts.

## Reliability and state

Every live run validates the source schema and update timestamp, checks the exact matching row count, and verifies that the source revision did not change while pages were downloaded. The final-page probe distinguishes a complete full page from truncation. Malformed/repeated pages, stale source data, incomplete downloads and corrupted state stop the run before replacing reports or advancing the checkpoint.

Reports are prepared in a staging directory. Each replacement is atomic; caught publication errors roll back already replaced files. The hash manifest is committed last, followed by the checkpoint. An abrupt process/OS crash during publication can still interrupt the multi-file transaction: rerun `verify_reports.py`, then rerun the fetch if verification fails. Separate output and checkpoint locks prevent concurrent local writers. After a crash, remove a leftover lock only after confirming the old process is no longer running.

Fingerprints include source links, application/status dates, master IDs, descriptions, areas and contact details. Changes revoke the previous approval for that revision. Existing v2 state migrates to v3; its first run can show previously collected records as updated because the fingerprint now covers more fields. Review that overlap before delivery.

The default issue window is `[as-of minus 14 days, as-of]`, **both dates inclusive**. After a missed-run gap it automatically extends back to the last successful run minus seven days. Permits backfilled with older issue dates and changes to old permits outside this range still require a wider audit. The system does not claim to monitor all active projects in Austin or provide a per-subscriber delivery ledger.

## Useful commands

```powershell
# Wider review: new observations can include older issued permits
python fetch_permits.py --days 60 --no-ai

# Inspect a fixed issue window without advancing saved state
python fetch_permits.py --as-of 2026-11-14 --days 14 --no-save-checkpoint --output-dir review-output --no-ai

# Re-include the full window in the changes feed deliberately
python fetch_permits.py --ignore-checkpoint --no-save-checkpoint --output-dir sample-output --no-ai

# Replay an archived source download without network calls or live state changes
python fetch_permits.py --input-json output/source_records.json --as-of 2026-11-14 --no-save-checkpoint --ignore-checkpoint --output-dir replay-output --no-ai
python verify_reports.py replay-output --allow-replay

# Optional manual bookkeeping; never sends messages
python fetch_permits.py --approve-permits "2026-149423 BP"
python fetch_permits.py --deliver-permits "2026-149423 BP"
```

Replace sample permit numbers and dates with the intended records. `--as-of` limits issuance dates; it does **not** reconstruct historical status or contacts. Offline replay manifests explicitly say that freshness/completeness was not checked live.

Other options: `--page-size` (200), `--max-pages` (50), `--max-source-age-days` (7), `--checkpoint` and `--output-dir`. State must stay outside the managed output directory. `--fail-on-truncation` is retained for compatibility; incomplete downloads now always fail. Exit codes: 0 success, 1 source/state/write failure, 2 bad arguments, 3 truncation.

## Optional AI

Rules are the default even when API keys exist. Copy `.env.example` to `.env` for an optional Socrata app token or AI credentials. `python fetch_permits.py --ai` adds clearly marked, **unverified suggestions only to the master/review sheets**. AI never changes customer scope, facts, eligibility or summaries. Provider failures fall back to rules. Provider quotas/model availability can change; no AI is required by the scheduled workflow.

Do not commit `.env` or API keys. CSV text is escaped against spreadsheet formula injection. The packaged release includes no credentials, live checkpoint or customer reports.

## Source and limits

- [City of Austin source dataset](https://data.austintexas.gov/d/3syk-w9eu)
- [JSON API](https://data.austintexas.gov/resource/3syk-w9eu.json)
- [Dataset metadata/schema](https://data.austintexas.gov/api/views/3syk-w9eu.json)
- [Socrata API documentation](https://dev.socrata.com/foundry/data.austintexas.gov/3syk-w9eu)
- [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

Issued permits are an opportunity signal. They cannot prove an unfilled vendor contract, available budget, demolition start date or cleaning completion date. The data includes commercially classified multifamily projects and Austin jurisdiction/ETJ records; it is not all of metro Austin. There are no source email addresses. Official URLs are checked for host/path and matching record ID, **not** successful portal-page loading. Source quality and actual buyer usefulness still need field validation with your first customers.
