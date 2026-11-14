#!/usr/bin/env python3
"""Permit Radar: auditable Austin commercial building permit opportunities.

Source facts, rule-based trade fit, review flags, incremental changes and
repeatable exports. No messages or outreach are sent. Python 3.10+; stdlib only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from contextlib import contextmanager, ExitStack
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_CHECKPOINT_PATH = BASE_DIR / "state" / "checkpoint.json"
PIPELINE_VERSION = "4.0.0"

UNCLASSIFIED_TRADE = "Unclassified / Needs Review"

CITY_CONFIGS = {
    "austin": {
        "label": "Austin, TX",
        "endpoint": "https://data.austintexas.gov/resource/3syk-w9eu.json",
        "metadata_url": "https://data.austintexas.gov/api/views/3syk-w9eu.json",
        "allowed_link_hosts": ("abc.austintexas.gov",),
        "allowed_link_path": "/web/permit/public-search-other",
        "detail_url_template": (
            "https://abc.austintexas.gov/web/permit/public-search-other"
            "?t_detail=1&t_selected_folderrsn={project_id}"
        ),
        "order": "issue_date DESC, project_id DESC, permit_number DESC",
    }
}

TRADE_FILES = {
    "Dumpster & Demolition": "dumpster_demolition_leads.csv",
    "Temp Fencing & Site Services": "temp_fencing_site_services_leads.csv",
    "Post-Construction Cleaning": "post_construction_cleaning_leads.csv",
    "Commercial Roofing & Exterior": "commercial_roofing_exterior_leads.csv",
}

ALL_PRIMARY_TRADES = list(TRADE_FILES.keys()) + [UNCLASSIFIED_TRADE]

QUALITY_COLUMNS = [
    "change_status", "first_seen_at", "last_checked_at", "issue_age_days",
    "lead_quality", "review_reasons", "dumpster_fit", "dumpster_evidence",
    "trade_evidence", "link_status", "member_source_urls",
    "contractor_trade", "applicant_company", "applicant_contact", "applicant_phone",
    "status_date", "expires_date", "maps_url", "reported_remodel_sqft", "reported_new_add_sqft",
]

MASTER_COLUMNS = [
    "issue_date",
    "applied_date",
    "timing_status",
    "status_current",
    "permit_number",
    "member_permit_numbers",
    "permit_count_at_site",
    "project_id",
    "member_project_ids",
    "masterpermitnum",
    "permit_type",
    "work_class",
    "sqft",
    "max_single_permit_sqft",
    "sqft_basis",
    "project_scale",
    "address",
    "city",
    "zip_code",
    "jurisdiction",
    "primary_trade",
    "applicable_trades",
    "classification_evidence",
    "plain_summary",
    "contact_provenance",
    "contractor_company",
    "contractor_contact",
    "contractor_phone",
    "buyer_brief",
    "optional_gc_call_opener",
    "source_url",
    "raw_description_clean",
]
MASTER_COLUMNS += QUALITY_COLUMNS + ["ai_review_suggestion"]

TRADE_COLUMNS = [
    "issue_date",
    "applied_date",
    "timing_status",
    "status_current",
    "permit_number",
    "member_permit_numbers",
    "permit_count_at_site",
    "project_id",
    "masterpermitnum",
    "work_class",
    "sqft",
    "max_single_permit_sqft",
    "sqft_basis",
    "project_scale",
    "address",
    "city",
    "zip_code",
    "jurisdiction",
    "target_trade",
    "classification_evidence",
    "plain_summary",
    "contact_provenance",
    "contractor_company",
    "contractor_contact",
    "contractor_phone",
    "buyer_brief",
    "optional_gc_call_opener",
    "source_url",
    "raw_description_clean",
]
TRADE_COLUMNS += QUALITY_COLUMNS + ["target_trade_basis"]

NOISE_PATTERN = re.compile(
    r"\b(?:rooftop solar|solar pv|solar array|photovoltaic|backfed breaker|"
    r"wall sign|monument sign|illuminated sign|signage|backflow|irrigation)\b",
    re.IGNORECASE,
)

CLAUSE_SPLIT_PATTERN = re.compile(
    r"[.;\n\u2014]+|,\s*(?=(?:install|replace|construct|demolish|remove|new)\b)|\s+--\s+|\s+\b(?:but|however|although|whereas|except|while)\b\s+|"
    r"\s+\band\s+(?=(?:install|replace|construct|build|perform|provide|add|demolish|remove|new)\b)",
    re.IGNORECASE,
)

NEG_PREFIX = (
    r"(?:\b(?:no|not|without|excluding|excludes)\b|"
    r"\bdoes\s+not\s+(?:include|contain|involve|have|require)\b|"
    r"\bdo\s+not\s+(?:include|contain|involve|have|require)\b|"
    r"\bdoesn'?t\s+(?:include|contain|involve|have|require)\b|"
    r"\bnon[-\s]+(?=roof|roofing|demo|demolition|fencing|cleaning))"
)

ROOF_NEGATION_RE = re.compile(
    rf"{NEG_PREFIX}[^.;\n]{{0,100}}\b(?:roof|roofing|reroof|re-roof|exterior|canopy|facade|siding)\b",
    re.IGNORECASE,
)
DEMO_NEGATION_RE = re.compile(
    rf"{NEG_PREFIX}[^.;\n]{{0,100}}\b(?:demo|demolition|dumpster|dumpsters|haul[-\s]?off|tear[-\s]?off|debris)\b",
    re.IGNORECASE,
)
FENCE_NEGATION_RE = re.compile(
    rf"{NEG_PREFIX}[^.;\n]{{0,100}}\b(?:fencing|fence|site\s+work|exterior\s+work|ground\s+disturbance|porta[-\s]?potty|portable\s+restroom)\b",
    re.IGNORECASE,
)
CLEAN_NEGATION_RE = re.compile(
    rf"{NEG_PREFIX}[^.;\n]{{0,100}}\b(?:finish[-\s]?out|interior\s+finish|cleaning|cleanup|janitorial)\b",
    re.IGNORECASE,
)

FULL_NEGATION_CLAUSE_RE = re.compile(
    rf"{NEG_PREFIX}[^.;\n]*",
    re.IGNORECASE,
)


@dataclass
class RunStats:
    """Tracks API calls, retries, pagination, links, filtering counts, and fallback behavior."""

    api_requests: int = 0
    api_retries: int = 0
    api_errors: list[str] = field(default_factory=list)
    pages_fetched: int = 0
    pagination_truncated: bool = False
    latest_source_date: str = "None"
    ai_batches_attempted: int = 0
    ai_batches_succeeded: int = 0
    ai_batches_failed_fallback: int = 0
    records_fetched: int = 0
    records_filtered_old_or_inactive: int = 0
    records_filtered_noise: int = 0
    records_skipped_checkpoint: int = 0
    records_updated_since_checkpoint: int = 0
    records_valid_links: int = 0
    records_unverified_links: int = 0
    sites_after_dedup: int = 0
    sites_classified: int = 0
    sites_needs_review: int = 0
    sites_valid_links: int = 0
    sites_unverified_links: int = 0
    warnings: list[str] = field(default_factory=list)
    source_updated_at: str = ""
    source_latest_issue_date: str = ""
    source_row_count: int = 0
    records_filtered_wrong_scope: int = 0
    records_unchanged: int = 0
    qualified_sites: int = 0
    new_or_updated_sites: int = 0


class DataQualityError(ValueError):
    """Source or state cannot safely support a complete customer report."""


def source_headers() -> dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": f"PermitRadar/{PIPELINE_VERSION}"}
    token = os.environ.get("SOCRATA_APP_TOKEN", "").strip()
    if token:
        headers["X-App-Token"] = token
    return headers


def load_dotenv(env_path: Path) -> None:
    """Load KEY=VALUE pairs from .env into os.environ without external deps."""
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if key and val:
            os.environ.setdefault(key, val)


def sanitize_csv_value(val: object) -> str:
    """Neutralize spreadsheet formula injection payloads in CSV cells."""
    text = "" if val is None else str(val)
    stripped = text.lstrip(" \t\r\n")
    if stripped and stripped[0] in ("=", "+", "-", "@"):
        return "'" + text
    if text and text[0] in ("\t", "\r"):
        return "'" + text
    return text


def fetch_with_retry(
    url: str,
    headers: dict[str, str],
    stats: RunStats,
    data: bytes | None = None,
    method: str = "GET",
    timeout: int = 25,
    max_retries: int = 3,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bytes:
    """Execute an HTTP request with exponential backoff on transient failures."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        retry_after = 0.0
        stats.api_requests += 1
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            try:
                retry_after = min(max(float(exc.headers.get("Retry-After", 0)), 0), 30)
            except (TypeError, ValueError, AttributeError):
                pass
            retryable = exc.code == 429 or exc.code >= 500
            if not retryable or attempt == max_retries:
                stats.api_errors.append(f"HTTP {exc.code} on {method} {url.split('?')[0]}")
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
            if attempt == max_retries:
                stats.api_errors.append(f"Network error on {method} {url.split('?')[0]}: {exc}")
                raise
        stats.api_retries += 1
        sleep_fn(max(retry_after, min(1.5 * (2**attempt), 8.0)))
    raise RuntimeError(f"Unreachable retry failure: {last_exc}")


def fetch_raw_permits(
    city: str,
    cutoff_date: date,
    page_size: int,
    max_pages: int,
    stats: RunStats,
    sleep_fn: Callable[[float], None] = time.sleep,
    as_of_date: date | None = None,
) -> list[dict]:
    """Fetch commercial building permits. Austin DS is driveway/sidewalk, NOT demolition.

    Fetch all statuses so closures within the issue window are recorded in state.
    The caller checks the dataset revision before/after offset pagination.
    """
    cfg = CITY_CONFIGS[city]
    cutoff_iso = f"{cutoff_date.isoformat()}T00:00:00.000"
    where_parts = [
        "permit_class_mapped='Commercial'",
        "permittype='BP'",
        f"issue_date >= '{cutoff_iso}'",
    ]
    if as_of_date is not None:
        as_of_iso = f"{as_of_date.isoformat()}T23:59:59.999"
        where_parts.append(f"issue_date <= '{as_of_iso}'")
    where_clause = " AND ".join(where_parts)

    all_rows: list[dict] = []
    headers = source_headers()
    seen_keys: set[str] = set()

    for page in range(max_pages):
        params = urllib.parse.urlencode(
            {
                "$where": where_clause,
                "$order": cfg["order"],
                "$limit": str(page_size),
                "$offset": str(page * page_size),
            }
        )
        url = f"{cfg['endpoint']}?{params}"
        raw_bytes = fetch_with_retry(
            url,
            headers=headers,
            stats=stats,
            timeout=25,
            max_retries=3,
            sleep_fn=sleep_fn,
        )
        page_rows = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(page_rows, list) or any(not isinstance(r, dict) for r in page_rows):
            raise DataQualityError("Socrata returned an invalid page; expected a list of permit objects")
        if len(page_rows) > page_size:
            raise DataQualityError("Socrata returned more rows than requested")
        if not page_rows:
            break
        for row in page_rows:
            if any(not clean_text(row.get(k)) for k in ("issue_date", "status_current", "permit_class_mapped", "permittype")):
                raise DataQualityError("Source record is missing a required date, status or permit type")
            key = get_permit_key(row.get("permit_number", ""), row.get("project_id", ""))
            if key == "unknown" or key in seen_keys:
                raise DataQualityError("Missing or repeated permit identity during pagination; retry the fetch")
            seen_keys.add(key)
        stats.pages_fetched += 1
        all_rows.extend(page_rows)
        if len(page_rows) < page_size:
            break
        if page == max_pages - 1:
            # An exactly full final page is complete if a one-row probe is empty.
            probe_params = urllib.parse.urlencode({"$where": where_clause, "$order": cfg["order"],
                                                  "$limit": "1", "$offset": str(len(all_rows))})
            probe = json.loads(fetch_with_retry(f"{cfg['endpoint']}?{probe_params}", headers,
                                               stats, sleep_fn=sleep_fn).decode("utf-8"))
            if not isinstance(probe, list) or any(not isinstance(r, dict) for r in probe):
                raise DataQualityError("Socrata returned an invalid pagination probe")
            stats.pagination_truncated = bool(probe)
            if probe:
                stats.warnings.append(
                f"TRUNCATED DATASET: Reached --max-pages={max_pages} with a full page "
                f"({page_size} rows, {len(all_rows)} total fetched); additional matching "
                "records may exist. Increase --max-pages or --page-size."
            )

    stats.records_fetched = len(all_rows)
    return all_rows


REQUIRED_SOURCE_FIELDS = {
    "permit_number", "project_id", "permittype", "permit_class_mapped", "issue_date",
    "status_current", "description", "work_class", "permit_location", "link",
    "contractor_company_name", "contractor_phone", "applieddate",
    "remodel_repair_sqft", "total_new_add_sqft",
}


def check_source_metadata(city: str, stats: RunStats) -> dict:
    """Check schema and retain the provider's revision, not a guessed refresh time."""
    raw = fetch_with_retry(CITY_CONFIGS[city]["metadata_url"], source_headers(), stats)
    metadata = json.loads(raw.decode("utf-8"))
    if not isinstance(metadata, dict) or not isinstance(metadata.get("columns"), list):
        raise DataQualityError("Invalid Socrata metadata")
    fields = {c.get("fieldName") for c in metadata["columns"] if isinstance(c, dict)}
    missing = REQUIRED_SOURCE_FIELDS - fields
    if missing:
        raise DataQualityError(f"Source schema changed; missing columns: {', '.join(sorted(missing))}")
    updated = metadata.get("rowsUpdatedAt")
    if not isinstance(updated, (int, float)) or not math.isfinite(updated) or updated <= 0:
        raise DataQualityError("Source refresh timestamp is missing or invalid")
    stats.source_updated_at = datetime.fromtimestamp(updated, timezone.utc).isoformat()
    return metadata


def check_source_totals(city: str, cutoff: date, as_of: date, stats: RunStats) -> int:
    """Count the exact window and check most recent issuance, including quiet windows."""
    where = ("permit_class_mapped='Commercial' AND permittype='BP' "
             f"AND issue_date <= '{as_of.isoformat()}T23:59:59.999'")
    params = urllib.parse.urlencode({"$select": "max(issue_date) as latest", "$where": where})
    latest = json.loads(fetch_with_retry(f"{CITY_CONFIGS[city]['endpoint']}?{params}",
                                        source_headers(), stats).decode("utf-8"))
    if not isinstance(latest, list) or len(latest) != 1 or not isinstance(latest[0], dict):
        raise DataQualityError("Invalid latest-issuance response")
    dt = parse_iso_date(latest[0].get("latest"))
    if dt is None:
        raise DataQualityError("Source has no parseable commercial issuance date")
    stats.source_latest_issue_date = dt.isoformat()
    where += f" AND issue_date >= '{cutoff.isoformat()}T00:00:00.000'"
    params = urllib.parse.urlencode({"$select": "count(*) as n", "$where": where})
    count = json.loads(fetch_with_retry(f"{CITY_CONFIGS[city]['endpoint']}?{params}",
                                       source_headers(), stats).decode("utf-8"))
    try:
        stats.source_row_count = int(count[0]["n"])
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        raise DataQualityError("Invalid source count response") from exc
    if stats.source_row_count < 0:
        raise DataQualityError("Negative source count")
    return stats.source_row_count


def clean_text(val: object) -> str:
    """Collapse internal whitespace and strip surrounding spaces."""
    if val is None:
        return ""
    return re.sub(r"\s+", " ", str(val).replace("\x00", "")).strip()


def clean_id(val: object) -> str:
    """Clean numeric or string permit/project identifiers."""
    text = clean_text(val)
    if text.endswith(".0") and text[:-2].isdigit():
        return text[:-2]
    return text


def clean_description(raw_text: object) -> str:
    """Strip Austin ePlan boilerplate lines and collapse excess whitespace."""
    if not raw_text:
        return ""
    cleaned = re.sub(
        r"(?:\r?\n)*Building Permit\s*(?:\r?\n)*\s*-\s*Permit AddCycle tab",
        "",
        str(raw_text),
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"^\s*ePlan:\s*", "", cleaned, flags=re.IGNORECASE)
    # Preserve clause boundaries before collapsing whitespace.
    cleaned = re.sub(r"[\r\n]+", "; ", cleaned)
    return clean_text(cleaned)


def parse_iso_date(raw_date: object) -> date | None:
    """Parse YYYY-MM-DD or ISO timestamp string into a date object."""
    if not raw_date:
        return None
    date_part = str(raw_date).strip().split("T", 1)[0]
    try:
        return date.fromisoformat(date_part)
    except ValueError:
        return None


def format_phone(raw_phone: object) -> str:
    """Normalize a 10-digit US phone string into (XXX) XXX-XXXX."""
    if not raw_phone:
        return ""
    digits = re.sub(r"\D", "", str(raw_phone))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return str(raw_phone).strip()


def parse_sqft(record: dict) -> int:
    """Use the maximum reported area; remodel and new/add fields may overlap."""
    total = 0
    for field_name in ("remodel_repair_sqft", "total_new_add_sqft"):
        val = record.get(field_name)
        if val in (None, ""):
            continue
        try:
            parsed = int(float(val))
            if parsed > 0:
                total = max(total, parsed)
        except (TypeError, ValueError, OverflowError):
            continue
    return total


def is_verified_source_url(source_url: str) -> bool:
    """Return True only when source_url is a verified official http(s) link."""
    return bool(source_url) and source_url.startswith(("http://", "https://"))


def validate_source_url(raw: dict, project_id: str, city: str) -> str:
    """Validate that source link is present, official, and points to this exact project_id."""
    link_obj = raw.get("link")
    candidate = ""
    if isinstance(link_obj, dict):
        candidate = clean_text(link_obj.get("url"))
    elif isinstance(link_obj, str):
        candidate = clean_text(link_obj)

    if not candidate:
        return "Link Unavailable"

    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or "." not in parsed.netloc:
        return "Link Unavailable"

    cfg = CITY_CONFIGS[city]
    allowed_hosts = tuple(h.lower() for h in cfg.get("allowed_link_hosts", ()))
    if allowed_hosts and parsed.netloc.lower() not in allowed_hosts:
        return "Unverified Link — Flagged for Manual Review"

    allowed_path = cfg.get("allowed_link_path", "")
    if allowed_path and parsed.path.rstrip("/") != allowed_path.rstrip("/"):
        return "Unverified Link — Flagged for Manual Review"

    qs = urllib.parse.parse_qs(parsed.query)
    selected = qs.get("t_selected_folderrsn") or [""]
    if len(selected) != 1:
        return "Unverified Link - Flagged for Manual Review"
    folder_rsn = clean_id(selected[0])
    if not (project_id.isdigit() and int(project_id) > 0 and folder_rsn == project_id):
        return "Unverified Link — Flagged for Manual Review"

    return urllib.parse.urlunparse(parsed._replace(scheme="https", fragment=""))


def compute_timing_status(applied_dt: date | None, issue_dt: date) -> str:
    """Distinguish newly applied & issued permits from >1yr delayed filings."""
    if applied_dt is None:
        return "Issued Recently (Application Date Unlisted)"
    lag_days = (issue_dt - applied_dt).days
    if lag_days < 0:
        return "Date Conflict (Application After Issuance - Review)"
    if lag_days <= 365:
        return "Newly Issued (Applied Within 1 Yr)"
    return "Delayed Issue (Applied >1 Yr Ago — Verify If Renewal/Phase)"


def compute_project_scale(sqft: int) -> str:
    """Categorize commercial job size based on square footage."""
    if sqft >= 50000:
        return "Mega (50k+ sq ft)"
    if sqft >= 10000:
        return "Large (10k-49k sq ft)"
    if sqft >= 2500:
        return "Mid-Size (2.5k-10k sq ft)"
    return "Standard Commercial"


def is_low_value_noise(desc_clean: str, work_class: str, sqft: int) -> bool:
    """Return True for standalone rooftop solar/signage/backflow noise."""
    combined = desc_clean
    if NOISE_PATTERN.search(combined) and not re.search(
        r"\b(?:demolition|demo|finish out|finish-out|new construction|reroof|re-roof|remodel|renovation|addition)\b",
        combined,
        re.IGNORECASE,
    ):
        return True
    return False


LIFECYCLE_ORDER = {
    "collected": 0,
    "processed": 1,
    "approved": 2,
    "delivered": 3,
}


def get_permit_key(permit_number: str, project_id: str) -> str:
    """Return canonical per-permit key; fall back to project_id only if permit_number missing."""
    pnum = clean_text(permit_number)
    pid = clean_id(project_id)
    if pnum and pnum != "Not Provided":
        return f"permit:{pnum}"
    if pid and pid != "Not Provided":
        return f"project:{pid}"
    return "unknown"


def compute_permit_fingerprint(record: dict) -> str:
    """Compute a deterministic SHA-256 fingerprint across canonical permit fields."""
    issue_dt = parse_iso_date(record.get("issue_date"))
    issue_str = issue_dt.isoformat() if issue_dt else clean_text(record.get("issue_date"))
    pnum = clean_text(record.get("permit_number")) or "Not Provided"
    pid = clean_id(record.get("project_id")) or "Not Provided"
    status_cur = clean_text(record.get("status_current")) or "Unknown"
    work_cls = clean_text(record.get("work_class") or record.get("permit_class"))
    sqft_val = record["sqft"] if isinstance(record.get("sqft"), int) else parse_sqft(record)
    address_val = (
        clean_text(record.get("address") or record.get("permit_location") or record.get("original_address1"))
        or "Address Not Listed"
    )
    if "raw_description_clean" in record:
        desc_val = clean_text(record.get("raw_description_clean"))
    else:
        desc_val = clean_description(record.get("description"))

    if "contractor_company" in record and "contractor_phone" in record:
        comp_val = clean_text(record.get("contractor_company"))
        phone_val = clean_text(record.get("contractor_phone"))
    else:
        _, comp_val, _, phone_val = extract_contact_info(record)

    payload = json.dumps(
        [pnum, pid, issue_str, status_cur, work_cls, sqft_val, address_val, comp_val, phone_val, desc_val,
         {k: record.get(k) for k in (
             "applieddate", "statusdate", "expiresdate", "completed_date", "masterpermitnum",
             "permit_class_mapped", "permittype", "original_city", "original_zip", "jurisdiction",
             "link", "contractor_full_name", "contractor_trade", "applicant_org",
             "applicant_full_name", "applicant_phone", "remodel_repair_sqft", "total_new_add_sqft")}],
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_persistence_summary(state: dict) -> dict[str, int]:
    """Return exact counts of tracked permits across collected/processed/approved/delivered."""
    counts = {
        "collected": 0,
        "processed": 0,
        "approved": 0,
        "delivered": 0,
        "total_tracked": 0,
    }
    permits = state.get("permits", {})
    if not isinstance(permits, dict):
        return counts
    for entry in permits.values():
        if not isinstance(entry, dict):
            continue
        stage = entry.get("stage", "collected")
        if stage in counts:
            counts[stage] += 1
        counts["total_tracked"] += 1
    return counts


def _empty_checkpoint_state() -> dict:
    return {
        "version": 3,
        "pipeline_version": PIPELINE_VERSION,
        "last_run_at": "",
        "last_as_of": "",
        "dataset_complete": True,
        "permits": {},
        "seen_project_ids": [],
        "seen_permit_numbers": [],
        "summary_counts": {
            "collected": 0,
            "processed": 0,
            "approved": 0,
            "delivered": 0,
            "total_tracked": 0,
        },
    }


def load_checkpoint_state(path: Path) -> dict:
    """Load structured checkpoint state with per-permit lifecycle tracking."""
    state = _empty_checkpoint_state()
    if not path.is_file():
        return state
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataQualityError(f"Cannot read checkpoint {path}; restore a backup before running") from exc
    if not isinstance(data, dict):
        raise DataQualityError("Checkpoint must contain a JSON object")
    if data.get("version", 1) not in (1, 2, 3):
        raise DataQualityError("Unsupported checkpoint version")

    permits_raw = data.get("permits", {})
    if not isinstance(permits_raw, dict) or any(
        not isinstance(v, dict) or v.get("stage") not in LIFECYCLE_ORDER
        or not isinstance(v.get("change_history", []), list)
        for v in permits_raw.values()
    ):
        raise DataQualityError("Checkpoint permit entries are malformed")
    for name in ("seen_project_ids", "seen_permit_numbers"):
        if not isinstance(data.get(name, []), list):
            raise DataQualityError(f"Checkpoint {name} must be a list")
    state["permits"] = permits_raw
    state["last_run_at"] = str(data.get("last_run_at", ""))
    state["last_as_of"] = str(data.get("last_as_of", ""))
    state["dataset_complete"] = bool(data.get("dataset_complete", True))
    state["seen_project_ids"] = [str(x) for x in data.get("seen_project_ids", []) if x]
    state["seen_permit_numbers"] = [str(x) for x in data.get("seen_permit_numbers", []) if x]
    state["summary_counts"] = get_persistence_summary(state)
    return state


def load_checkpoint(path: Path) -> tuple[set[str], set[str]]:
    """Load previously processed project_ids and permit_numbers from checkpoint JSON."""
    state = load_checkpoint_state(path)
    seen_pids = {str(x) for x in state.get("seen_project_ids", []) if x}
    seen_pnums = {str(x) for x in state.get("seen_permit_numbers", []) if x}
    for entry in state.get("permits", {}).values():
        stage = entry.get("stage", "collected")
        if LIFECYCLE_ORDER.get(stage, 0) >= LIFECYCLE_ORDER["processed"]:
            pnum = clean_text(entry.get("permit_number"))
            pid = clean_id(entry.get("project_id"))
            if pnum and pnum != "Not Provided":
                seen_pnums.add(pnum)
            if pid and pid != "Not Provided":
                seen_pids.add(pid)
    return seen_pids, seen_pnums


def _write_checkpoint_state(path: Path, state: dict) -> None:
    seen_pids = set(state.get("seen_project_ids", []))
    seen_pnums = set(state.get("seen_permit_numbers", []))
    for entry in state.get("permits", {}).values():
        if not isinstance(entry, dict):
            continue
        stage = entry.get("stage", "collected")
        if LIFECYCLE_ORDER.get(stage, 0) >= LIFECYCLE_ORDER["processed"]:
            pid = clean_id(entry.get("project_id"))
            pnum = clean_text(entry.get("permit_number"))
            if pid and pid != "Not Provided":
                seen_pids.add(pid)
            if pnum and pnum != "Not Provided":
                seen_pnums.add(pnum)

    state["seen_project_ids"] = sorted(seen_pids)[-10000:]
    state["seen_permit_numbers"] = sorted(seen_pnums)[-10000:]
    state["summary_counts"] = get_persistence_summary(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = 3
    state["pipeline_version"] = PIPELINE_VERSION
    atomic_write_text(path, json.dumps(state, indent=2, sort_keys=True))


def record_collected_permits(state: dict, raw_records: list[dict], city: str) -> int:
    """Record every raw Socrata record at stage='collected', detecting fingerprint changes."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    permits = state.setdefault("permits", {})
    changed_or_new = 0

    for raw in raw_records:
        pnum = clean_text(raw.get("permit_number")) or "Not Provided"
        pid = clean_id(raw.get("project_id")) or "Not Provided"
        key = get_permit_key(pnum, pid)
        if key == "unknown":
            continue

        new_fp = compute_permit_fingerprint(raw)
        issue_dt = parse_iso_date(raw.get("issue_date"))
        issue_str = issue_dt.isoformat() if issue_dt else ""
        status_cur = clean_text(raw.get("status_current")) or "Unknown"
        source_url = validate_source_url(raw, pid, city)
        existing = permits.get(key)

        if existing is None:
            permits[key] = {
                "permit_key": key,
                "permit_number": pnum,
                "project_id": pid,
                "stage": "collected",
                "fingerprint": new_fp,
                "issue_date": issue_str,
                "status_current": status_cur,
                "link_verified": is_verified_source_url(source_url),
                "collected_at": now_iso,
                "processed_at": "",
                "approved_at": "",
                "delivered_at": "",
                "last_seen_at": now_iso,
                "change_detected": False,
                "change_history": [],
            }
            changed_or_new += 1
        elif existing.get("fingerprint") != new_fp:
            history = list(existing.get("change_history", []))
            history.append(
                {
                    "detected_at": now_iso,
                    "previous_fingerprint": existing.get("fingerprint", ""),
                    "new_fingerprint": new_fp,
                    "previous_stage": existing.get("stage", "collected"),
                    "previous_approved_at": existing.get("approved_at", ""),
                    "previous_delivered_at": existing.get("delivered_at", ""),
                }
            )
            existing["fingerprint"] = new_fp
            existing["issue_date"] = issue_str
            existing["status_current"] = status_cur
            existing["link_verified"] = is_verified_source_url(source_url)
            existing["stage"] = "collected"
            existing["approved_at"] = ""
            existing["delivered_at"] = ""
            existing["last_seen_at"] = now_iso
            existing["change_detected"] = True
            existing["change_history"] = history[-20:]
            changed_or_new += 1
        else:
            existing["last_seen_at"] = now_iso

    state["summary_counts"] = get_persistence_summary(state)
    return changed_or_new


def record_processed_sites(state: dict, sites: list[dict]) -> int:
    """Promote processed permit records to stage='processed' while preserving approved/delivered."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    permits = state.setdefault("permits", {})
    promoted = 0

    for site in sites:
        member_meta = site.get("_member_records_meta") or [
            {
                "permit_number": site["permit_number"],
                "project_id": site["project_id"],
                "fingerprint": compute_permit_fingerprint(site),
                "issue_date": site["issue_date"],
                "status_current": site["status_current"],
                "link_verified": site.get("link_verified", is_verified_source_url(site["source_url"])),
            }
        ]
        for meta in member_meta:
            pnum = meta["permit_number"]
            pid = meta["project_id"]
            key = get_permit_key(pnum, pid)
            if key == "unknown":
                continue

            fp_val = meta["fingerprint"]
            existing = permits.get(key)
            if existing is None:
                permits[key] = {
                    "permit_key": key,
                    "permit_number": pnum,
                    "project_id": pid,
                    "stage": "processed",
                    "fingerprint": fp_val,
                    "issue_date": meta["issue_date"],
                    "status_current": meta["status_current"],
                    "primary_trade": site["primary_trade"],
                    "link_verified": meta["link_verified"],
                    "collected_at": now_iso,
                    "processed_at": now_iso,
                    "approved_at": "",
                    "delivered_at": "",
                    "last_seen_at": now_iso,
                    "change_detected": False,
                    "change_history": [],
                }
                promoted += 1
                continue

            cur_stage = existing.get("stage", "collected")
            if existing.get("fingerprint") == fp_val and LIFECYCLE_ORDER.get(cur_stage, 0) >= LIFECYCLE_ORDER["approved"]:
                # Preserve higher human-verified stage ('approved' or 'delivered') when unchanged
                existing["primary_trade"] = site["primary_trade"]
                existing["last_seen_at"] = now_iso
                existing["change_detected"] = False
            else:
                existing["stage"] = "processed"
                existing["fingerprint"] = fp_val
                existing["issue_date"] = meta["issue_date"]
                existing["status_current"] = meta["status_current"]
                existing["primary_trade"] = site["primary_trade"]
                existing["link_verified"] = meta["link_verified"]
                existing["processed_at"] = now_iso
                existing["last_seen_at"] = now_iso
                existing["change_detected"] = False
            promoted += 1

    state["summary_counts"] = get_persistence_summary(state)
    return promoted


def _transition_permit_stage(path: Path, permit_numbers: list[str], target_stage: str) -> int:
    """Move matching permits cleanly to 'approved' or 'delivered' in checkpoint state."""
    targets = {clean_text(p) for p in permit_numbers if clean_text(p)}
    if not targets:
        return 0
    state = load_checkpoint_state(path)
    known = {clean_text(e.get("permit_number")) for e in state.get("permits", {}).values()}
    missing = targets - known
    if missing:
        raise DataQualityError(f"Unknown permit numbers: {', '.join(sorted(missing))}")
    for entry in state.get("permits", {}).values():
        if clean_text(entry.get("permit_number")) in targets:
            if entry.get("stage") == "collected" or str(entry.get("status_current", "")).lower() != "active":
                raise DataQualityError("Only processed, active permits can be approved/delivered")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    updated = 0
    target_rank = LIFECYCLE_ORDER[target_stage]

    for key, entry in state.get("permits", {}).items():
        if not isinstance(entry, dict):
            continue
        pnum = clean_text(entry.get("permit_number"))
        if pnum in targets or key in {f"permit:{t}" for t in targets}:
            cur_rank = LIFECYCLE_ORDER.get(entry.get("stage", "collected"), 0)
            if cur_rank <= target_rank:
                entry["stage"] = target_stage
            entry[f"{target_stage}_at"] = now_iso
            updated += 1

    if updated:
        _write_checkpoint_state(path, state)
    return updated


def mark_permits_approved(path: Path, permit_numbers: list[str]) -> int:
    """Mark specified permit numbers as manually reviewed and approved."""
    return _transition_permit_stage(path, permit_numbers, "approved")


def mark_permits_delivered(path: Path, permit_numbers: list[str]) -> int:
    """Mark specified permit numbers as successfully delivered to buyers."""
    return _transition_permit_stage(path, permit_numbers, "delivered")


def save_checkpoint(
    path: Path,
    as_of_date: date,
    seen_pids: set[str],
    seen_pnums: set[str],
    sites: list[dict],
    checkpoint_state: dict | None = None,
    pagination_truncated: bool = False,
) -> None:
    """Persist per-permit lifecycle state and seen permit identifiers."""
    state = checkpoint_state if checkpoint_state is not None else load_checkpoint_state(path)
    state["last_run_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["last_as_of"] = as_of_date.isoformat()
    state["dataset_complete"] = not pagination_truncated

    merged_pids = set(state.get("seen_project_ids", [])) | set(seen_pids)
    merged_pnums = set(state.get("seen_permit_numbers", [])) | set(seen_pnums)
    state["seen_project_ids"] = sorted(merged_pids)
    state["seen_permit_numbers"] = sorted(merged_pnums)

    record_processed_sites(state, sites)
    _write_checkpoint_state(path, state)


def extract_contact_info(raw: dict) -> tuple[str, str, str, str]:
    """Extract contact provenance and honest fields without fabricating blanks."""
    c_comp = clean_text(raw.get("contractor_company_name"))
    c_name = clean_text(raw.get("contractor_full_name"))
    c_phone = format_phone(raw.get("contractor_phone"))

    a_org = clean_text(raw.get("applicant_org"))
    a_name = clean_text(raw.get("applicant_full_name"))
    a_phone = format_phone(raw.get("applicant_phone"))

    if c_comp:
        return (
            "Contractor Record",
            c_comp,
            c_name or "Not Listed",
            c_phone or "Not Listed",
        )
    if a_org:
        if c_name or c_phone:
            return ("Contractor Record", "Not Listed", c_name or "Not Listed", c_phone or "Not Listed")
        return (
            "Applicant Fallback",
            a_org,
            a_name or "Not Listed",
            a_phone or "Not Listed",
        )
    if c_name or c_phone:
        return (
            "Contractor Record",
            "Not Listed",
            c_name or "Not Listed",
            c_phone or "Not Listed",
        )
    if a_name or a_phone:
        return (
            "Applicant Fallback",
            "Not Listed",
            a_name or "Not Listed",
            a_phone or "Not Listed",
        )
    return ("No Contact Listed", "Not Listed", "Not Listed", "Not Listed")


def normalize_record(
    raw: dict,
    city: str,
    cutoff_date: date,
    seen_project_ids: set[str],
    seen_permit_numbers: set[str],
    ignore_checkpoint: bool,
    stats: RunStats,
    as_of_date: date | None = None,
    checkpoint_state: dict | None = None,
    include_unchanged: bool = False,
) -> dict | None:
    """Validate lower/upper dates, status, permit-level checkpoint, and provenance."""
    issue_dt = parse_iso_date(raw.get("issue_date"))
    if issue_dt is not None:
        iso_issue = issue_dt.isoformat()
        if stats.latest_source_date == "None" or iso_issue > stats.latest_source_date:
            stats.latest_source_date = iso_issue

    status_current = clean_text(raw.get("status_current")) or "Unknown"
    if raw.get("permit_class_mapped", "Commercial") != "Commercial" or raw.get("permittype", "BP") != "BP":
        stats.records_filtered_wrong_scope += 1
        return None
    if (
        issue_dt is None
        or issue_dt < cutoff_date
        or (as_of_date is not None and issue_dt > as_of_date)
        or status_current.lower() != "active"
    ):
        stats.records_filtered_old_or_inactive += 1
        return None

    project_id = clean_id(raw.get("project_id")) or "Not Provided"
    permit_number = clean_text(raw.get("permit_number")) or "Not Provided"
    permit_key = get_permit_key(permit_number, project_id)
    fingerprint = compute_permit_fingerprint(raw)

    change_status = "New Record"
    already_processed_unchanged = False

    if checkpoint_state is not None:
        entry = checkpoint_state.get("permits", {}).get(permit_key)
        if isinstance(entry, dict):
            stage = entry.get("stage", "collected")
            if stage == "collected":
                change_status = "Updated Record" if entry.get("change_detected") else "New Record"
            elif LIFECYCLE_ORDER.get(stage, 0) >= LIFECYCLE_ORDER["processed"]:
                if entry.get("fingerprint") and entry.get("fingerprint") != fingerprint:
                    change_status = "Updated Record"
                else:
                    already_processed_unchanged = True
        elif permit_number != "Not Provided":
            # Backwards-compat for legacy v1 checkpoints without 'permits' dict
            already_processed_unchanged = permit_number in seen_permit_numbers
        elif project_id != "Not Provided":
            already_processed_unchanged = project_id in seen_project_ids
    else:
        if permit_number != "Not Provided":
            already_processed_unchanged = permit_number in seen_permit_numbers
        elif project_id != "Not Provided":
            already_processed_unchanged = project_id in seen_project_ids

    if change_status == "Updated Record":
        stats.records_updated_since_checkpoint += 1

    if already_processed_unchanged and not ignore_checkpoint:
        stats.records_unchanged += 1
        change_status = "Unchanged"
        if not include_unchanged:
            stats.records_skipped_checkpoint += 1
            return None
    elif already_processed_unchanged:
        change_status = "Reprocessed (Requested)"

    desc_clean = clean_description(raw.get("description"))
    work_class = clean_text(raw.get("work_class") or raw.get("permit_class"))
    sqft = parse_sqft(raw)

    if is_low_value_noise(desc_clean, work_class, sqft):
        stats.records_filtered_noise += 1
        return None

    applied_dt = parse_iso_date(raw.get("applieddate"))
    address = clean_text(raw.get("permit_location") or raw.get("original_address1")) or "Address Not Listed"
    city_name = clean_text(raw.get("original_city") or CITY_CONFIGS[city]["label"].split(",")[0]).title()
    zip_code = clean_id(raw.get("original_zip")) or "Not Listed"
    jurisdiction = clean_text(raw.get("jurisdiction")) or "Not Listed"
    masterpermitnum = clean_id(raw.get("masterpermitnum")) or "Not Provided"

    provenance, company, contact, phone = extract_contact_info(raw)
    source_url = validate_source_url(raw, project_id, city)
    link_verified = is_verified_source_url(source_url)
    if link_verified:
        stats.records_valid_links += 1
        link_status = "Official URL / ID Matched (Page Not Checked)"
    elif source_url == "Link Unavailable":
        stats.records_unverified_links += 1
        link_status = "Link Unavailable — Manual Review Required"
    else:
        stats.records_unverified_links += 1
        link_status = "Unverified Link — Flagged for Manual Review"

    return {
        "issue_date": issue_dt.isoformat(),
        "applied_date": applied_dt.isoformat() if applied_dt else "Not Listed",
        "_issue_dt": issue_dt,
        "_applied_dt": applied_dt,
        "timing_status": compute_timing_status(applied_dt, issue_dt),
        "status_current": status_current,
        "permit_number": permit_number,
        "project_id": project_id,
        "masterpermitnum": masterpermitnum,
        "permit_type": clean_text(raw.get("permit_type_desc") or raw.get("permittype")),
        "work_class": work_class,
        "sqft": sqft,
        "reported_remodel_sqft": clean_text(raw.get("remodel_repair_sqft")),
        "reported_new_add_sqft": clean_text(raw.get("total_new_add_sqft")),
        "address": address,
        "city": city_name,
        "zip_code": zip_code,
        "jurisdiction": jurisdiction,
        "contact_provenance": provenance,
        "contractor_company": company,
        "contractor_contact": contact,
        "contractor_phone": phone,
        "contractor_trade": clean_text(raw.get("contractor_trade")) or "Not Listed",
        "applicant_company": clean_text(raw.get("applicant_org")) or "Not Listed",
        "applicant_contact": clean_text(raw.get("applicant_full_name")) or "Not Listed",
        "applicant_phone": format_phone(raw.get("applicant_phone")) or "Not Listed",
        "status_date": clean_text(raw.get("statusdate")),
        "expires_date": clean_text(raw.get("expiresdate")),
        "first_seen_at": (checkpoint_state or {}).get("permits", {}).get(permit_key, {}).get("collected_at", ""),
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "issue_age_days": ((as_of_date or datetime.now(timezone.utc).date()) - issue_dt).days,
        "maps_url": "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(f"{address}, {city_name}, TX {zip_code}"),
        "source_url": source_url,
        "link_verified": link_verified,
        "link_status": link_status,
        "change_status": change_status,
        "_fingerprint": fingerprint,
        "raw_description_clean": desc_clean,
        "_is_new_since_checkpoint": ignore_checkpoint or not already_processed_unchanged,
    }


def _normalize_company_key(text: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
    return "" if norm == "not listed" else norm


def _normalize_base_address(address: str) -> str:
    base = re.sub(
        r"\b(?:bldg|building|unit|ste|suite|apt|fl|floor|rm|room)\b.*$",
        "",
        (address or "").lower(),
    )
    return re.sub(r"[^a-z0-9]+", " ", base).strip()


def _extract_suite_or_unit_designator(record: dict) -> str:
    """Return normalized suite/unit designator so separate suites never merge."""
    addr = record.get("address", "")
    m_addr = re.search(
        r"(?:\b(?:ste|suite|unit|apt|rm|room)\.?\s*#?\s*|#\s*)([a-z0-9-]*\d[a-z0-9-]*|[a-z])\b",
        addr,
        re.IGNORECASE,
    )
    if m_addr:
        return f"suite:{m_addr.group(1).lower()}"

    desc = record.get("raw_description_clean", "")
    m_desc = re.search(
        r"(?:\b(?:ste|suite|apt|rm|room)\s*#?\s*([a-z0-9-]*\d[a-z0-9-]*|[a-z])\b|"
        r"\bunit\s*#?\s*([a-z0-9-]*\d[a-z0-9-]*)\b)",
        desc,
        re.IGNORECASE,
    )
    if m_desc:
        val = m_desc.group(1) or m_desc.group(2)
        if val:
            return f"suite:{val.lower()}"
    return ""


def _extract_building_or_fire_area(record: dict) -> str:
    """Return explicit distinct building/fire-area designator for multi-bldg additivity."""
    addr = record.get("address", "")
    m_bldg = re.search(
        r"\b(?:bldg|building)\s*#?\s*([a-z0-9-]*\d[a-z0-9-]*|[a-z])\b",
        addr,
        re.IGNORECASE,
    )
    if m_bldg:
        return f"bldg:{m_bldg.group(1).lower()}"
    desc = record.get("raw_description_clean", "")
    m_fa = re.search(r"\bfire\s+area\s+([a-z0-9-]+)\b", desc, re.IGNORECASE)
    if m_fa:
        return f"fa:{m_fa.group(1).lower()}"
    return ""


def _dates_within(d1: date | None, d2: date | None, max_days: int) -> bool:
    if d1 is None or d2 is None:
        return False
    return abs((d1 - d2).days) <= max_days


def _can_merge_records(a: dict, b: dict) -> bool:
    """Strict project-identity merge rules: never merge different suites, years, or unverified jobs."""
    if not _dates_within(a["_issue_dt"], b["_issue_dt"], 30):
        return False
    if a["_applied_dt"] is not None and b["_applied_dt"] is not None:
        if not _dates_within(a["_applied_dt"], b["_applied_dt"], 30):
            return False
    elif (a["_applied_dt"] is None) != (b["_applied_dt"] is None):
        return False

    pid_a, pid_b = a["project_id"], b["project_id"]
    pnum_a, pnum_b = a["permit_number"], b["permit_number"]
    suite_a = _extract_suite_or_unit_designator(a)
    suite_b = _extract_suite_or_unit_designator(b)
    has_distinct_suites = bool(suite_a and suite_b and suite_a != suite_b)

    # Case A: Exact duplicate permit record (prefer permit_number when both are present)
    if pnum_a != "Not Provided" and pnum_b != "Not Provided":
        if pnum_a == pnum_b:
            return True
    elif pid_a != "Not Provided" and pid_a == pid_b and not has_distinct_suites:
        return True

    # Never merge records with explicitly different suite/unit designators
    if suite_a != suite_b:
        return False

    comp_a = _normalize_company_key(a["contractor_company"])
    comp_b = _normalize_company_key(b["contractor_company"])
    if not comp_a or comp_a != comp_b:
        return False

    # Case B: Verified shared masterpermitnum with matching contractor, base address, and period
    m_a, m_b = a["masterpermitnum"], b["masterpermitnum"]
    if (m_a not in ("Not Provided", "0", "") and m_a == m_b
            and a["address"] != "Address Not Listed" and b["address"] != "Address Not Listed"
            and a["city"] == b["city"] and a["zip_code"] == b["zip_code"]):
        if _normalize_base_address(a["address"]) == _normalize_base_address(b["address"]):
            return True

    return False


def deduplicate_projects(records: list[dict]) -> list[dict]:
    """Safely group verified related permits while preserving full provenance."""
    n = len(records)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        # Pairwise compatibility prevents an unspecified suite/date from bridging
        # two unrelated jobs through transitive union.
        members_x = [i for i in range(n) if find(i) == rx]
        members_y = [i for i in range(n) if find(i) == ry]
        if rx != ry and all(_can_merge_records(records[i], records[j]) for i in members_x for j in members_y):
            parent[ry] = rx

    for i in range(n):
        for j in range(i + 1, n):
            if _can_merge_records(records[i], records[j]):
                union(i, j)

    clusters: dict[int, list[dict]] = defaultdict(list)
    for i, rec in enumerate(records):
        clusters[find(i)].append(rec)

    consolidated: list[dict] = []
    for rows in clusters.values():
        rows_sorted = sorted(
            rows,
            key=lambda r: (
                1 if r.get("link_verified", True) else 0,
                1 if r["contractor_phone"] != "Not Listed" else 0,
                r["issue_date"],
                r["sqft"],
            ),
            reverse=True,
        )
        lead = dict(rows_sorted[0])

        # Deduplicate exact project_id/permit_number rows before inspecting sqft additivity
        unique_by_pid: list[dict] = []
        seen_pid_pnum: set[tuple[str, str]] = set()
        for r in rows_sorted:
            key = (r["project_id"], r["permit_number"])
            if key not in seen_pid_pnum:
                seen_pid_pnum.add(key)
                unique_by_pid.append(r)

        member_pnums = sorted(
            {r["permit_number"] for r in unique_by_pid if r["permit_number"] != "Not Provided"}
        )
        member_pids = sorted(
            {r["project_id"] for r in unique_by_pid if r["project_id"] != "Not Provided"}
        )

        max_sqft = max(r["sqft"] for r in unique_by_pid)
        if len(unique_by_pid) == 1:
            final_sqft = max_sqft
            sqft_basis = "Single Permit"
        else:
            designators = [_extract_building_or_fire_area(r) for r in unique_by_pid]
            all_distinct_designators = (
                all(d.startswith("bldg:") for d in designators)
                and len(set(designators)) == len(unique_by_pid)
                and all(r["sqft"] > 0 for r in unique_by_pid)
            )
            if all_distinct_designators:
                final_sqft = sum(r["sqft"] for r in unique_by_pid)
                sqft_basis = "Sum of Distinct Buildings/Fire Areas"
            else:
                final_sqft = max_sqft
                sqft_basis = "Max Single Permit (Non-Additive Group)"

        unique_descs: list[str] = []
        for r in unique_by_pid:
            d = r["raw_description_clean"]
            if d and d not in unique_descs:
                unique_descs.append(d)
        merged_desc = " | ".join(unique_descs)

        valid_applied = [r["_applied_dt"] for r in unique_by_pid if r["_applied_dt"] is not None]
        latest_issue_dt = max(r["_issue_dt"] for r in unique_by_pid)
        earliest_applied_dt = min(valid_applied) if valid_applied else None

        lead["issue_date"] = latest_issue_dt.isoformat()
        lead["applied_date"] = earliest_applied_dt.isoformat() if earliest_applied_dt else "Not Listed"
        lead["_issue_dt"] = latest_issue_dt
        lead["_applied_dt"] = earliest_applied_dt
        lead["timing_status"] = compute_timing_status(earliest_applied_dt, latest_issue_dt)
        lead["permit_count_at_site"] = len(unique_by_pid)
        lead["member_permit_numbers"] = "; ".join(member_pnums) if member_pnums else lead["permit_number"]
        lead["member_project_ids"] = "; ".join(member_pids) if member_pids else lead["project_id"]
        lead["member_source_urls"] = "; ".join(dict.fromkeys(r["source_url"] for r in unique_by_pid))
        lead["_member_site_records"] = unique_by_pid
        lead["_member_permit_numbers"] = member_pnums
        lead["_member_project_ids"] = member_pids
        lead["_member_records_meta"] = [
            {
                "permit_number": r["permit_number"],
                "project_id": r["project_id"],
                "fingerprint": r.get("_fingerprint") or compute_permit_fingerprint(r),
                "issue_date": r["issue_date"],
                "status_current": r["status_current"],
                "link_verified": r.get("link_verified", is_verified_source_url(r["source_url"])),
            }
            for r in unique_by_pid
        ]
        lead["sqft"] = final_sqft
        lead["max_single_permit_sqft"] = max_sqft
        lead["sqft_basis"] = sqft_basis
        lead["project_scale"] = compute_project_scale(final_sqft)
        lead["raw_description_clean"] = merged_desc
        lead["_is_new_since_checkpoint"] = any(r["_is_new_since_checkpoint"] for r in unique_by_pid)
        lead["change_status"] = (
            "Updated Record" if any(r["change_status"] == "Updated Record" for r in unique_by_pid)
            else "New Record" if any(r["change_status"] == "New Record" for r in unique_by_pid)
            else "Reprocessed (Requested)" if any(r["change_status"] == "Reprocessed (Requested)" for r in unique_by_pid)
            else "Unchanged"
        )
        lead["first_seen_at"] = min((r["first_seen_at"] for r in unique_by_pid if r["first_seen_at"]), default="")
        consolidated.append(lead)

    return consolidated


def _build_buyer_brief(site: dict, evidence: str, summary: str) -> str:
    """Build factual customer-facing permit brief for suppliers/rental buyers."""
    sqft_part = f"{site['sqft']:,} sq ft ({site['sqft_basis']})" if site["sqft"] > 0 else "SqFt Unlisted"
    work_label = site["work_class"] or site["permit_type"] or "Commercial Permit"
    return (
        f"[{site['status_current']} | {site['timing_status']}] "
        f"{sqft_part} {work_label} at {site['address']} "
        f"(Issued {site['issue_date']}, Applied {site['applied_date']}). "
        f"Summary: {summary}. "
        f"Contact on file: {site['contractor_company']} / {site['contractor_contact']} / "
        f"{site['contractor_phone']} [{site['contact_provenance']}]. "
        f"Evidence: {evidence}."
    )


def _build_optional_gc_opener(site: dict, trade: str) -> str:
    """Build optional, strictly factual GC call opener (empty if unclassified or no contact)."""
    if trade == UNCLASSIFIED_TRADE or site["contractor_company"] == "Not Listed":
        return ""
    contact_first = (
        site["contractor_contact"].split()[0]
        if site["contractor_contact"] != "Not Listed"
        else "team"
    )
    sqft_note = f" ({site['sqft']:,} sq ft)" if site["sqft"] > 0 else ""
    work = site["work_class"] or site["permit_type"]
    scope_map = {
        "Dumpster & Demolition": "roll-off dumpster or debris haul-off quotes",
        "Temp Fencing & Site Services": "temporary fencing, restrooms, or site container quotes",
        "Post-Construction Cleaning": "post-construction final cleaning bids",
        "Commercial Roofing & Exterior": "commercial roofing or exterior material pricing",
    }
    offering = scope_map.get(trade, "subcontractor or material quotes")
    return (
        f"Hi {contact_first}, referencing permit {site['permit_number']} ({work}{sqft_note}) "
        f"at {site['address']} — are you taking {offering} for this project?"
    )


def classify_rule_based(site: dict) -> dict:
    """Evidence-based opportunity fit; no assertion that a vendor is still needed."""
    desc = site["raw_description_clean"]
    work = site["work_class"].lower()
    sqft = site["sqft"]
    affirmative = []
    blocked: set[str] = set()
    negation_rules = {
        "Commercial Roofing & Exterior": ROOF_NEGATION_RE,
        "Dumpster & Demolition": DEMO_NEGATION_RE,
        "Temp Fencing & Site Services": FENCE_NEGATION_RE,
        "Post-Construction Cleaning": CLEAN_NEGATION_RE,
    }
    for clause in CLAUSE_SPLIT_PATTERN.split(desc):
        if not clause.strip():
            continue
        for trade, pattern in negation_rules.items():
            if pattern.search(clause):
                blocked.add(trade)
        # Suffix negations and work belonging to another permit must not become leads.
        if re.search(r"\b(?:not (?:included|required|proposed|in scope)|excluded|by others|"
                     r"under (?:a )?separate permit|to remain|existing to remain)\b", clause, re.I):
            for trade, terms in {
                "Commercial Roofing & Exterior": r"roof|roofing|exterior|facade|siding",
                "Dumpster & Demolition": r"demo|demolition|dumpster|debris",
                "Temp Fencing & Site Services": r"fenc\w*|site work|foundation",
                "Post-Construction Cleaning": r"clean\w*|finish[- ]out",
            }.items():
                if re.search(r"\b(?:" + terms + r")\b", clause, re.I):
                    blocked.add(trade)
            continue
        cleaned = FULL_NEGATION_CLAUSE_RE.sub(" ", clause).strip()
        if cleaned:
            affirmative.append(cleaned)
    text = " . ".join(affirmative).lower()
    evidence: dict[str, str] = {}

    admin = bool(re.search(r"\bno\s+(?:new\s+)?construction\b|\bcertificate of occupancy only\b", desc, re.I))
    has_work = bool(re.search(r"\b(?:install|replace|demolish|demolition|demo|build|construct|"
                              r"reroof|remodel|finish[- ]out|renovat\w*)\b", text))
    if not admin or has_work:
        # A roof location (HVAC/solar) is not roofing scope. Require a work action/material.
        roof = re.search(r"\b(?:re-?roof(?:ing)?|roof(?:ing)? replacement|roof(?:ing)? repairs?|"
                         r"(?:replace|repair|install|new)\b[^.;]{0,55}\b(?:roof(?:ing)?|tpo|epdm|"
                         r"shingles?|siding|cladding|facade|stucco|storefront|waterproofing)|"
                         r"(?:tpo|epdm)\s+(?:roof|membrane))\b", text)
        if roof:
            evidence["Commercial Roofing & Exterior"] = f"Explicit scope: '{roof.group(0)}'"

        demo = re.search(r"\b(?:demolition|demolish(?:ing)?|demo|tear[- ]off|gut(?:ting)?|"
                         r"debris|dumpsters?(?!\s+(?:enclosure|screen|pad))|roll[- ]off|strip[- ]out|"
                         r"remov(?:e|al of|ing)\s+(?:existing\s+)?(?:walls?|partitions?|flooring|concrete|roof(?:ing)?))\b", text)
        demo_work = bool(re.search(r"\b(?:demo|demolition)\b", work)) and "Dumpster & Demolition" not in blocked
        major_build = sqft >= 2500 and (
            bool(re.search(r"\b(?:finish[- ]out|build[- ]out|tenant improvements?|remodel|renovation|addition|new construction|new (?:commercial )?building|demising wall|foundation repair)\b", text)))
        no_dumpster = bool(re.search(r"\b(?:no|without|not require)\b[^.;,]{0,35}\b(?:dumpsters?|roll[- ]off)\b", desc, re.I)
                           or re.search(r"\b(?:dumpsters?|roll[- ]off)\b[^.;,]{0,25}\bnot required\b", desc, re.I))
        if not no_dumpster:
            if demo:
                evidence["Dumpster & Demolition"] = f"Explicit scope: '{demo.group(0)}'; dumpster demand unconfirmed"
            elif demo_work:
                evidence["Dumpster & Demolition"] = f"Explicit work class: '{site['work_class']}'; dumpster demand unconfirmed"
            elif major_build and "Dumpster & Demolition" not in blocked:
                evidence["Dumpster & Demolition"] = f"Inferred: substantial construction ({sqft:,} sq ft); debris service unconfirmed"

        fence = re.search(r"\b(?:temporary fenc\w*|temp fenc\w*|portable restrooms?|portable toilets?|barricades?)\b", text)
        ground = re.search(r"\b(?:ground[- ]up|new construction|shell building|foundation|grading|site work|excavation)\b", text)
        if fence:
            evidence["Temp Fencing & Site Services"] = f"Explicit scope: '{fence.group(0)}'"
        elif "Temp Fencing & Site Services" not in blocked and (ground or work == "demolition"):
            if not re.search(r"\binterior\b", text) or ground:
                evidence["Temp Fencing & Site Services"] = "Inferred: exterior construction/site access; temporary services unconfirmed"

        cleaning = re.search(r"\b(?:final clean(?:ing)?|post[- ]construction clean(?:ing)?|cleanup)\b", text)
        finish = re.search(r"\b(?:finish[- ]out|tenant improvements?|build[- ]out|new finishes|remodel|renovation|new construction|new (?:commercial )?building)\b", text)
        pure_demo = bool(demo or demo_work) and not finish and work not in ("new", "shell", "remodel", "addition and remodel")
        if cleaning:
            evidence["Post-Construction Cleaning"] = f"Explicit scope: '{cleaning.group(0)}'"
        elif "Post-Construction Cleaning" not in blocked and not pure_demo and (
                finish):
            evidence["Post-Construction Cleaning"] = "Inferred: interior/building construction; completion date and cleaning demand unconfirmed"

    applicable = list(evidence)
    if "Commercial Roofing & Exterior" in evidence and work not in ("new", "shell"):
        primary = "Commercial Roofing & Exterior"
    elif evidence.get("Dumpster & Demolition", "").startswith("Explicit"):
        primary = "Dumpster & Demolition"
    elif work in ("new", "shell") and "Temp Fencing & Site Services" in evidence:
        primary = "Temp Fencing & Site Services"
    elif "Post-Construction Cleaning" in evidence:
        primary = "Post-Construction Cleaning"
    else:
        primary = applicable[0] if applicable else UNCLASSIFIED_TRADE
    summary = desc[:240].rstrip(" ,.;:-") + ("..." if len(desc) > 240 else "")
    return {
        "primary_trade": primary,
        "applicable_trades": applicable,
        "trade_evidence": evidence,
        "classification_evidence": "; ".join(f"{t}: {v}" for t, v in evidence.items())
            or "No supported trade scope; manual review required",
        "plain_summary": summary or site["work_class"] or site["permit_type"],
    }


def _build_batch_prompt(batch: list[dict]) -> str:
    """Build structured prompt for optional batched AI classification."""
    allowed_primary = ", ".join(ALL_PRIMARY_TRADES)
    allowed_trades = ", ".join(TRADE_FILES.keys())
    records_payload = [
        {
            "id": i,
            "permit": s["permit_number"],
            "permit_type": s["permit_type"],
            "work_class": s["work_class"],
            "sqft": s["sqft"],
            "description": s["raw_description_clean"][:280],
        }
        for i, s in enumerate(batch)
    ]
    return (
        "Classify each commercial permit strictly from verified scope text.\n"
        "If scope is minor/irrelevant (e.g. shelving, signs, solar) or unclear, return primary_trade='Unclassified / Needs Review' and applicable_trades=[].\n"
        "Respect explicit negations (e.g. 'no roofing work', 'no demolition', 'does not require dumpsters').\n"
        f"Allowed primary_trade: [{allowed_primary}].\n"
        f"Allowed applicable_trades: [{allowed_trades}].\n"
        "Return JSON object {\"results\": [{\"id\": int, \"primary_trade\": str, \"applicable_trades\": list[str], \"classification_evidence\": str, \"plain_summary\": str}]}.\n"
        f"Permits:\n{json.dumps(records_payload)}"
    )


def _call_openai_compat(
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    stats: RunStats,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str | None:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a cautious construction permit classifier. Output valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 1800,
        "response_format": {"type": "json_object"},
    }
    try:
        raw_bytes = fetch_with_retry(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "PermitRadar/3.0",
            },
            stats=stats,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            timeout=30,
            max_retries=1,
            sleep_fn=sleep_fn,
        )
        data = json.loads(raw_bytes.decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        stats.warnings.append(f"AI provider call failed ({url.split('/')[2]}): {type(exc).__name__}")
        return None


def _call_gemini(
    api_key: str,
    prompt: str,
    stats: RunStats,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str | None:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    try:
        raw_bytes = fetch_with_retry(
            url,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key, "User-Agent": "PermitRadar/4.0"},
            stats=stats,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            timeout=30,
            max_retries=1,
            sleep_fn=sleep_fn,
        )
        data = json.loads(raw_bytes.decode("utf-8"))
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as exc:
        stats.warnings.append(f"Gemini AI call failed: {type(exc).__name__}")
        return None


def _parse_batch_response(raw_text: str | None, batch_len: int) -> dict[int, dict]:
    """Validate batched JSON results strictly."""
    if not isinstance(raw_text, str) or not raw_text:
        return {}
    match = re.search(r"\{.*\}", raw_text, flags=re.DOTALL)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict) or not isinstance(parsed.get("results"), list):
        return {}

    valid: dict[int, dict] = {}
    for entry in parsed.get("results", []):
        if not isinstance(entry, dict):
            continue
        idx = entry.get("id")
        if type(idx) is not int or not (0 <= idx < batch_len) or idx in valid:
            continue
        primary = str(entry.get("primary_trade", "")).strip()
        trades = entry.get("applicable_trades")
        if not isinstance(trades, list) or any(not isinstance(t, str) for t in trades):
            continue
        applicable = list(dict.fromkeys(t for t in trades if t in TRADE_FILES))
        evidence = clean_text(entry.get("classification_evidence"))
        summary = clean_text(entry.get("plain_summary"))
        if primary not in ALL_PRIMARY_TRADES or not evidence or not summary:
            continue
        if primary == UNCLASSIFIED_TRADE:
            applicable = []
        elif primary not in applicable:
            continue
        valid[idx] = {
            "primary_trade": primary,
            "applicable_trades": applicable,
            "classification_evidence": evidence,
            "plain_summary": summary,
        }
    return valid


def classify_sites(
    sites: list[dict],
    use_ai: bool,
    stats: RunStats,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """Classify deduplicated sites via batched free AI or deterministic rules."""
    groq_key = os.environ.get("GROQ_API_KEY", "").strip() if use_ai else ""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip() if use_ai else ""
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip() if use_ai else ""

    has_any_ai = bool(groq_key or gemini_key or openrouter_key)
    active_mode = "Rule-Based Clause/Negation Engine ($0 zero-config)"
    if groq_key:
        active_mode = "Deterministic rules + optional Groq review suggestions"
    elif gemini_key:
        active_mode = "Deterministic rules + optional Gemini review suggestions"
    elif openrouter_key:
        active_mode = "Deterministic rules + optional OpenRouter review suggestions"

    batch_size = 10
    for start in range(0, len(sites), batch_size):
        batch = sites[start : start + batch_size]
        ai_map: dict[int, dict] = {}

        if has_any_ai and batch:
            stats.ai_batches_attempted += 1
            prompt = _build_batch_prompt(batch)
            if groq_key:
                ai_map = _parse_batch_response(
                    _call_openai_compat(
                        "https://api.groq.com/openai/v1/chat/completions",
                        groq_key,
                        os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile"),
                        prompt,
                        stats,
                        sleep_fn=sleep_fn,
                    ),
                    len(batch),
                )
            if not ai_map and gemini_key:
                ai_map = _parse_batch_response(
                    _call_gemini(gemini_key, prompt, stats, sleep_fn=sleep_fn),
                    len(batch),
                )
            if not ai_map and openrouter_key:
                ai_map = _parse_batch_response(
                    _call_openai_compat(
                        "https://openrouter.ai/api/v1/chat/completions",
                        openrouter_key,
                        os.environ.get("OPENROUTER_MODEL", "openrouter/free"),
                        prompt,
                        stats,
                        sleep_fn=sleep_fn,
                    ),
                    len(batch),
                )
            if ai_map:
                stats.ai_batches_succeeded += 1
            else:
                stats.ai_batches_failed_fallback += 1
                stats.warnings.append(
                    f"AI batch {start // batch_size + 1} failed or returned invalid JSON; used rule-based fallback."
                )
            sleep_fn(1.0)

        for idx, site in enumerate(batch):
            # AI suggestions are isolated from all buyer facts and trade eligibility.
            res = classify_rule_based(site)
            member_results = [classify_rule_based(r) for r in site.get("_member_site_records", [])]
            if member_results:
                trade_evidence: dict[str, str] = {}
                for member, result in zip(site["_member_site_records"], member_results):
                    for trade, evidence in result["trade_evidence"].items():
                        item = f"{evidence} [permit {member['permit_number']}]"
                        if trade not in trade_evidence or evidence.startswith("Explicit"):
                            trade_evidence[trade] = item
                res["trade_evidence"] = trade_evidence
                res["applicable_trades"] = list(trade_evidence)
                if res["primary_trade"] not in trade_evidence:
                    res["primary_trade"] = next(iter(trade_evidence), UNCLASSIFIED_TRADE)
                res["classification_evidence"] = "; ".join(f"{t}: {v}" for t, v in trade_evidence.items()) or "No supported trade scope; manual review required"
            suggestion = ai_map.get(idx)
            site["ai_review_suggestion"] = (
                "UNVERIFIED AI SUGGESTION: " + json.dumps(suggestion, ensure_ascii=True) if suggestion else ""
            )
            site["_trade_evidence"] = res["trade_evidence"]
            site["trade_evidence"] = json.dumps(res["trade_evidence"], ensure_ascii=True, sort_keys=True)
            site["primary_trade"] = res["primary_trade"]
            site["_applicable_list"] = res["applicable_trades"]
            site["applicable_trades"] = (
                "; ".join(res["applicable_trades"]) if res["applicable_trades"] else "None"
            )
            evidence_text = res["classification_evidence"]
            link_ok = site.get("link_verified", is_verified_source_url(site["source_url"]))
            if link_ok:
                stats.sites_valid_links += 1
            else:
                stats.sites_unverified_links += 1
                evidence_text = f"{evidence_text} [UNVERIFIED/MISSING LINK: Manual Review Required]"

            site["classification_evidence"] = evidence_text
            site["plain_summary"] = res["plain_summary"]
            brief_text = _build_buyer_brief(site, evidence_text, res["plain_summary"])
            if not link_ok:
                brief_text = f"{brief_text} [UNVERIFIED/MISSING LINK: Manual Review Required]"
            site["buyer_brief"] = brief_text
            site["optional_gc_call_opener"] = _build_optional_gc_opener(site, res["primary_trade"])
            assess_lead_quality(site)
            if site["lead_quality"] == "Qualified opportunity":
                stats.qualified_sites += 1

            if site["primary_trade"] == UNCLASSIFIED_TRADE:
                stats.sites_needs_review += 1
            else:
                stats.sites_classified += 1

    return active_mode


def assess_lead_quality(site: dict) -> None:
    """Expose why a record is usable or held for review; this is not sales probability."""
    reasons = []
    members = site.get("_member_site_records", [site])
    if any(not r.get("link_verified", False) for r in members):
        reasons.append("Missing/mismatched official source URL")
    if any(r["address"] == "Address Not Listed" for r in members):
        reasons.append("Site address missing")
    if not re.fullmatch(r"\([2-9]\d{2}\) [2-9]\d{2}-\d{4}", site["contractor_phone"]):
        reasons.append("Usable contact phone missing")
    if site["contact_provenance"] != "Contractor Record":
        reasons.append("Applicant contact only; confirm contractor")
    if "Delayed Issue" in site["timing_status"] or "Date Conflict" in site["timing_status"]:
        reasons.append("Application/issuance timing needs confirmation")
    if site["primary_trade"] == UNCLASSIFIED_TRADE:
        reasons.append("No supported trade scope")
    site["lead_quality"] = "Needs Review" if reasons else "Qualified opportunity"
    site["review_reasons"] = "; ".join(reasons)
    demo_evidence = site.get("_trade_evidence", {}).get("Dumpster & Demolition", "")
    site["dumpster_evidence"] = demo_evidence or "No supported dumpster/debris scope"
    site["dumpster_fit"] = "High" if demo_evidence.startswith("Explicit") else "Medium" if demo_evidence else "Not Matched"


def is_customer_candidate(site: dict) -> bool:
    return site.get("lead_quality") == "Qualified opportunity" and site["status_current"].lower() == "active"


def site_rank_key(site: dict) -> tuple[int, int, str, int, int]:
    """Recency-first, actionability-first ranking."""
    is_actionable = (
        1
        if (
            site["status_current"].lower() == "active"
            and site["primary_trade"] != UNCLASSIFIED_TRADE
            and is_customer_candidate(site)
        )
        else 0
    )
    is_fresh_app = 0 if "Delayed Issue" in site["timing_status"] else 1
    has_phone = 1 if site["contractor_phone"] != "Not Listed" else 0
    return (
        is_actionable,
        is_fresh_app,
        site["issue_date"],
        has_phone,
        site["sqft"],
    )


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    """Write sanitized rows to CSV with UTF-8 BOM for clean Excel opening."""
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_rows = [
        {col: sanitize_csv_value(row.get(col, "")) for col in fieldnames}
        for row in rows
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(safe_rows)


def get_trade_matches(ranked_sites: list[dict], trade_name: str) -> list[dict]:
    """Return classified sites matching trade_name, prioritizing primary_trade and recency."""
    matches = [
        s
        for s in ranked_sites
        if is_customer_candidate(s) and s["primary_trade"] != UNCLASSIFIED_TRADE
        and (trade_name in s["_applicable_list"] or s["primary_trade"] == trade_name)
    ]
    return sorted(
        matches,
        key=lambda s: (1 if s.get("_trade_evidence", {}).get(trade_name, "").startswith("Explicit") else 0, site_rank_key(s)),
        reverse=True,
    )


def export_by_trade(ranked_sites: list[dict], output_dir: Path) -> dict[str, int]:
    """Export 4 dedicated trade CSV files, excluding Unclassified rows."""
    by_trade_dir = output_dir / "by_trade"
    counts: dict[str, int] = {}
    for trade_name, filename in TRADE_FILES.items():
        trade_rows = []
        for site in get_trade_matches(ranked_sites, trade_name):
            row = dict(site)
            row["target_trade"] = trade_name
            row["target_trade_basis"] = site.get("_trade_evidence", {}).get(trade_name, "")
            row["optional_gc_call_opener"] = _build_optional_gc_opener(site, trade_name)
            trade_rows.append(row)
        write_csv(by_trade_dir / filename, TRADE_COLUMNS, trade_rows)
        counts[trade_name] = len(trade_rows)
    return counts


def write_monday_snippets(ranked_sites: list[dict], city_label: str, output_dir: Path) -> Path:
    """Generate factual permit digest snippets for draft review (human-approved only)."""
    lines = [
        f"# Weekly Permit Digest Snippets — {city_label} Commercial Permit Radar",
        "",
        "For manual human review before sending any email. Verify source links before quoting.",
        "",
    ]
    for trade_name, filename in TRADE_FILES.items():
        matches = get_trade_matches(ranked_sites, trade_name)[:3]
        lines.append(f"## {trade_name} (`output/by_trade/{filename}`)")
        lines.append("")
        if not matches:
            lines.append("- *No verified permits matched this trade in the current window.*")
        for idx, s in enumerate(matches, start=1):
            sqft_txt = f"**{s['sqft']:,} sq ft** " if s["sqft"] > 0 else ""
            lines.append(
                f"{idx}. [{s['issue_date']}] {sqft_txt}**{s['work_class']}** — `{s['address']}` "
                f"(Permit `{s['permit_number']}` | Status: {s['status_current']} | {s['timing_status']} | "
                f"Contact [{s['contact_provenance']}]: {s['contractor_company']} — "
                f"{s['contractor_contact']}, `{s['contractor_phone']}` | Link: {s['source_url']})"
            )
        lines.append("")

    snippet_path = output_dir / "monday_email_snippets.md"
    snippet_path.parent.mkdir(parents=True, exist_ok=True)
    snippet_path.write_text("\n".join(lines), encoding="utf-8")
    return snippet_path


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def run_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = path.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise DataQualityError(f"Another run holds {path}. If it crashed, confirm it has stopped before removing this lock.") from exc
    try:
        with handle:
            handle.write(json.dumps({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}))
        yield
    finally:
        path.unlink(missing_ok=True)


@contextmanager
def staging_directory(parent: Path):
    # Ordinary inherited permissions also work with restricted Windows accounts.
    path = parent / f".permit-radar-stage-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path)


def publish_run(staged: Path, output: Path, checkpoint: Path | None, staged_checkpoint: Path | None) -> None:
    """Stage everything first; on caught write errors restore previous files.

    Each file replacement is atomic. The manifest is last and contains hashes,
    so an interrupted process cannot make a mixed generation appear verified.
    Checkpoint is committed only after all report files have been published.
    """
    sources = sorted((staged / "reports").rglob("*"))
    pairs = [(src, output / src.relative_to(staged / "reports")) for src in sources if src.is_file() and src.name != "run_manifest.json"]
    pairs.append((staged / "reports" / "run_manifest.json", output / "run_manifest.json"))
    if checkpoint is not None and staged_checkpoint is not None:
        pairs.append((staged_checkpoint, checkpoint))
    applied = []
    backup_dir = staged / "backups"
    backup_dir.mkdir()
    try:
        for i, (src, dest) in enumerate(pairs):
            dest.parent.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / str(i)
            existed = dest.exists()
            if existed:
                shutil.copy2(dest, backup)
            # Copy beside destination so replace stays on the same filesystem.
            temp_dest = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
            try:
                shutil.copyfile(src, temp_dest)
                os.replace(temp_dest, dest)
            finally:
                temp_dest.unlink(missing_ok=True)
            applied.append((dest, backup if existed else None))
    except Exception:
        for dest, backup in reversed(applied):
            if backup:
                os.replace(backup, dest)
            else:
                dest.unlink(missing_ok=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Austin commercial building permit intelligence. Local reports only.")
    ap.add_argument("--city", default="austin", choices=sorted(CITY_CONFIGS))
    ap.add_argument("--days", type=int, default=14, help="Look back this many days, including both boundary dates")
    ap.add_argument("--as-of", default="", help="Issue-window end YYYY-MM-DD; source status is always current")
    ap.add_argument("--page-size", type=int, default=200)
    ap.add_argument("--max-pages", type=int, default=50)
    ap.add_argument("--fail-on-truncation", action="store_true", help="Compatibility flag: incomplete downloads always fail")
    ap.add_argument("--max-source-age-days", type=int, default=7, help="Fail when source refresh or latest issuance is older than this")
    ap.add_argument("--checkpoint", default=str(DEFAULT_CHECKPOINT_PATH))
    ap.add_argument("--ignore-checkpoint", action="store_true", help="Include all matching sites in the changes feed")
    ap.add_argument("--no-save-checkpoint", action="store_true")
    ap.add_argument("--approve-permits", default="")
    ap.add_argument("--deliver-permits", default="")
    ap.add_argument("--output-dir", default=str(OUTPUT_DIR))
    ai = ap.add_mutually_exclusive_group()
    ai.add_argument("--ai", action="store_true", help="Opt in to optional AI review suggestions; never changes buyer facts")
    ai.add_argument("--no-ai", action="store_true", help="Deterministic mode (already the default)")
    ap.add_argument("--input-json", type=Path, help="Replay a saved raw JSON list offline; output is explicitly marked as a replay")
    return ap.parse_args(argv)


def _run(args: argparse.Namespace, checkpoint_path: Path, output_dir: Path) -> int:
    if args.no_save_checkpoint and (args.approve_permits or args.deliver_permits):
        raise DataQualityError("Stage updates cannot be combined with --no-save-checkpoint")
    if args.approve_permits or args.deliver_permits:
        approved = mark_permits_approved(checkpoint_path, args.approve_permits.split(",")) if args.approve_permits else 0
        delivered = mark_permits_delivered(checkpoint_path, args.deliver_permits.split(",")) if args.deliver_permits else 0
        print(f"Updated stages: approved={approved}, delivered={delivered}")
        return 0
    if not 1 <= args.days <= 3650 or not 1 <= args.page_size <= 50000 or not 1 <= args.max_pages <= 1000 or args.max_source_age_days < 1:
        print("Invalid bounds: days 1..3650, page-size 1..50000, max-pages 1..1000, max-source-age-days >= 1", file=sys.stderr)
        return 2
    try:
        if args.as_of and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.as_of):
            raise ValueError
        as_of = date.fromisoformat(args.as_of) if args.as_of else datetime.now(timezone.utc).date()
        cutoff = as_of - timedelta(days=args.days)
    except (ValueError, OverflowError):
        print("Invalid --as-of: expected YYYY-MM-DD with a valid lookback", file=sys.stderr)
        return 2

    stats = RunStats()
    state = load_checkpoint_state(checkpoint_path)
    # Recover missed scheduled runs by extending the issue window to the last success.
    previous_as_of = parse_iso_date(state.get("last_as_of"))
    if previous_as_of and previous_as_of < cutoff and not args.ignore_checkpoint and not args.input_json:
        cutoff = previous_as_of - timedelta(days=7)
        stats.warnings.append(f"Extended lookback to {cutoff} after a gap in successful runs")
    seen_pids, seen_pnums = load_checkpoint(checkpoint_path)
    city_label = CITY_CONFIGS[args.city]["label"]
    print(f"Fetching {city_label} commercial building permits: {cutoff} through {as_of}")
    if args.input_json:
        raw_records = json.loads(args.input_json.read_text(encoding="utf-8-sig"))
        if not isinstance(raw_records, list) or any(not isinstance(r, dict) for r in raw_records):
            raise DataQualityError("Replay must be a JSON list of raw source records")
        stats.records_fetched = len(raw_records)
        stats.warnings.append("OFFLINE REPLAY: source freshness and pagination were not checked live")
        if not args.no_save_checkpoint:
            raise DataQualityError("Offline replay requires --no-save-checkpoint to protect live delivery state")
    else:
        before = check_source_metadata(args.city, stats)
        updated = datetime.fromtimestamp(before["rowsUpdatedAt"], timezone.utc).date()
        if (datetime.now(timezone.utc).date() - updated).days > args.max_source_age_days:
            raise DataQualityError(f"Source refresh is stale ({updated}); previous reports retained")
        expected_count = check_source_totals(args.city, cutoff, as_of, stats)
        if (as_of - date.fromisoformat(stats.source_latest_issue_date)).days > args.max_source_age_days:
            raise DataQualityError(f"Latest source issuance is stale ({stats.source_latest_issue_date})")
        raw_records = fetch_raw_permits(args.city, cutoff, args.page_size, args.max_pages, stats, as_of_date=as_of)
        if stats.pagination_truncated:
            print("Incomplete download: increase --max-pages. Reports and checkpoint were not replaced.", file=sys.stderr)
            return 3
        after = check_source_metadata(args.city, stats)
        if before["rowsUpdatedAt"] != after["rowsUpdatedAt"]:
            raise DataQualityError("Source changed during pagination; rerun to get a consistent download")
        if len(raw_records) != expected_count:
            raise DataQualityError(f"Source count mismatch: expected {expected_count}, fetched {len(raw_records)}")

    # Bound saved state to the actual query window, including in offline replays.
    in_window = [r for r in raw_records if (dt := parse_iso_date(r.get("issue_date"))) is not None
                 and cutoff <= dt <= as_of and r.get("permit_class_mapped") == "Commercial" and r.get("permittype") == "BP"]
    record_collected_permits(state, in_window, args.city)
    cleaned = []
    for raw in raw_records:
        rec = normalize_record(raw, args.city, cutoff, seen_pids, seen_pnums, args.ignore_checkpoint,
                               stats, as_of_date=as_of, checkpoint_state=state, include_unchanged=True)
        if rec is not None:
            cleaned.append(rec)
    sites = deduplicate_projects(cleaned)
    stats.sites_after_dedup = len(sites)
    mode = classify_sites(sites, use_ai=args.ai, stats=stats)
    ranked = sorted(sites, key=site_rank_key, reverse=True)
    candidates = [s for s in ranked if is_customer_candidate(s)]
    changes = [s for s in ranked if s["_is_new_since_checkpoint"]]
    stats.new_or_updated_sites = len(changes)
    review = [s for s in ranked if not is_customer_candidate(s)]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    with staging_directory(output_dir.parent) as staged:
        reports = staged / "reports"
        write_csv(reports / f"{args.city}_commercial_permits_full.csv", MASTER_COLUMNS, ranked)
        write_csv(reports / f"{args.city}_new_or_updated.csv", MASTER_COLUMNS, changes)
        write_csv(reports / f"{args.city}_needs_review.csv", MASTER_COLUMNS, review)
        write_csv(reports / f"{args.city}_top_15_sample_sheet.csv", MASTER_COLUMNS, candidates[:15])
        write_csv(reports / f"{args.city}_top_20_sample_sheet.csv", MASTER_COLUMNS, candidates[:20])
        trade_counts = export_by_trade(ranked, reports)
        change_trade_counts = export_by_trade(changes, reports / "changes")
        write_monday_snippets([s for s in changes if is_customer_candidate(s)], city_label, reports)
        # Keep original published fields for audits and future reproducible classification.
        atomic_write_text(reports / "source_records.json", json.dumps(raw_records, ensure_ascii=False, indent=2))
        status_rows = [{"permit_number": r.get("permit_number", ""), "project_id": r.get("project_id", ""),
                        "issue_date": r.get("issue_date", ""), "status_current": r.get("status_current", ""),
                        "status_date": r.get("statusdate", ""),
                        "source_url": validate_source_url(r, clean_id(r.get("project_id")), args.city)}
                       for r in in_window if clean_text(r.get("status_current")).lower() != "active"]
        write_csv(reports / f"{args.city}_inactive_in_window.csv",
                  ["permit_number", "project_id", "issue_date", "status_current", "status_date", "source_url"], status_rows)
        hashes = {str(f.relative_to(reports)).replace("\\", "/"): hashlib.sha256(f.read_bytes()).hexdigest()
                  for f in sorted(reports.rglob("*")) if f.is_file()}
        manifest = {
            "run_id": run_id, "pipeline_version": PIPELINE_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_mode": "offline_replay" if args.input_json else "live",
            "source_url": CITY_CONFIGS[args.city]["endpoint"],
            "window_start": cutoff.isoformat(), "window_end": as_of.isoformat(),
            "complete_source_window": not args.input_json,
            "status_as_of": "Current at retrieval; not a historical status snapshot",
            "stats": asdict(stats), "trade_counts": trade_counts, "changes_trade_counts": change_trade_counts,
            "files_sha256": hashes,
            "limits": ["Issued permits do not establish unfilled vendor demand or job start/completion dates",
                       "Contacts are city-published, not independently verified",
                       "Source URLs are checked for official host and matching ID; pages are not visited",
                       "Only issuance dates in the stated window are refreshed; older backfills/changes need a wider run"],
        }
        atomic_write_text(reports / "run_manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        staged_checkpoint = staged / "checkpoint.json"
        if not args.no_save_checkpoint:
            # Never move the recovery watermark backwards during a historical replay.
            watermark = max(as_of, previous_as_of) if previous_as_of else as_of
            save_checkpoint(staged_checkpoint, watermark, seen_pids, seen_pnums, sites, checkpoint_state=state)
        publish_run(staged, output_dir, None if args.no_save_checkpoint else checkpoint_path,
                    None if args.no_save_checkpoint else staged_checkpoint)
    print(f"Completed {run_id}: fetched={stats.records_fetched}, sites={len(ranked)}, qualified={len(candidates)}, needs_review={len(review)}, new_or_updated={len(changes)}")
    print(f"Classifier: {mode}. AI suggestions cannot change customer eligibility.")
    for trade, count in trade_counts.items():
        print(f"  {trade}: {count} current opportunities; {change_trade_counts[trade]} new/updated")
    for warning in stats.warnings:
        print(f"  [WARN] {warning}")
    print(f"Reports: {output_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_dotenv(BASE_DIR / ".env")
    checkpoint = Path(args.checkpoint).resolve()
    output = Path(args.output_dir).resolve()
    try:
        # The output directory is managed by this tool; keep state outside it.
        if checkpoint == output or output in checkpoint.parents:
            raise DataQualityError("Keep --checkpoint outside --output-dir")
        with ExitStack() as locks:
            lock_paths = {output.parent / f".{output.name}.lock", checkpoint.with_suffix(checkpoint.suffix + ".lock")}
            for path in sorted(lock_paths):
                locks.enter_context(run_lock(path))
            return _run(args, checkpoint, output)
    except (DataQualityError, OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Run failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
