"""Unit tests for Commercial Permit Radar (fetch_permits.py)."""

from __future__ import annotations

import csv
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

RADAR_DIR = Path(__file__).resolve().parents[1]
if str(RADAR_DIR) not in sys.path:
    sys.path.insert(0, str(RADAR_DIR))

import fetch_permits as fp


def _make_raw_permit(**overrides) -> dict:
    pid = overrides.get("project_id", "900001")
    base = {
        "permittype": "BP",
        "permit_type_desc": "Building Permit",
        "permit_number": "2026-100001 BP",
        "permit_class_mapped": "Commercial",
        "permit_class": "C-1000 Commercial Remodel",
        "work_class": "Remodel",
        "permit_location": "500 CONGRESS AVE",
        "description": "Commercial interior remodel of suite 200",
        "applieddate": "2026-10-20T00:00:00.000",
        "issue_date": "2026-11-10T00:00:00.000",
        "status_current": "Active",
        "remodel_repair_sqft": "3000",
        "original_address1": "500 CONGRESS AVE",
        "original_city": "AUSTIN",
        "original_zip": "78701",
        "jurisdiction": "AUSTIN FULL PURPOSE",
        "link": {
            "url": (
                "https://abc.austintexas.gov/web/permit/public-search-other"
                f"?t_detail=1&t_selected_folderrsn={pid}"
            )
        },
        "project_id": pid,
        "masterpermitnum": "",
        "contractor_company_name": "Acme Builders LLC",
        "contractor_full_name": "Jane Doe",
        "contractor_phone": "5125550100",
    }
    base.update(overrides)
    return base


class FakeHTTPResponse:
    def __init__(self, payload: object) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *args) -> None:
        return None


@contextmanager
def mock_source(rows):
    metadata = {"rowsUpdatedAt": int(datetime.now(timezone.utc).timestamp()),
                "columns": [{"fieldName": n} for n in fp.REQUIRED_SOURCE_FIELDS]}
    def respond(req, timeout=25):
        if "/api/views/" in req.full_url:
            return FakeHTTPResponse(metadata)
        query = fp.urllib.parse.parse_qs(fp.urllib.parse.urlparse(req.full_url).query)
        selection = query.get("$select", [""])[0]
        if selection.startswith("max("):
            return FakeHTTPResponse([{"latest": max(r["issue_date"] for r in rows)}])
        if selection.startswith("count("):
            return FakeHTTPResponse([{"n": str(len(rows))}])
        offset = int(query.get("$offset", [0])[0])
        limit = int(query.get("$limit", [200])[0])
        return FakeHTTPResponse(rows[offset:offset+limit])
    with mock.patch("urllib.request.urlopen", side_effect=respond):
        yield


class TestFetchPermits(unittest.TestCase):
    def test_same_address_different_years_not_merged_and_old_filtered(self) -> None:
        stats = fp.RunStats()
        cutoff = date(2026, 11, 1)

        old_2024 = _make_raw_permit(
            permit_number="2024-000111 BP",
            project_id="700111",
            masterpermitnum="888999",
            applieddate="2024-04-01T00:00:00.000",
            issue_date="2024-05-10T00:00:00.000",
            remodel_repair_sqft="5000",
        )
        new_2026 = _make_raw_permit(
            permit_number="2026-222333 BP",
            project_id="900333",
            masterpermitnum="888999",
            applieddate="2020-03-01T00:00:00.000",
            issue_date="2026-11-12T00:00:00.000",
            remodel_repair_sqft="4000",
        )
        # Also verify two non-filtered records from different years would never merge
        norm_2024 = fp.normalize_record(
            old_2024, "austin", date(2024, 1, 1), set(), set(), True, fp.RunStats()
        )
        norm_2026 = fp.normalize_record(
            new_2026, "austin", cutoff, set(), set(), True, stats
        )
        self.assertIsNotNone(norm_2024)
        self.assertIsNotNone(norm_2026)
        merged = fp.deduplicate_projects([norm_2024, norm_2026])
        self.assertEqual(len(merged), 2)

        # Verify 2024 permit IS filtered out by --days 14 cutoff
        filtered_old = fp.normalize_record(
            old_2024, "austin", cutoff, set(), set(), True, stats
        )
        self.assertIsNone(filtered_old)
        self.assertEqual(stats.records_filtered_old_or_inactive, 1)
        self.assertEqual(
            norm_2026["timing_status"],
            "Delayed Issue (Applied >1 Yr Ago — Verify If Renewal/Phase)",
        )

    def test_negation_no_roofing_work(self) -> None:
        raw = _make_raw_permit(
            description="Commercial interior remodel; no roofing work",
            remodel_repair_sqft="3200",
        )
        rec = fp.normalize_record(
            raw, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats()
        )
        site = fp.deduplicate_projects([rec])[0]
        res = fp.classify_rule_based(site)
        self.assertNotEqual(res["primary_trade"], "Commercial Roofing & Exterior")
        self.assertNotIn("Commercial Roofing & Exterior", res["applicable_trades"])
        self.assertIn("Post-Construction Cleaning", res["applicable_trades"])

    def test_irrelevant_shelving_work_unclassified(self) -> None:
        raw = _make_raw_permit(
            work_class="Repair",
            description="Install interior metal shelving and storage racks in warehouse",
            remodel_repair_sqft="0",
        )
        rec = fp.normalize_record(
            raw, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats()
        )
        site = fp.deduplicate_projects([rec])[0]
        stats = fp.RunStats()
        fp.classify_sites([site], use_ai=False, stats=stats)

        self.assertEqual(site["primary_trade"], fp.UNCLASSIFIED_TRADE)
        self.assertEqual(site["_applicable_list"], [])
        self.assertEqual(stats.sites_needs_review, 1)
        self.assertEqual(site["optional_gc_call_opener"], "")

        with tempfile.TemporaryDirectory() as tmp:
            counts = fp.export_by_trade([site], Path(tmp))
            self.assertEqual(sum(counts.values()), 0)

    def test_mixed_demolition_negations(self) -> None:
        roof_only_raw = _make_raw_permit(
            permit_number="2026-300001 BP",
            project_id="930001",
            description="Commercial roof replacement; no demolition and does not require dumpsters",
            remodel_repair_sqft="6000",
        )
        demo_only_raw = _make_raw_permit(
            permit_number="2026-300002 BP",
            project_id="930002",
            work_class="Interior Demo Non-Structural",
            description="Interior non-structural demolition of office suites; no roofing work",
            remodel_repair_sqft="6000",
        )
        r1 = fp.normalize_record(roof_only_raw, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
        r2 = fp.normalize_record(demo_only_raw, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
        s1, s2 = fp.deduplicate_projects([r1, r2])

        c1 = fp.classify_rule_based(s1)
        self.assertEqual(c1["primary_trade"], "Commercial Roofing & Exterior")
        self.assertNotIn("Dumpster & Demolition", c1["applicable_trades"])

        c2 = fp.classify_rule_based(s2)
        self.assertEqual(c2["primary_trade"], "Dumpster & Demolition")
        self.assertIn("Dumpster & Demolition", c2["applicable_trades"])
        self.assertNotIn("Commercial Roofing & Exterior", c2["applicable_trades"])

    def test_blank_contact_and_invalid_link_provenance(self) -> None:
        raw = _make_raw_permit(
            project_id="NOT_A_NUMBER",
            link={"url": "javascript:alert(1)"},
            contractor_company_name="",
            contractor_full_name="",
            contractor_phone="",
            applicant_org="",
            applicant_full_name="",
            applicant_phone="",
            applieddate="",
            jurisdiction="",
        )
        rec = fp.normalize_record(
            raw, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats()
        )
        self.assertIsNotNone(rec)
        self.assertEqual(rec["contact_provenance"], "No Contact Listed")
        self.assertEqual(rec["contractor_company"], "Not Listed")
        self.assertEqual(rec["contractor_contact"], "Not Listed")
        self.assertEqual(rec["contractor_phone"], "Not Listed")
        self.assertEqual(rec["source_url"], "Link Unavailable")
        self.assertEqual(rec["applied_date"], "Not Listed")
        self.assertEqual(rec["jurisdiction"], "Not Listed")
        self.assertEqual(rec["timing_status"], "Issued Recently (Application Date Unlisted)")

    def test_duplicate_project_ids_and_non_additive_sqft(self) -> None:
        dup_a = _make_raw_permit(
            permit_number="2026-400001 BP",
            project_id="940001",
            remodel_repair_sqft="10000",
        )
        dup_b = _make_raw_permit(
            permit_number="2026-400001 BP",
            project_id="940001",
            remodel_repair_sqft="10000",
        )
        norm_a = fp.normalize_record(dup_a, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
        norm_b = fp.normalize_record(dup_b, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
        merged_dup = fp.deduplicate_projects([norm_a, norm_b])
        self.assertEqual(len(merged_dup), 1)
        self.assertEqual(merged_dup[0]["sqft"], 10000)
        self.assertEqual(merged_dup[0]["permit_count_at_site"], 1)
        self.assertEqual(merged_dup[0]["sqft_basis"], "Single Permit")

        # Same masterpermitnum without distinct building/fire-area designators must NOT sum sqft
        same_master_1 = _make_raw_permit(
            permit_number="2026-500001 BP",
            project_id="950001",
            masterpermitnum="888000",
            permit_location="700 OAK ST",
            remodel_repair_sqft="8000",
        )
        same_master_2 = _make_raw_permit(
            permit_number="2026-500002 BP",
            project_id="950002",
            masterpermitnum="888000",
            permit_location="700 OAK ST",
            remodel_repair_sqft="8000",
        )
        m1 = fp.normalize_record(same_master_1, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
        m2 = fp.normalize_record(same_master_2, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
        merged_non_add = fp.deduplicate_projects([m1, m2])
        self.assertEqual(len(merged_non_add), 1)
        self.assertEqual(merged_non_add[0]["sqft"], 8000)
        self.assertEqual(merged_non_add[0]["sqft_basis"], "Max Single Permit (Non-Additive Group)")
        self.assertEqual(merged_non_add[0]["member_permit_numbers"], "2026-500001 BP; 2026-500002 BP")

        # Distinct building designators on same masterpermitnum CAN sum sqft
        bldg_1 = _make_raw_permit(
            permit_number="2026-600001 BP",
            project_id="960001",
            masterpermitnum="888111",
            permit_location="900 PINE ST BLDG 1",
            remodel_repair_sqft="5000",
        )
        bldg_2 = _make_raw_permit(
            permit_number="2026-600002 BP",
            project_id="960002",
            masterpermitnum="888111",
            permit_location="900 PINE ST BLDG 2",
            remodel_repair_sqft="6000",
        )
        nb1 = fp.normalize_record(bldg_1, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
        nb2 = fp.normalize_record(bldg_2, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
        merged_add = fp.deduplicate_projects([nb1, nb2])
        self.assertEqual(len(merged_add), 1)
        self.assertEqual(merged_add[0]["sqft"], 11000)
        self.assertEqual(merged_add[0]["sqft_basis"], "Sum of Distinct Buildings/Fire Areas")

    def test_failed_ai_requests_and_api_retry_fallback(self) -> None:
        sample_rows = [_make_raw_permit()]
        call_counter = {"n": 0}

        def fake_urlopen(req, timeout=25):
            call_counter["n"] += 1
            url = req.full_url
            if "data.austintexas.gov" in url:
                if call_counter["n"] == 1:
                    raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, io.BytesIO(b""))
                return FakeHTTPResponse(sample_rows)
            # Fail Groq AI request
            raise urllib.error.URLError("Simulated AI timeout")

        stats = fp.RunStats()
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            fetched = fp.fetch_raw_permits(
                "austin", date(2026, 11, 1), page_size=50, max_pages=1, stats=stats, sleep_fn=lambda _: None
            )
            self.assertEqual(len(fetched), 1)
            self.assertEqual(stats.api_retries, 1)

            rec = fp.normalize_record(fetched[0], "austin", date(2026, 11, 1), set(), set(), True, stats)
            sites = fp.deduplicate_projects([rec])
            with mock.patch.dict(os.environ, {"GROQ_API_KEY": "fake-key"}, clear=False):
                fp.classify_sites(sites, use_ai=True, stats=stats, sleep_fn=lambda _: None)

            self.assertEqual(stats.ai_batches_attempted, 1)
            self.assertEqual(stats.ai_batches_succeeded, 0)
            self.assertEqual(stats.ai_batches_failed_fallback, 1)
            self.assertEqual(sites[0]["primary_trade"], "Post-Construction Cleaning")

    def test_repeat_weekly_runs_checkpoint(self) -> None:
        sample_rows = [_make_raw_permit(permit_number="2026-777001 BP", project_id="977001")]

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ckpt_path = tmp_path / "state" / "checkpoint.json"
            out_dir = tmp_path / "output"

            with mock_source(sample_rows):
                rc1 = fp.main(
                    [
                        "--days", "14",
                        "--as-of", "2026-11-14",
                        "--checkpoint", str(ckpt_path),
                        "--output-dir", str(out_dir),
                        "--no-ai",
                    ]
                )
            self.assertEqual(rc1, 0)
            self.assertTrue(ckpt_path.is_file())
            with (out_dir / "austin_commercial_permits_full.csv").open(encoding="utf-8-sig") as fh:
                run1_rows = list(csv.DictReader(fh))
            self.assertEqual(len(run1_rows), 1)

            # Repeat run with identical permits must skip already-checkpointed record
            with mock_source(sample_rows):
                rc2 = fp.main(
                    [
                        "--days", "14",
                        "--as-of", "2026-11-14",
                        "--checkpoint", str(ckpt_path),
                        "--output-dir", str(out_dir),
                        "--no-ai",
                    ]
                )
            self.assertEqual(rc2, 0)
            with (out_dir / "austin_commercial_permits_full.csv").open(encoding="utf-8-sig") as fh:
                run2_rows = list(csv.DictReader(fh))
            self.assertEqual(len(run2_rows), 1)
            with (out_dir / "austin_new_or_updated.csv").open(encoding="utf-8-sig") as fh:
                self.assertEqual(list(csv.DictReader(fh)), [])

    def test_csv_formula_injection_prevention(self) -> None:
        self.assertEqual(fp.sanitize_csv_value("=CMD('calc')"), "'=CMD('calc')")
        self.assertEqual(fp.sanitize_csv_value("+1+1"), "'+1+1")
        self.assertEqual(fp.sanitize_csv_value("-500"), "'-500")
        self.assertEqual(fp.sanitize_csv_value("@SUM(A1:A2)"), "'@SUM(A1:A2)")
        self.assertEqual(fp.sanitize_csv_value("  =HYPERLINK()"), "'  =HYPERLINK()")
        self.assertEqual(fp.sanitize_csv_value("\t=1"), "'\t=1")
        self.assertEqual(fp.sanitize_csv_value("(512) 555-0100"), "(512) 555-0100")

    def test_recency_ranking_prefers_newer_issue_date_and_fresh_app(self) -> None:
        older_huge = _make_raw_permit(
            permit_number="2026-800001 BP",
            project_id="980001",
            applieddate="2026-10-01T00:00:00.000",
            issue_date="2026-11-05T00:00:00.000",
            remodel_repair_sqft="50000",
        )
        newer_smaller = _make_raw_permit(
            permit_number="2026-800002 BP",
            project_id="980002",
            applieddate="2026-10-25T00:00:00.000",
            issue_date="2026-11-13T00:00:00.000",
            remodel_repair_sqft="4000",
        )
        delayed_old_app = _make_raw_permit(
            permit_number="2026-800003 BP",
            project_id="980003",
            applieddate="2019-04-19T00:00:00.000",
            issue_date="2026-11-13T00:00:00.000",
            remodel_repair_sqft="60000",
        )
        recs = [
            fp.normalize_record(r, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
            for r in (older_huge, newer_smaller, delayed_old_app)
        ]
        sites = fp.deduplicate_projects(recs)
        fp.classify_sites(sites, use_ai=False, stats=fp.RunStats())
        ranked = sorted(sites, key=fp.site_rank_key, reverse=True)
        self.assertEqual(
            [s["permit_number"] for s in ranked],
            ["2026-800002 BP", "2026-800001 BP", "2026-800003 BP"],
        )

    def test_same_address_different_suites_not_merged(self) -> None:
        ste_100 = _make_raw_permit(
            permit_number="2026-910001 BP",
            project_id="991001",
            permit_location="500 CONGRESS AVE STE 100",
            original_address1="500 CONGRESS AVE STE 100",
            applieddate="2026-10-20T00:00:00.000",
            issue_date="2026-11-10T00:00:00.000",
            description="Tenant interior remodel",
            remodel_repair_sqft="2500",
        )
        ste_200 = _make_raw_permit(
            permit_number="2026-910002 BP",
            project_id="991002",
            permit_location="500 CONGRESS AVE STE 200",
            original_address1="500 CONGRESS AVE STE 200",
            applieddate="2026-10-20T00:00:00.000",
            issue_date="2026-11-10T00:00:00.000",
            description="Tenant interior remodel",
            remodel_repair_sqft="3500",
        )
        # Same street address without suite suffix, distinguished only by description suite
        desc_ste_300 = _make_raw_permit(
            permit_number="2026-910003 BP",
            project_id="991003",
            permit_location="600 GUADALUPE ST",
            original_address1="600 GUADALUPE ST",
            applieddate="2026-10-20T00:00:00.000",
            issue_date="2026-11-10T00:00:00.000",
            description="Interior finish-out for suite 300",
            remodel_repair_sqft="4000",
        )
        desc_ste_400 = _make_raw_permit(
            permit_number="2026-910004 BP",
            project_id="991004",
            permit_location="600 GUADALUPE ST",
            original_address1="600 GUADALUPE ST",
            applieddate="2026-10-20T00:00:00.000",
            issue_date="2026-11-10T00:00:00.000",
            description="Interior finish-out for suite 400",
            remodel_repair_sqft="4500",
        )
        norms = [
            fp.normalize_record(r, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
            for r in (ste_100, ste_200, desc_ste_300, desc_ste_400)
        ]
        merged = fp.deduplicate_projects(norms)
        self.assertEqual(len(merged), 4)
        self.assertEqual(sorted(m["permit_number"] for m in merged), [
            "2026-910001 BP", "2026-910002 BP", "2026-910003 BP", "2026-910004 BP"
        ])

    def test_checkpoint_new_permit_same_project_id_and_changed_permit_not_discarded(self) -> None:
        permit_a = _make_raw_permit(
            permit_number="2026-777001 BP",
            project_id="977001",
            remodel_repair_sqft="3000",
        )
        permit_b_same_pid = _make_raw_permit(
            permit_number="2026-777002 BP",
            project_id="977001",
            remodel_repair_sqft="4200",
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ckpt_path = tmp_path / "state" / "checkpoint.json"
            out_dir = tmp_path / "output"

            # Run 1: Permit A processed
            with mock_source([permit_a]):
                rc1 = fp.main([
                    "--days", "14", "--as-of", "2026-11-14",
                    "--checkpoint", str(ckpt_path), "--output-dir", str(out_dir), "--no-ai",
                ])
            self.assertEqual(rc1, 0)

            # Run 2: Permit B shares project_id="977001" AND Permit A is unchanged -> B MUST NOT be discarded
            with mock_source([permit_a, permit_b_same_pid]):
                rc2 = fp.main([
                    "--days", "14", "--as-of", "2026-11-14",
                    "--checkpoint", str(ckpt_path), "--output-dir", str(out_dir), "--no-ai",
                ])
            self.assertEqual(rc2, 0)
            with (out_dir / "austin_new_or_updated.csv").open(encoding="utf-8-sig") as fh:
                run2_rows = list(csv.DictReader(fh))
            self.assertEqual([r["permit_number"] for r in run2_rows], ["2026-777002 BP"])

            # Run 3: Permit A description/sqft updated -> Permit A MUST be reprocessed, Permit B skipped
            permit_a_changed = _make_raw_permit(
                permit_number="2026-777001 BP",
                project_id="977001",
                remodel_repair_sqft="6500",
                description="Expanded scope: Commercial interior remodel and TPO roof patch",
            )
            with mock_source([permit_a_changed, permit_b_same_pid]):
                rc3 = fp.main([
                    "--days", "14", "--as-of", "2026-11-14",
                    "--checkpoint", str(ckpt_path), "--output-dir", str(out_dir), "--no-ai",
                ])
            self.assertEqual(rc3, 0)
            with (out_dir / "austin_new_or_updated.csv").open(encoding="utf-8-sig") as fh:
                run3_rows = list(csv.DictReader(fh))
            self.assertEqual([r["permit_number"] for r in run3_rows], ["2026-777001 BP"])
            self.assertEqual(run3_rows[0]["sqft"], "6500")

            state = fp.load_checkpoint_state(ckpt_path)
            entry_a = state["permits"]["permit:2026-777001 BP"]
            self.assertEqual(len(entry_a["change_history"]), 1)

    def test_persistence_lifecycle_collected_processed_approved_delivered(self) -> None:
        raw_p1 = _make_raw_permit(permit_number="2026-660001 BP", project_id="966001")
        raw_p2 = _make_raw_permit(permit_number="2026-660002 BP", project_id="966002")

        with tempfile.TemporaryDirectory() as tmp:
            ckpt_path = Path(tmp) / "state" / "checkpoint.json"
            state = fp.load_checkpoint_state(ckpt_path)

            # Stage 1: Collected
            fp.record_collected_permits(state, [raw_p1, raw_p2], "austin")
            fp._write_checkpoint_state(ckpt_path, state)
            s1 = fp.get_persistence_summary(fp.load_checkpoint_state(ckpt_path))
            self.assertEqual(s1, {"collected": 2, "processed": 0, "approved": 0, "delivered": 0, "total_tracked": 2})

            # Collected permit must NOT be skipped by incremental normalize_record
            rec1 = fp.normalize_record(
                raw_p1, "austin", date(2026, 11, 1), set(), set(), False, fp.RunStats(),
                as_of_date=date(2026, 11, 14), checkpoint_state=state,
            )
            self.assertIsNotNone(rec1)

            # Stage 2: Processed
            site1 = fp.deduplicate_projects([rec1])[0]
            fp.classify_sites([site1], use_ai=False, stats=fp.RunStats())
            fp.save_checkpoint(
                ckpt_path, date(2026, 11, 14), set(), set(), [site1], checkpoint_state=state
            )
            s2 = fp.get_persistence_summary(fp.load_checkpoint_state(ckpt_path))
            self.assertEqual(s2, {"collected": 1, "processed": 1, "approved": 0, "delivered": 0, "total_tracked": 2})

            # Stage 3: Approved via CLI/function
            updated_app = fp.mark_permits_approved(ckpt_path, ["2026-660001 BP"])
            self.assertEqual(updated_app, 1)
            s3 = fp.get_persistence_summary(fp.load_checkpoint_state(ckpt_path))
            self.assertEqual(s3, {"collected": 1, "processed": 0, "approved": 1, "delivered": 0, "total_tracked": 2})

            # Stage 4: Delivered
            updated_del = fp.mark_permits_delivered(ckpt_path, ["2026-660001 BP"])
            self.assertEqual(updated_del, 1)
            s4 = fp.get_persistence_summary(fp.load_checkpoint_state(ckpt_path))
            self.assertEqual(s4, {"collected": 1, "processed": 0, "approved": 0, "delivered": 1, "total_tracked": 2})

    def test_negation_no_roof_repairs_install_new_tpo_roof_membrane(self) -> None:
        raw_tpo = _make_raw_permit(
            work_class="Remodel",
            description="No roof repairs. Install new TPO roof membrane",
            remodel_repair_sqft="4200",
        )
        rec = fp.normalize_record(
            raw_tpo, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats()
        )
        site = fp.deduplicate_projects([rec])[0]
        res = fp.classify_rule_based(site)
        self.assertEqual(res["primary_trade"], "Commercial Roofing & Exterior")
        self.assertIn("Commercial Roofing & Exterior", res["applicable_trades"])

        raw_multi = _make_raw_permit(
            permit_number="2026-920002 BP",
            project_id="992002",
            work_class="Remodel",
            description="No roof repairs. No temporary fencing. Selective interior demolition and office finish-out.",
            remodel_repair_sqft="5000",
        )
        rec2 = fp.normalize_record(
            raw_multi, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats()
        )
        site2 = fp.deduplicate_projects([rec2])[0]
        res2 = fp.classify_rule_based(site2)
        self.assertNotIn("Commercial Roofing & Exterior", res2["applicable_trades"])
        self.assertNotIn("Temp Fencing & Site Services", res2["applicable_trades"])
        self.assertIn("Dumpster & Demolition", res2["applicable_trades"])
        self.assertIn("Post-Construction Cleaning", res2["applicable_trades"])

    def test_historical_as_of_enforces_lower_and_upper_date_bounds(self) -> None:
        too_old = _make_raw_permit(
            permit_number="2026-930001 BP",
            project_id="993001",
            issue_date="2026-11-01T00:00:00.000",
        )
        in_window = _make_raw_permit(
            permit_number="2026-930002 BP",
            project_id="993002",
            issue_date="2026-11-08T00:00:00.000",
        )
        after_as_of = _make_raw_permit(
            permit_number="2026-930003 BP",
            project_id="993003",
            issue_date="2026-11-13T00:00:00.000",
        )
        captured_urls: list[str] = []

        def fake_urlopen(req, timeout=25):
            captured_urls.append(req.full_url)
            return FakeHTTPResponse([too_old, in_window, after_as_of])

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_dir = tmp_path / "output"
            ckpt_path = tmp_path / "state" / "checkpoint.json"
            with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen), mock.patch.object(fp, "check_source_metadata", return_value={"rowsUpdatedAt": datetime.now(timezone.utc).timestamp()}), mock.patch.object(fp, "check_source_totals", side_effect=lambda c, a, b, stats: (setattr(stats, "source_latest_issue_date", "2026-11-08") or 3)):
                rc = fp.main([
                    "--days", "7", "--as-of", "2026-11-10",
                    "--checkpoint", str(ckpt_path), "--output-dir", str(out_dir), "--no-ai",
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(len(captured_urls), 1)
            query = fp.urllib.parse.parse_qs(fp.urllib.parse.urlparse(captured_urls[0]).query)
            where_sql = query["$where"][0]
            self.assertIn("issue_date >= '2026-11-03T00:00:00.000'", where_sql)
            self.assertIn("issue_date <= '2026-11-10T23:59:59.999'", where_sql)

            with (out_dir / "austin_commercial_permits_full.csv").open(encoding="utf-8-sig") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([r["permit_number"] for r in rows], ["2026-930002 BP"])

    def test_pagination_truncation_warning_and_fail_on_truncation(self) -> None:
        page1 = [_make_raw_permit(permit_number=f"2026-94000{i} BP", project_id=f"99400{i}") for i in (1, 2)]
        page2 = [_make_raw_permit(permit_number=f"2026-94000{i} BP", project_id=f"99400{i}") for i in (3, 4)]
        responses = [FakeHTTPResponse(page1), FakeHTTPResponse(page2), FakeHTTPResponse([_make_raw_permit(project_id="994005", permit_number="2026-940005 BP")])]

        stats = fp.RunStats()
        with mock.patch("urllib.request.urlopen", side_effect=responses):
            rows = fp.fetch_raw_permits(
                "austin", date(2026, 11, 1), page_size=2, max_pages=2, stats=stats,
                sleep_fn=lambda _: None, as_of_date=date(2026, 11, 14),
            )
        self.assertEqual(len(rows), 4)
        self.assertEqual(stats.pages_fetched, 2)
        self.assertTrue(stats.pagination_truncated)
        self.assertTrue(any("TRUNCATED DATASET" in w for w in stats.warnings))

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            responses_cli = [FakeHTTPResponse(page1), FakeHTTPResponse(page2)]
            with mock_source(page1 + page2 + [_make_raw_permit(project_id="994005", permit_number="2026-940005 BP")]):
                rc = fp.main([
                    "--days", "14", "--as-of", "2026-11-14",
                    "--page-size", "2", "--max-pages", "2", "--fail-on-truncation",
                    "--checkpoint", str(tmp_path / "ckpt.json"), "--output-dir", str(tmp_path / "out"), "--no-ai",
                ])
            self.assertEqual(rc, 3)

    def test_source_link_validation_mismatched_and_untrusted_links(self) -> None:
        mismatched = _make_raw_permit(
            permit_number="2026-950001 BP",
            project_id="995001",
            link={"url": "https://abc.austintexas.gov/web/permit/public-search-other?t_detail=1&t_selected_folderrsn=111111"},
        )
        untrusted = _make_raw_permit(
            permit_number="2026-950002 BP",
            project_id="995002",
            link={"url": "https://example.org/permit?t_selected_folderrsn=995002"},
        )
        missing_link = _make_raw_permit(
            permit_number="2026-950003 BP",
            project_id="995003",
            link=None,
        )
        valid_link = _make_raw_permit(
            permit_number="2026-950004 BP",
            project_id="995004",
        )

        stats = fp.RunStats()
        norms = [
            fp.normalize_record(r, "austin", date(2026, 11, 1), set(), set(), True, stats)
            for r in (mismatched, untrusted, missing_link, valid_link)
        ]
        sites = fp.deduplicate_projects(norms)
        fp.classify_sites(sites, use_ai=False, stats=stats)
        by_pnum = {s["permit_number"]: s for s in sites}

        self.assertEqual(by_pnum["2026-950001 BP"]["source_url"], "Unverified Link — Flagged for Manual Review")
        self.assertFalse(by_pnum["2026-950001 BP"]["link_verified"])
        self.assertIn("Manual Review Required", by_pnum["2026-950001 BP"]["classification_evidence"])

        self.assertEqual(by_pnum["2026-950002 BP"]["source_url"], "Unverified Link — Flagged for Manual Review")
        self.assertEqual(by_pnum["2026-950003 BP"]["source_url"], "Link Unavailable")
        self.assertTrue(by_pnum["2026-950004 BP"]["link_verified"])
        self.assertTrue(by_pnum["2026-950004 BP"]["source_url"].startswith("https://abc.austintexas.gov/"))
        self.assertEqual(stats.sites_valid_links, 1)
        self.assertEqual(stats.sites_unverified_links, 3)

    def test_missing_fields_and_invalid_statuses(self) -> None:
        stats = fp.RunStats()
        cutoff = date(2026, 11, 1)
        for bad_status in ("Final", "Expired", "Withdrawn", ""):
            bad = _make_raw_permit(status_current=bad_status)
            self.assertIsNone(fp.normalize_record(bad, "austin", cutoff, set(), set(), True, stats))
        missing_date = _make_raw_permit(issue_date="")
        self.assertIsNone(fp.normalize_record(missing_date, "austin", cutoff, set(), set(), True, stats))
        self.assertEqual(stats.records_filtered_old_or_inactive, 5)

        sparse_active = {
            "permit_number": "",
            "project_id": "",
            "issue_date": "2026-11-10T00:00:00.000",
            "status_current": "Active",
            "work_class": "",
            "permit_type_desc": "Building Permit",
            "description": "",
            "remodel_repair_sqft": "invalid",
            "total_new_add_sqft": None,
        }
        rec = fp.normalize_record(sparse_active, "austin", cutoff, set(), set(), True, stats)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["permit_number"], "Not Provided")
        self.assertEqual(rec["sqft"], 0)
        self.assertEqual(rec["source_url"], "Link Unavailable")
        site = fp.deduplicate_projects([rec])[0]
        fp.classify_sites([site], use_ai=False, stats=stats)
        self.assertEqual(site["primary_trade"], fp.UNCLASSIFIED_TRADE)

    def test_all_ai_providers_fail_gracefully_to_rules(self) -> None:
        raw = _make_raw_permit(description="No roof repairs. Install new TPO roof membrane", remodel_repair_sqft="4000")
        rec = fp.normalize_record(raw, "austin", date(2026, 11, 1), set(), set(), True, fp.RunStats())
        site = fp.deduplicate_projects([rec])[0]
        stats = fp.RunStats()

        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("All providers down")):
            with mock.patch.dict(
                os.environ,
                {"GROQ_API_KEY": "k1", "GEMINI_API_KEY": "k2", "OPENROUTER_API_KEY": "k3"},
                clear=False,
            ):
                fp.classify_sites([site], use_ai=True, stats=stats, sleep_fn=lambda _: None)

        self.assertEqual(stats.ai_batches_attempted, 1)
        self.assertEqual(stats.ai_batches_succeeded, 0)
        self.assertEqual(stats.ai_batches_failed_fallback, 1)
        self.assertEqual(site["primary_trade"], "Commercial Roofing & Exterior")


if __name__ == "__main__":
    unittest.main()
