"""Download NADC FirstTuesday bulk CSV extracts (2022+).

Fetches the yearly {year}_ContributionLoanExtract.csv.zip files published at
https://nadc-e.nebraska.gov/PublicSite/DataDownload.aspx, runs them through
validate.py, and archives a dated snapshot per year.

Why dated snapshots, not one file per year: these extracts have no amendment
history of their own (README: "Amendments overwrite; there is no history"),
so every dated pull we keep IS the history the state doesn't preserve.

Usage:
    python scripts/download_extracts.py
    python scripts/download_extracts.py --years 2024 2025 --force
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

from validate import MAX_ROW_COUNT_DROP_FRACTION, SCHEMAS, ValidationError, validate_extract

BASE_URL = "https://nadc-e.nebraska.gov/PublicSite/Docs/BulkDataDownloads"
FIRST_YEAR = 2022  # FirstTuesday cutover; see README "Scope"

DEFAULT_USER_AGENT = (
    "ne-campaign-finance-scraper/0.1 "
    "(https://github.com/diepjustin/diepjustin.github.io; "
    "contact: sdiepxj367@gmail.com)"
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"
META_PATH = DATA_DIR / "scrape_meta.json"

EXTRACT_FILENAMES = {
    "contributions": "{year}_ContributionLoanExtract.csv.zip",
    "expenditures": "{year}_ExpenditureExtract.csv.zip",
}
DATASETS = sorted(EXTRACT_FILENAMES)

# Adding a dataset here but not to validate.py (or vice versa) would mean
# downloading a file nothing knows how to check. Fail at import, not at 3am.
if set(DATASETS) != set(SCHEMAS):
    raise RuntimeError(
        f"dataset names disagree: downloader has {sorted(DATASETS)}, "
        f"validate.py has {sorted(SCHEMAS)}"
    )


def extract_url(year: int, dataset: str = "contributions") -> str:
    return f"{BASE_URL}/{EXTRACT_FILENAMES[dataset].format(year=year)}"


def fetch_zip(url: str, *, user_agent: str, timeout: int = 60) -> bytes:
    resp = requests.get(url, headers={"User-Agent": user_agent}, timeout=timeout)
    resp.raise_for_status()
    return resp.content


def csv_text_from_zip(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if len(csv_names) != 1:
            raise ValidationError(f"expected exactly one CSV in the zip, found {csv_names!r}")
        raw = zf.read(csv_names[0])
    # The export isn't UTF-8 in practice -- confirmed against a real 2026 pull,
    # which has a Windows-1252 smart quote (0x91) an ASP.NET/SQL Server export
    # typically emits. Try UTF-8 first (cheap, and correct if they ever fix
    # it) and fall back rather than crash on real filer-entered text.
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def load_meta() -> dict:
    if META_PATH.exists():
        return json.loads(META_PATH.read_text())
    return {}


def save_meta(meta: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def previous_row_count(meta: dict, dataset: str, year: int):
    runs = meta.get(dataset, {}).get(str(year), [])
    return runs[-1]["row_count"] if runs else None


def download_year(
    year: int,
    dataset: str,
    *,
    user_agent: str,
    force: bool,
    allow_shrink: bool,
    run_date: date,
) -> dict:
    out_dir = RAW_DIR / dataset / str(year)
    out_path = out_dir / f"{run_date.isoformat()}.csv"

    if out_path.exists() and not force:
        print(f"  {dataset} {year}: already pulled today, skipping ({out_path})")
        return {"year": year, "skipped": True}

    url = extract_url(year, dataset)
    print(f"  {dataset} {year}: GET {url}")
    zip_bytes = fetch_zip(url, user_agent=user_agent)
    csv_text = csv_text_from_zip(zip_bytes)

    report = validate_extract(csv_text, dataset=dataset, year=year)

    meta = load_meta()
    prev_count = previous_row_count(meta, dataset, year)
    if (
        prev_count
        and report.row_count < prev_count * (1 - MAX_ROW_COUNT_DROP_FRACTION)
        and not allow_shrink
    ):
        raise ValidationError(
            f"{dataset} {year}: row count dropped from {prev_count} to "
            f"{report.row_count} (>{MAX_ROW_COUNT_DROP_FRACTION:.0%}) -- "
            "pass --allow-shrink if this is expected"
        )

    report.raise_if_failing()
    for warning in report.warnings:
        print(f"    warning: {warning}")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(csv_text)
    sha256 = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()

    meta.setdefault(dataset, {}).setdefault(str(year), []).append(
        {
            "run_date": run_date.isoformat(),
            "row_count": report.row_count,
            "column_count": report.column_count,
            "sha256": sha256,
            "path": str(out_path.relative_to(DATA_DIR)),
        }
    )
    save_meta(meta)

    print(f"    ok: {report.row_count} rows, {report.column_count} columns")
    return {"year": year, "skipped": False, "row_count": report.row_count}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years",
        type=int,
        nargs="+",
        default=list(range(FIRST_YEAR, date.today().year + 1)),
        help=f"years to fetch (default: {FIRST_YEAR}..current year)",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        choices=DATASETS,
        default=DATASETS,
        help="which extracts to fetch (default: both)",
    )
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument(
        "--force", action="store_true", help="re-download even if already pulled today"
    )
    parser.add_argument(
        "--allow-shrink",
        action="store_true",
        help="allow a large row-count drop vs. the previous run without failing",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    run_date = date.today()
    print(f"Fetching {args.datasets} for {args.years} as of {run_date}")
    attempted = 0
    failures = []
    for dataset in args.datasets:
        for year in args.years:
            attempted += 1
            try:
                download_year(
                    year,
                    dataset,
                    user_agent=args.user_agent,
                    force=args.force,
                    allow_shrink=args.allow_shrink,
                    run_date=run_date,
                )
            except (requests.RequestException, ValidationError) as exc:
                print(f"  {dataset} {year}: FAILED -- {exc}", file=sys.stderr)
                failures.append((dataset, year, str(exc)))
    if failures:
        print(f"\n{len(failures)} of {attempted} pulls failed:", file=sys.stderr)
        for dataset, year, msg in failures:
            print(f"  {dataset} {year}: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
