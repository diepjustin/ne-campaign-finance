"""Download and extract the frozen pre-2022 NADC legacy dataset.

Fetches `nebraska.gov/nadc_data/nadc_data.zip` -- a single archive covering
paper-record campaign-finance and financial-disclosure filings before the
2022 FirstTuesday cutover (README.md "Scope": "Out" -- deliberately excluded
from the modern pipeline, in scope for `PLAN.md` Phase 1). Recon (2026-09-11,
see `../ne-connect/docs/DATA_SOURCES.md`) confirmed it is genuinely frozen:
two independent fetches produced the identical sha256.

The zip holds 64 pipe-delimited files. This only extracts the ~13 that Phase
1 actually normalizes -- the contribution and expenditure forms the
Accountability Project's validation set names (B1AB/B2A/B4A/B5/B72/B73,
B1D/B2B/B4B1) plus the C-1/C-2 financial-disclosure forms recon turned up
(C1/C1INC/C1PROP/C2) -- rather than all 64, most of which nothing here plans
to use. `NEEDED_FORMS` is the single place that scope lives; add a name there
if a later phase needs another form.

Usage:
    python scripts/download_legacy.py
    python scripts/download_legacy.py --force
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from datetime import date
from pathlib import Path

import requests

from download_extracts import DEFAULT_USER_AGENT, fetch_zip

LEGACY_URL = "https://nebraska.gov/nadc_data/nadc_data.zip"
ZIP_PREFIX = "nadc_data/"

# Forms Phase 1 normalizes. See this module's docstring for why these and not
# all 64 -- ne-connect/PLAN.md 1.1/1.2/1.5 name each of these explicitly.
CONTRIBUTION_FORMS = ["formb1ab", "formb2a", "formb4a", "formb5", "formb72", "formb73"]
EXPENDITURE_FORMS = ["formb1d", "formb2b", "formb4b1"]
C1_C2_FORMS = ["formc1", "formc1inc", "formc1prop", "formc2"]
NEEDED_FORMS = CONTRIBUTION_FORMS + EXPENDITURE_FORMS + C1_C2_FORMS

# Not form data, but worth keeping alongside every capture: the state's own
# schema doc and its "how current is this" marker.
DOC_MEMBERS = ["nadc_tables.rtf", "DATE_UPDATED.TXT"]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"
META_PATH = DATA_DIR / "scrape_meta.json"


def needed_members() -> list:
    """Full in-zip paths for everything this script extracts."""
    names = [f"{form}.txt" for form in NEEDED_FORMS] + DOC_MEMBERS
    return [f"{ZIP_PREFIX}{name}" for name in names]


def load_meta() -> dict:
    if META_PATH.exists():
        return json.loads(META_PATH.read_text())
    return {}


def save_meta(meta: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def extract_members(zip_bytes: bytes, out_dir: Path) -> dict:
    """Write the needed members to out_dir; return {filename: byte size}."""
    members = needed_members()
    extracted = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        available = set(zf.namelist())
        missing = [m for m in members if m not in available]
        if missing:
            raise ValueError(f"nadc_data.zip is missing expected member(s): {missing!r}")
        out_dir.mkdir(parents=True, exist_ok=True)
        for member in members:
            data = zf.read(member)
            name = member[len(ZIP_PREFIX) :]
            (out_dir / name).write_bytes(data)
            extracted[name] = len(data)
    return extracted


def download_legacy(
    *,
    user_agent: str = DEFAULT_USER_AGENT,
    force: bool = False,
    run_date: date = None,
) -> dict:
    """Fetch the zip, extract the needed forms once, skip on an unchanged sha256.

    The zip is fetched every run -- cheap (~22 MB) and this runs rarely -- and
    its sha256 compared against the last capture. Unchanged content skips
    re-extraction entirely: there's nothing new to write. A changed sha256
    means the state touched a file the whole plan has assumed is frozen; that
    is surfaced as a warning rather than silently accepted, and captured under
    a new dated directory so the previous capture is never overwritten (same
    raw-captures-are-immutable rule as everywhere else in this project).
    """
    run_date = run_date or date.today()
    meta = load_meta()
    previous = meta.get("legacy")

    print(f"GET {LEGACY_URL}")
    zip_bytes = fetch_zip(LEGACY_URL, user_agent=user_agent)
    sha256 = hashlib.sha256(zip_bytes).hexdigest()

    if previous and previous["sha256"] == sha256 and not force:
        print(
            f"  unchanged since {previous['retrieved_at']} "
            f"(sha256 {sha256[:12]}...), skipping extraction"
        )
        return {"skipped": True, "sha256": sha256}

    if previous and previous["sha256"] != sha256:
        print(
            f"  WARNING: nadc_data.zip changed -- it was assumed frozen. "
            f"previous sha256 {previous['sha256'][:12]}..., now {sha256[:12]}...",
            file=sys.stderr,
        )

    out_dir = RAW_DIR / "legacy" / run_date.isoformat()
    extracted = extract_members(zip_bytes, out_dir)

    meta["legacy"] = {
        "source_url": LEGACY_URL,
        "sha256": sha256,
        "retrieved_at": run_date.isoformat(),
        "path": str(out_dir.relative_to(DATA_DIR)),
        "members": extracted,
    }
    save_meta(meta)

    print(f"  ok: {len(extracted)} files extracted to {out_dir}")
    return {"skipped": False, "sha256": sha256, "path": str(out_dir)}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--force", action="store_true", help="re-extract even if the sha256 is unchanged"
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        download_legacy(user_agent=args.user_agent, force=args.force)
    except (requests.RequestException, ValueError) as exc:
        print(f"FAILED -- {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
