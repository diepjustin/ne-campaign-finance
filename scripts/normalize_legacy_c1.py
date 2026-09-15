"""Pre-2022-07-11 C-1/C-2 legacy forms -> financial_interests.csv (era=pre2022).

Reads the four forms download_legacy.py already pulled into
data/raw/legacy/<date>/ that scripts/normalize_legacy.py explicitly leaves
alone ("C-1/C-2 ... is Phase 1.5's job, not this module's" -- its own
docstring):

    formc1.txt      81,804 rows -- filer name/office/address/filing metadata,
                    one row per statement filed.
    formc1inc.txt   10,604 rows -- income sources, business associations,
                    financial institutions, creditors and gifts, one row per
                    disclosed item. `Type of Inocome` (the source file's own
                    spelling) is a single-letter code confirmed against
                    nadc_tables.rtf's FORM10 section: I=Source of Income,
                    B=Business Association, F=Financial Institution,
                    S=Issuers of Stock, C=Creditors, G=Gifts.
    formc1prop.txt  3 rows -- real/other property. Essentially unused; handled
                    for completeness, not because it carries real volume.
    formc2.txt      1,013 rows -- Potential Conflict of Interest statements,
                    a free-text narrative field rather than itemized entries.

Joined on (Candidate ID, Date Received), exactly as nadc_tables.rtf's own
comment on formc1inc/formc1prop instructs ("Use along with Date Received to
link to FORM10" -- FORM10 being formc1's internal table name). formc2 is its
own disclosure (a distinct statement, not an item under a C-1) and gets its
own disclosure_id rather than being joined to a C-1 at all.

PRIVACY (docs/PRIVACY.md rule 2, ne-connect/CLAUDE.md's non-negotiable #1
carried over): `Candidate Address` in formc1.txt is a home street address in
plaintext. It is read only long enough to be dropped -- `strip_to_city_state_zip()`
is the one function that touches it, and nothing downstream of it ever sees
the street value. financial_interests.csv has no address column at all, so
this mostly enforces itself; the function exists anyway so any future column
addition has an obvious, tested place to route through rather than a fresh
chance to leak it.

Output shape matches build_financial_interests.py's financial_interests.csv
exactly (disclosure_id, item_type, counterparty_name_raw, detail, source_url,
retrieved_at, ocr) plus `era="pre2022"` and `source_form`, the same two extra
columns normalize_legacy.py adds to the modern-shaped tables it writes -- so a
caller that unions both eras only ever needs to check one column, matching
Phase 1.4's stated convention.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_legacy import latest_legacy_dir, read_legacy_form  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROCESSED_DIR = DATA_DIR / "processed"

ERA = "pre2022"

# nadc_tables.rtf, FORM10 section, transcribed verbatim 2026-09-15.
INCOME_TYPE_CODES = {
    "I": "income_source",
    "B": "business_association",
    "F": "financial_institution",
    "S": "stock",
    "C": "creditor",
    "G": "gift",
}

FIELDNAMES = ["disclosure_id", "item_type", "counterparty_name_raw", "detail",
              "source_url", "retrieved_at", "ocr", "era", "source_form"]

# No hosted document for a paper filing scanned decades ago -- the frozen zip
# itself, not a per-filing URL, is the primary record. Same "stopgap door"
# reasoning scrape_c1.py uses for Electronic filings with no static PDF.
LEGACY_SOURCE_URL = "https://nebraska.gov/nadc_data/nadc_data.zip"


def strip_to_city_state_zip(row: dict) -> dict:
    """The one function allowed to read `Candidate Address`. Returns a new
    dict with the street address removed and city/state/zip kept -- see
    module docstring's privacy note. Never mutates the input in place, so a
    caller that reuses the raw formc1 row elsewhere can't be surprised by a
    silently-stripped field.
    """
    out = dict(row)
    out.pop("Candidate Address", None)
    return out


def disclosure_id_for_c1(candidate_id: str, date_received: str) -> str:
    return f"legacy:formc1:{candidate_id}:{date_received}"


def disclosure_id_for_c2(candidate_id: str, date_received: str) -> str:
    return f"legacy:formc2:{candidate_id}:{date_received}"


def build_income_rows(inc_rows: list, retrieved_at: str, unknown_codes: dict) -> list:
    rows = []
    for r in inc_rows:
        code = (r.get("Type of Inocome") or "").strip()
        item_type = INCOME_TYPE_CODES.get(code)
        if item_type is None:
            unknown_codes[code] = unknown_codes.get(code, 0) + 1
            item_type = f"unknown:{code!r}"
        name = (r.get("Income Source Name") or "").strip()
        address = (r.get("Income Source Address") or "").strip()
        if not name and not address:
            continue
        rows.append({
            "disclosure_id": disclosure_id_for_c1(r["Candidate ID"], r["Date Received"]),
            "item_type": item_type,
            "counterparty_name_raw": name,
            "detail": address,
            "source_url": LEGACY_SOURCE_URL,
            "retrieved_at": retrieved_at,
            "ocr": False,
            "era": ERA,
            "source_form": "formc1inc",
        })
    return rows


def build_property_rows(prop_rows: list, retrieved_at: str) -> list:
    rows = []
    for r in prop_rows:
        for field, item_type in (("Real Property", "real_property"),
                                  ("Other Property", "other_financial_interest")):
            value = (r.get(field) or "").strip()
            if not value:
                continue
            rows.append({
                "disclosure_id": disclosure_id_for_c1(r["Candidate ID"], r["Date Received"]),
                "item_type": item_type,
                "counterparty_name_raw": value,
                "detail": "",
                "source_url": LEGACY_SOURCE_URL,
                "retrieved_at": retrieved_at,
                "ocr": False,
                "era": ERA,
                "source_form": "formc1prop",
            })
    return rows


def build_c2_rows(c2_rows: list, retrieved_at: str) -> list:
    """Each Form C-2 is one narrative "potential conflict of interest"
    statement -- unlike C-1's itemized schedules there is nothing to segment,
    so the whole statement becomes one row. `Conflict` is the state's own
    prose describing the conflict, reproduced verbatim (CLAUDE.md rule 1)."""
    rows = []
    for r in c2_rows:
        conflict_text = (r.get("Conflict") or "").strip()
        agency = (r.get("Agency") or "").strip()
        if not conflict_text:
            continue
        rows.append({
            "disclosure_id": disclosure_id_for_c2(r["Candidate ID Number"], r["Date Received"]),
            "item_type": "potential_conflict_of_interest",
            "counterparty_name_raw": agency,
            "detail": conflict_text,
            "source_url": LEGACY_SOURCE_URL,
            "retrieved_at": retrieved_at,
            "ocr": False,
            "era": ERA,
            "source_form": "formc2",
        })
    return rows


def build(*, raw_dir: Path = None, out_dir: Path = None) -> dict:
    legacy_dir = latest_legacy_dir(raw_dir)
    out_dir = out_dir or PROCESSED_DIR
    retrieved_at = legacy_dir.name  # the dated capture directory IS the retrieval date

    c1_rows, c1_dupes = read_legacy_form(legacy_dir / "formc1.txt", "formc1")
    inc_rows, inc_dupes = read_legacy_form(legacy_dir / "formc1inc.txt", "formc1inc")
    prop_rows, prop_dupes = read_legacy_form(legacy_dir / "formc1prop.txt", "formc1prop")
    c2_rows, c2_dupes = read_legacy_form(legacy_dir / "formc2.txt", "formc2")

    # formc1 itself carries no schedule items -- only filer identity -- so it
    # is read (and its address stripped) but contributes no rows of its own.
    # Kept here, not skipped, so the privacy strip is exercised and so a
    # future column addition (e.g. filer_name) has a tested row to draw from.
    for row in c1_rows:
        strip_to_city_state_zip(row)

    unknown_codes: dict = {}
    all_rows = []
    all_rows.extend(build_income_rows(inc_rows, retrieved_at, unknown_codes))
    all_rows.extend(build_property_rows(prop_rows, retrieved_at))
    all_rows.extend(build_c2_rows(c2_rows, retrieved_at))

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "financial_interests_legacy.csv"
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    return {
        "rows_written": len(all_rows),
        "c1_filers": len(c1_rows),
        "c1_duplicates": c1_dupes,
        "inc_duplicates": inc_dupes,
        "prop_duplicates": prop_dupes,
        "c2_duplicates": c2_dupes,
        "unknown_income_codes": unknown_codes,
        "out_path": str(out_path),
    }


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    summary = build(raw_dir=args.raw_dir, out_dir=args.out_dir)
    print(f"Wrote {summary['rows_written']} items to {summary['out_path']}")
    print(f"formc1 filers seen: {summary['c1_filers']} "
          f"({summary['c1_duplicates']} duplicate rows dropped)")
    if summary["unknown_income_codes"]:
        print(f"WARNING: unrecognized 'Type of Inocome' codes: "
              f"{summary['unknown_income_codes']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
