#!/usr/bin/env python3
"""Independently validate a Permit Radar report bundle before handing it to a buyer."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from fetch_permits import TRADE_FILES


def verify(directory: Path, allow_replay: bool = False) -> dict:
    root = directory.resolve()
    manifest = json.loads((root / "run_manifest.json").read_text(encoding="utf-8"))
    if not allow_replay and (manifest.get("data_mode") != "live" or not manifest.get("complete_source_window")):
        raise ValueError("Bundle is an offline replay or incomplete source window")
    hashes = manifest.get("files_sha256")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("Missing file hashes")
    for name, digest in hashes.items():
        path = (root / name).resolve()
        if root not in path.parents:
            raise ValueError("Invalid path in manifest")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"File changed or mixed report generations: {name}")
    required = {"austin_commercial_permits_full.csv", "austin_new_or_updated.csv", "source_records.json"}
    required.update(f"by_trade/{f}" for f in TRADE_FILES.values())
    required.update(f"changes/by_trade/{f}" for f in TRADE_FILES.values())
    if not required.issubset(hashes):
        raise ValueError("Report bundle is missing required files")

    def rows(name):
        with (root / name).open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    master = rows("austin_commercial_permits_full.csv")
    identities = {r["permit_number"] for r in master}
    if len(identities) != len(master):
        raise ValueError("Duplicate primary permits in master")
    members = [p for r in master for p in r["member_permit_numbers"].split("; ") if p != "Not Provided"]
    if len(set(members)) != len(members):
        raise ValueError("A permit belongs to more than one site")
    changes = {r["permit_number"] for r in rows("austin_new_or_updated.csv")}
    if not changes.issubset(identities):
        raise ValueError("Changes refer to missing master records")
    for trade, filename in TRADE_FILES.items():
        for prefix in ("", "changes/"):
            feed = rows(prefix + "by_trade/" + filename)
            expected = manifest["changes_trade_counts" if prefix else "trade_counts"][trade]
            if len(feed) != expected:
                raise ValueError(f"Wrong count in {prefix}{filename}")
            for row in feed:
                if (row["lead_quality"] != "Qualified opportunity" or row["status_current"].lower() != "active"
                        or row["target_trade"] != trade or not row["target_trade_basis"]
                        or row["permit_number"] not in (changes if prefix else identities)
                        or not row["source_url"].startswith("https://abc.austintexas.gov/")):
                    raise ValueError(f"Invalid customer lead: {row['permit_number']}")
    return {"run_id": manifest["run_id"], "sites": len(master), "changes": len(changes), "files": len(hashes)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path(__file__).resolve().parent / "output")
    parser.add_argument("--allow-replay", action="store_true")
    args = parser.parse_args()
    try:
        print("Verified:", json.dumps(verify(args.directory, args.allow_replay)))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Verification failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
