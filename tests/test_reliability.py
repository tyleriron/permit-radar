"""Regression tests for lost leads, misleading scope, source failures and publication."""
import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from test_fetch_permits import fp, _make_raw_permit, FakeHTTPResponse, mock_source


def site_for(**fields):
    raw = _make_raw_permit(**fields)
    rec = fp.normalize_record(raw, "austin", date(2026, 11, 1), set(), set(), True,
                              fp.RunStats(), as_of_date=date(2026, 11, 14))
    if rec is None:
        return None
    site = fp.deduplicate_projects([rec])[0]
    fp.classify_sites([site], False, fp.RunStats())
    return site


class ReliabilityTests(unittest.TestCase):
    def run_main(self, root, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return fp.main(["--as-of", "2026-11-14", "--no-ai", "--output-dir", str(root / "out"),
                            "--checkpoint", str(root / "checkpoint.json"), *extra])

    def test_ds_is_not_demolition_and_residential_is_excluded(self):
        self.assertIsNone(site_for(permittype="DS", permit_type_desc="Driveway / Sidewalks"))
        self.assertIsNone(site_for(permit_class_mapped="Residential"))

    def test_minor_scope_cannot_use_whole_building_area_as_evidence(self):
        for description in ("Upgrade electrical service.", "Installation of 18 EV charging stalls.",
                            "Private event for members. Party and two tailgates.",
                            "DUMPSTER ENCLOSURE AT NEW GAS STATION.", "Replace rooftop HVAC unit."):
            with self.subTest(description=description):
                site = site_for(description=description, remodel_repair_sqft="20000")
                self.assertEqual(site["_applicable_list"], [])

    def test_interior_demo_does_not_imply_outdoor_fencing_or_final_cleaning(self):
        site = site_for(description="Interior non structural demolition of office suites",
                        work_class="Interior Demo Non-Structural")
        self.assertEqual(site["_applicable_list"], ["Dumpster & Demolition"])
        self.assertEqual(site["dumpster_fit"], "High")

    def test_roof_location_is_not_roof_replacement(self):
        site = site_for(description="Install rooftop air conditioning equipment.")
        self.assertNotIn("Commercial Roofing & Exterior", site["_applicable_list"])
        replacement = site_for(description="Remove existing roofing system and install shingles.")
        self.assertEqual(replacement["dumpster_fit"], "High")

    def test_comma_negations_and_suffix_negations(self):
        for description in ("No roofing, demolition or temporary fencing.",
                            "Demolition is not included. Roof repairs are excluded.",
                            "Demolition under a separate permit."):
            self.assertEqual(site_for(description=description)["_applicable_list"], [])
        self.assertEqual(site_for(description="Interior demolition; no dumpsters required.")["dumpster_fit"], "Not Matched")

    def test_long_description_keeps_late_scope_and_negations(self):
        text = "Administrative notes for this address. " * 25 + "Selective interior demolition; no roofing work."
        site = site_for(description=text)
        self.assertEqual(site["raw_description_clean"], text)
        self.assertEqual(site["dumpster_fit"], "High")
        self.assertNotIn("Commercial Roofing & Exterior", site["_applicable_list"])

    def test_area_fields_do_not_double_count_and_nonfinite_is_safe(self):
        self.assertEqual(fp.parse_sqft({"remodel_repair_sqft": "2735", "total_new_add_sqft": "2735"}), 2735)
        for val in ("NaN", "Infinity", "-Infinity", "-20", "bad"):
            self.assertEqual(fp.parse_sqft({"remodel_repair_sqft": val}), 0)

    def test_transitive_suite_and_date_bridges_do_not_merge_jobs(self):
        records = []
        for i, suite in enumerate(("100", "", "200")):
            raw = _make_raw_permit(permit_number=f"P{i}", project_id=str(900+i), masterpermitnum="12",
                                   permit_location="500 CONGRESS AVE" + (f" STE {suite}" if suite else ""),
                                   description="Interior remodel")
            records.append(fp.normalize_record(raw, "austin", date(2026,11,1), set(), set(), True, fp.RunStats()))
        self.assertEqual(len(fp.deduplicate_projects(records)), 3)

    def test_member_negation_cannot_cancel_other_permit_scope(self):
        records = []
        for i, desc in enumerate(("No demolition; interior finish-out", "Selective interior demolition")):
            raw = _make_raw_permit(permit_number=f"P{i}", project_id=str(900+i), masterpermitnum="12", description=desc)
            records.append(fp.normalize_record(raw, "austin", date(2026,11,1), set(), set(), True, fp.RunStats()))
        sites = fp.deduplicate_projects(records)
        self.assertEqual(len(sites), 1)
        fp.classify_sites(sites, False, fp.RunStats())
        self.assertEqual(sites[0]["dumpster_fit"], "High")
        self.assertIn("permit P1", sites[0]["dumpster_evidence"])
        self.assertEqual(len(sites[0]["member_source_urls"].split("; ")), 2)

    def test_missing_phone_link_or_date_conflict_goes_to_review(self):
        for fields in ({"link": None}, {"contractor_phone": "not a phone"},
                       {"applieddate": "2026-11-12T00:00:00"},
                       {"contractor_company_name": "", "contractor_full_name": "", "contractor_phone": "", "applicant_org": "Architect", "applicant_phone": "5125551234"}):
            site = site_for(**fields)
            self.assertEqual(site["lead_quality"], "Needs Review")
            self.assertEqual(fp.get_trade_matches([site], "Dumpster & Demolition"), [])

    def test_contractor_phone_not_replaced_by_applicant_organization(self):
        raw = _make_raw_permit(contractor_company_name="", applicant_org="Designer")
        provenance, _, name, phone = fp.extract_contact_info(raw)
        self.assertEqual(provenance, "Contractor Record")
        self.assertEqual(name, "Jane Doe")
        self.assertEqual(phone, "(512) 555-0100")

    def test_fingerprint_includes_contacts_dates_links_and_master(self):
        raw = _make_raw_permit()
        for field, val in {"contractor_full_name": "Changed", "applicant_phone": "5125550199",
                           "applieddate": "2020-01-01", "masterpermitnum": "42", "link": None}.items():
            self.assertNotEqual(fp.compute_permit_fingerprint(raw), fp.compute_permit_fingerprint(dict(raw, **{field:val})))

    def test_ai_cannot_invent_trade_scope_or_buyer_summary(self):
        site = site_for(description="No demolition. Upgrade electrical service.")
        fake = json.dumps({"results": [{"id":0,"primary_trade":"Dumpster & Demolition",
                          "applicable_trades":["Dumpster & Demolition"],"classification_evidence":"Invented demolition",
                          "plain_summary":"Needs a dumpster tomorrow"}]})
        with mock.patch.dict(os.environ, {"GROQ_API_KEY":"fake"}, clear=True), mock.patch.object(fp,"_call_openai_compat", return_value=fake):
            fp.classify_sites([site], True, fp.RunStats(), sleep_fn=lambda _:None)
        self.assertEqual(site["_applicable_list"], [])
        self.assertNotIn("tomorrow", site["buyer_brief"])
        self.assertIn("UNVERIFIED AI SUGGESTION", site["ai_review_suggestion"])

    def test_malformed_ai_results_are_ignored(self):
        for payload in ({"results":None}, {"results":[{"id":0,"applicable_trades":None}]},
                        {"results":[{"id":True,"applicable_trades":[{}]}]}):
            self.assertEqual(fp._parse_batch_response(json.dumps(payload),1), {})

    def test_exact_page_limit_is_complete_when_probe_empty(self):
        rows = [_make_raw_permit()]
        stats = fp.RunStats()
        with mock.patch("urllib.request.urlopen", side_effect=[FakeHTTPResponse(rows), FakeHTTPResponse([])]):
            self.assertEqual(len(fp.fetch_raw_permits("austin",date(2026,11,1),1,1,stats)), 1)
        self.assertFalse(stats.pagination_truncated)

    def test_invalid_or_repeated_pages_fail(self):
        for payload in ({"error":"Unavailable"}, [None], [{"permit_number":"P1"}]):
            with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse(payload)):
                with self.assertRaises(fp.DataQualityError):
                    fp.fetch_raw_permits("austin",date(2026,11,1),10,2,fp.RunStats())
        with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse([_make_raw_permit()])):
            with self.assertRaises(fp.DataQualityError):
                fp.fetch_raw_permits("austin",date(2026,11,1),1,2,fp.RunStats())

    def test_corrupt_checkpoint_stops_without_fetch_or_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/"checkpoint.json").write_text('{broken')
            with mock.patch.object(fp,"fetch_raw_permits") as fetch:
                self.assertEqual(self.run_main(root),1)
            fetch.assert_not_called()
            self.assertEqual((root/"checkpoint.json").read_text(),'{broken')

    def test_truncation_preserves_last_reports_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            rows=[_make_raw_permit()]
            with mock_source(rows): self.assertEqual(self.run_main(root),0)
            before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            rows.append(_make_raw_permit(project_id="900002",permit_number="2026-2 BP"))
            with mock_source(rows): self.assertEqual(self.run_main(root,"--page-size","1","--max-pages","1"),3)
            self.assertEqual(before,{p:p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_count_mismatch_schema_change_and_stale_source_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            rows=[_make_raw_permit()]
            with mock_source(rows), mock.patch.object(fp,"fetch_raw_permits", return_value=[]):
                self.assertEqual(self.run_main(root),1)
            with mock.patch("urllib.request.urlopen", return_value=FakeHTTPResponse({"columns":[]})):
                self.assertEqual(self.run_main(root),1)
            with mock.patch.object(fp,"check_source_metadata", return_value={"rowsUpdatedAt":1}):
                self.assertEqual(self.run_main(root),1)
            self.assertFalse((root/"checkpoint.json").exists())
            self.assertFalse((root/"out").exists())

    def test_source_revision_change_is_not_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            stamp=datetime.now(timezone.utc).timestamp()
            with mock_source([_make_raw_permit()]), mock.patch.object(fp,"check_source_metadata", side_effect=[{"rowsUpdatedAt":stamp},{"rowsUpdatedAt":stamp+1}]):
                self.assertEqual(self.run_main(root),1)
            self.assertFalse((root/"out").exists())

    def test_unchanged_rerun_keeps_full_report_and_empty_change_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for _ in range(2):
                with mock_source([_make_raw_permit()]): self.assertEqual(self.run_main(root),0)
            def rows(name):
                with (root/'out'/name).open(encoding='utf-8-sig') as f: return list(csv.DictReader(f))
            self.assertEqual(len(rows('austin_commercial_permits_full.csv')),1)
            self.assertEqual(rows('austin_new_or_updated.csv'),[])
            self.assertEqual(rows('changes/by_trade/dumpster_demolition_leads.csv'),[])

    def test_source_closure_removed_from_current_leads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with mock_source([_make_raw_permit()]): self.assertEqual(self.run_main(root),0)
            with mock_source([_make_raw_permit(status_current="Final")]): self.assertEqual(self.run_main(root),0)
            with (root/'out/austin_commercial_permits_full.csv').open(encoding='utf-8-sig') as f:
                self.assertEqual(list(csv.DictReader(f)),[])
            state=fp.load_checkpoint_state(root/'checkpoint.json')
            self.assertEqual(next(iter(state['permits'].values()))['status_current'],'Final')

    def test_output_write_failure_does_not_advance_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with mock_source([_make_raw_permit()]): self.assertEqual(self.run_main(root),0)
            before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with mock_source([_make_raw_permit(description="Selective demolition")]), mock.patch.object(fp,"write_csv",side_effect=OSError("Disk full")):
                self.assertEqual(self.run_main(root),1)
            self.assertEqual(before,{p:p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_publication_failure_rolls_back_already_replaced_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with mock_source([_make_raw_permit()]): self.assertEqual(self.run_main(root),0)
            before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
            real_replace=os.replace
            def fail_one(src,dest):
                if Path(dest)==root/'checkpoint.json' and str(src).endswith('.tmp'):
                    raise PermissionError('Checkpoint locked')
                return real_replace(src,dest)
            with mock_source([_make_raw_permit(description="Selective demolition")]), mock.patch.object(fp.os,"replace",side_effect=fail_one):
                self.assertEqual(self.run_main(root),1)
            self.assertEqual(before,{p:p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_lock_stops_concurrent_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'.out.lock').write_text('busy')
            with mock.patch.object(fp,'fetch_raw_permits') as fetch:
                self.assertEqual(self.run_main(root),1)
            fetch.assert_not_called()

    def test_offline_replay_cannot_write_live_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'saved.json'
            source.write_text(json.dumps([_make_raw_permit()]))
            self.assertEqual(self.run_main(root,'--input-json',str(source)),1)
            self.assertEqual(self.run_main(root,'--input-json',str(source),'--no-save-checkpoint'),0)
            self.assertFalse((root/'checkpoint.json').exists())
            manifest=json.loads((root/'out/run_manifest.json').read_text())
            self.assertEqual(manifest['data_mode'],'offline_replay')
            self.assertFalse(manifest['complete_source_window'])

    def test_manifest_hashes_match_published_files(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with mock_source([_make_raw_permit()]): self.assertEqual(self.run_main(root),0)
            manifest=json.loads((root/'out/run_manifest.json').read_text())
            for name,digest in manifest['files_sha256'].items():
                self.assertEqual(hashlib.sha256((root/'out'/name).read_bytes()).hexdigest(),digest)

    def test_independent_verifier_detects_altered_missing_and_replayed_reports(self):
        import verify_reports
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with mock_source([_make_raw_permit()]): self.assertEqual(self.run_main(root),0)
            self.assertEqual(verify_reports.verify(root/'out')['sites'],1)
            target=root/'out/by_trade/dumpster_demolition_leads.csv'
            original=target.read_bytes()
            target.write_bytes(original+b'altered')
            with self.assertRaisesRegex(ValueError,'changed|mixed'):
                verify_reports.verify(root/'out')
            target.write_bytes(original)
            target.unlink()
            with self.assertRaises(OSError): verify_reports.verify(root/'out')
            target.write_bytes(original)
            manifest_path=root/'out/run_manifest.json'
            manifest=json.loads(manifest_path.read_text())
            manifest['data_mode']='offline_replay'
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError,'replay'):
                verify_reports.verify(root/'out')

    def test_unknown_or_unprocessed_permits_cannot_be_marked_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            ckpt=Path(tmp)/'checkpoint.json'
            state=fp._empty_checkpoint_state()
            fp.record_collected_permits(state,[_make_raw_permit()],'austin')
            fp._write_checkpoint_state(ckpt,state)
            for pnum in ('unknown','2026-100001 BP'):
                with self.assertRaises(fp.DataQualityError): fp.mark_permits_delivered(ckpt,[pnum])

    def test_changed_permit_revokes_approval_and_records_delivery_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            raw=_make_raw_permit()
            with mock_source([raw]): self.assertEqual(self.run_main(root),0)
            fp.mark_permits_approved(root/'checkpoint.json',[raw['permit_number']])
            fp.mark_permits_delivered(root/'checkpoint.json',[raw['permit_number']])
            with mock_source([dict(raw,contractor_phone='5125550199')]): self.assertEqual(self.run_main(root),0)
            entry=next(iter(fp.load_checkpoint_state(root/'checkpoint.json')['permits'].values()))
            self.assertEqual(entry['stage'],'processed')
            self.assertEqual(entry['delivered_at'],'')
            self.assertTrue(entry['change_history'][-1]['previous_delivered_at'])

    def test_members_with_separate_fire_areas_do_not_automatically_add_area(self):
        records=[]
        for i in (1,2):
            raw=_make_raw_permit(permit_number=f'P{i}',project_id=str(900+i),masterpermitnum='12',
                                 description=f'New building fire area {i}', remodel_repair_sqft='6000')
            records.append(fp.normalize_record(raw,'austin',date(2026,11,1),set(),set(),True,fp.RunStats()))
        sites=fp.deduplicate_projects(records)
        self.assertEqual(len(sites),1)
        self.assertEqual(sites[0]['sqft'],6000)

    def test_missed_run_extends_recovery_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            state=fp._empty_checkpoint_state()
            state['last_as_of']='2026-10-01'
            fp._write_checkpoint_state(root/'checkpoint.json',state)
            with mock_source([_make_raw_permit()]): self.assertEqual(self.run_main(root),0)
            manifest=json.loads((root/'out/run_manifest.json').read_text())
            self.assertEqual(manifest['window_start'],'2026-09-24')


if __name__=='__main__':
    unittest.main()
