"""Raw NADC extracts -> canonical tables.

Reads the newest dated snapshot per year from data/raw/ and writes five tables
to data/processed/. Dates become ISO (YYYY-MM-DD), amounts become plain floats,
columns become snake_case, and every row carries the year and snapshot it came
from so a published number can be traced back to a specific pull.

Five tables, and why:

  contributions.csv   money and value received: Monetary, In-Kind, Earmarked,
                      and Pledge Payment Received.
  loans.csv           receipt-side loan activity, split out because a loan is
                      borrowed money, not support. Folding loans into
                      contributions is the single easiest way to overstate what
                      a candidate raised -- a $50,000 self-loan is not $50,000
                      of support.
  other_receipts.csv  everything else in the contributions extract: interest
                      income, adjustments to cash, anonymous cash, debt
                      forgiveness, and bare Pledges.
  expenditures.csv    EVERY row of the expenditures extract, faithfully.
  independent_expenditures.csv
                      a VIEW of the rows in expenditures.csv whose type is
                      Independent Expenditure. These are a SUBSET, not extra
                      rows -- do not add the two tables together.

Two traps this module exists to defuse:

1. A bare "Pledge" is a promise, not money. It is deliberately NOT a
   contribution; the matching "Pledge Payment Received" is. Counting both
   double-counts the same dollar.

2. The state fans one transaction out over an associated dimension and repeats
   its ID. An independent expenditure against five candidates is five rows
   sharing an Expenditure ID; a contribution to a slate committee is one row
   per candidate that committee supports, sharing a Receipt ID. Sometimes the
   amount is apportioned across those rows, more often the full amount is
   repeated on each -- so summing the amount column blindly over-counts.

   Every row in every table therefore carries `include_in_total`. Sum where it
   is true and the total is right. It matters most where you would least
   expect it: 2022 contributions are $68,865,363 rather than $68,955,221, and
   2026 expenditures $56,147,281 rather than $56,186,294.

Transaction types are classified by an explicit map. Anything unrecognized
lands in other_receipts (or stays a plain expenditure) AND is reported at the
end of the run, because a new type the state invents should show up as a
question, never as a silent omission.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import sys
from datetime import date
from pathlib import Path

from validate import parse_amount, parse_date

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"

# --- Receipt classification -------------------------------------------------
# Values are the ones the extract actually uses, which are NOT the ones the
# layout PDF lists (the PDF says "In-Kind", the data says "In-Kind
# Contribution"). Confirmed against the real 2026 file.

CONTRIBUTION_TYPES = {
    "Monetary",
    "In-Kind Contribution",
    "Earmarked Monetary",
    "Earmarked In-Kind",
    "Pledge Payment Received",
}

LOAN_TYPES = {
    "Loan",
    "Loan Forgiveness Received",
    "Payment Received for Loan Made",
}

# Named so an unrecognized type is distinguishable from a known-but-uncounted
# one when the run reports what it saw.
OTHER_RECEIPT_TYPES = {
    "Other Funds Received (Miscellaneous Receipts)",
    "Debt Forgiveness",
    "Pledge",  # a promise, not money -- see module docstring
    "Refund",
    "Pledge Made Forgiveness",
}

INDEPENDENT_EXPENDITURE_TYPE = "Independent Expenditure"

# Every expenditure type present in the real 2026 file, plus the one the PDF
# lists that 2026 happens not to use. Not a validation gate -- a type outside
# this set is still kept, just reported, because the state adds types (the
# PDF's list was already out of date when we read it).
EXPENDITURE_TYPES_SEEN = {
    "Campaign Expense",
    "Direct contribution",
    "In-Kind Contribution (Exp)",
    "In-Kind Expenditure",
    "Third Party Expenditure",
    "Independent Expenditure",
    "Administrative Expenses",
    "Miscellaneous Expenses",
    "Officeholder Expense specifically allowed by Statute",
    "Debt Payment",
    "Charitable Contributions / Gifts of Acknowledgement (amount limited by Statute)",
    "Disbursement to Federal/Out of State Candidates",
    # The state renamed this between 2023 and 2024; both spellings are live in
    # the corpus, and neither appears in the layout PDF. Exactly the drift the
    # unknown-type report exists to surface.
    "Dissolution Surplus Funds Transfer and Returns",  # 2024+
    "Dissolution Surplus Funds Transfer",  # 2022-2023
    "Agent Expenditure",
    "Loan Payment",
    "Establishment / Administration of the SSPF",
    "Pledge Made",
    "Pledge Payment Made",
    "Non Cash In-Kind Expenditure",
    "Ind. Expend. Contributor Source over $250",
    "Pledge Forgiveness Granted",
    "Earmarked Monetary",
    "Loan Made",
    "Earmarked In-Kind",
    "Refunded Expenditure",
}

# --- Canonical column names -------------------------------------------------

RECEIPT_COLUMNS = {
    "Receipt ID": "receipt_id",
    "Org ID": "org_id",
    "Filer Type": "filer_type",
    "Filer Name": "filer_name",
    "Candidate Name": "candidate_name",
    "Receipt Transaction/Contribution Type": "transaction_type",
    "Other Funds Type": "other_funds_type",
    "Receipt Date": "receipt_date",
    "Receipt Amount": "amount",
    "Description": "description",
    "Contributor or Transaction Source Type": "source_type",
    "Contributor or Source Name (Individual Last Name)": "source_last_name",
    "First Name": "source_first_name",
    "Middle Name": "source_middle_name",
    "Suffix": "source_suffix",
    "Address 1": "address_1",
    "Address 2": "address_2",
    "City": "city",
    "State": "state",
    "Zip": "zip",
    "Filed Date": "filed_date",
    "Amended": "amended",
    "Employer": "employer",
    "Occupation": "occupation",
}

EXPENDITURE_COLUMNS = {
    "Expenditure ID": "expenditure_id",
    "Org ID": "org_id",
    "Filer Type": "filer_type",
    "Filer Name": "filer_name",
    "Candidate Name": "candidate_name",
    "Expenditure Transaction Type": "transaction_type",
    "Expenditure Sub Type": "sub_type",
    "Expenditure Date": "expenditure_date",
    "Expenditure Amount": "amount",
    "Description": "description",
    "Payee or Recipient or In-Kind Contributor Type": "payee_type",
    "Payee or Recipient or In-Kind Contributor Name": "payee_last_name",
    "First Name": "payee_first_name",
    "Middle Name": "payee_middle_name",
    "Suffix": "payee_suffix",
    "Address 1": "address_1",
    "Address 2": "address_2",
    "City": "city",
    "State": "state",
    "Zip": "zip",
    "Filed Date": "filed_date",
    "Support Or Oppose": "support_or_oppose",
    "Candidate Name or Ballot Issue": "target_name",
    "Jurisdiction - Office - District or Ballot Description": "target_jurisdiction",
    "Amended": "amended",
    "Employer": "employer",
    "Occupation": "occupation",
    "Principal Place of Business": "principal_place_of_business",
}

DATE_FIELDS = ("receipt_date", "expenditure_date", "filed_date")

RECEIPT_OUTPUT_COLUMNS = list(RECEIPT_COLUMNS.values()) + [
    "source_name",
    "rows_sharing_id",
    "include_in_total",
    "source_year",
    "source_snapshot",
]

EXPENDITURE_OUTPUT_COLUMNS = list(EXPENDITURE_COLUMNS.values()) + [
    "payee_name",
    "rows_sharing_id",
    "include_in_total",
    "source_year",
    "source_snapshot",
]


def display_name(last: str, first: str, middle: str, suffix: str) -> str:
    """One searchable name.

    The state puts an organization's whole name in the "last name" column and
    leaves the rest blank, so joining the parts handles both people and
    organizations without needing to know which this is.
    """
    parts = [p.strip() for p in (first, middle, last) if p and p.strip()]
    name = " ".join(parts)
    if suffix and suffix.strip():
        name = f"{name} {suffix.strip()}"
    return name.strip()


def latest_snapshot(dataset: str, year: int, raw_dir: Path = None):
    """Newest dated CSV for one dataset-year, or None if never pulled.

    Snapshots are named by ISO run date, so the lexicographic max is newest.
    """
    year_dir = (raw_dir or RAW_DIR) / dataset / str(year)
    if not year_dir.is_dir():
        return None
    snapshots = sorted(year_dir.glob("*.csv"))
    return snapshots[-1] if snapshots else None


def read_snapshot(path: Path):
    # Written by download_extracts.py, which already normalized the encoding.
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _base_row(row: dict, mapping: dict, *, year: int, snapshot: str) -> dict:
    out = {canonical: (row.get(raw) or "").strip() for raw, canonical in mapping.items()}
    for field_name in DATE_FIELDS:
        if field_name in out:
            parsed = parse_date(out[field_name])
            out[field_name] = parsed.isoformat() if parsed else ""
    amount = parse_amount(out.get("amount", ""))
    out["amount"] = amount if amount is not None else ""
    out["source_year"] = year
    out["source_snapshot"] = snapshot
    return out


def flag_include_in_total(rows, id_field: str) -> None:
    """Mark which rows of a shared-ID group may be added up. Mutates in place.

    The state fans one transaction out over an associated dimension and
    repeats the ID: an independent expenditure against five candidates is five
    rows; a contribution to a slate committee is one row per candidate it
    supports. Where those rows repeat a single amount, only the first counts.
    Where they carry different amounts, the state has already apportioned the
    money and every row counts.

    Applied to receipts AND expenditures because both fan out this way -- a
    fact that cost us two failed years to learn.
    """
    by_id = collections.defaultdict(list)
    for row in rows:
        by_id[row[id_field]].append(row)

    for group in by_id.values():
        repeated = len(group) > 1 and len({row["amount"] for row in group}) == 1
        for index, row in enumerate(group):
            row["rows_sharing_id"] = len(group)
            row["include_in_total"] = not (repeated and index > 0)


def normalize_receipts(rows, *, year: int, snapshot: str):
    """Split one year of the contributions/loans extract into three tables."""
    tables = {"contributions": [], "loans": [], "other_receipts": []}
    unknown_types = collections.Counter()

    normalized = []
    for row in rows:
        out = _base_row(row, RECEIPT_COLUMNS, year=year, snapshot=snapshot)
        out["source_name"] = display_name(
            out["source_last_name"],
            out["source_first_name"],
            out["source_middle_name"],
            out["source_suffix"],
        )
        normalized.append(out)

    # Flagged across the whole extract before splitting, so a repeated ID is
    # still detected when its rows would land in different tables.
    flag_include_in_total(normalized, "receipt_id")

    for out in normalized:
        transaction_type = out["transaction_type"]
        if transaction_type in CONTRIBUTION_TYPES:
            tables["contributions"].append(out)
        elif transaction_type in LOAN_TYPES:
            tables["loans"].append(out)
        else:
            if transaction_type not in OTHER_RECEIPT_TYPES:
                unknown_types[transaction_type] += 1
            tables["other_receipts"].append(out)

    return tables, unknown_types


def normalize_expenditures(rows, *, year: int, snapshot: str):
    """Normalize one year of expenditures and flag which rows may be summed.

    An expenditure that targets several candidates arrives as several rows
    sharing an Expenditure ID. Where those rows repeat one amount, only the
    first may be counted; where they carry different amounts, the state has
    already split the spending and every row counts.
    """
    normalized = [
        _base_row(row, EXPENDITURE_COLUMNS, year=year, snapshot=snapshot) for row in rows
    ]
    for out in normalized:
        out["payee_name"] = display_name(
            out["payee_last_name"],
            out["payee_first_name"],
            out["payee_middle_name"],
            out["payee_suffix"],
        )

    flag_include_in_total(normalized, "expenditure_id")

    unknown_types = collections.Counter()
    for out in normalized:
        if out["transaction_type"] not in EXPENDITURE_TYPES_SEEN:
            unknown_types[out["transaction_type"]] += 1

    return normalized, unknown_types


def write_table(rows, path: Path, columns) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def build(years, *, raw_dir: Path = None, out_dir: Path = None) -> dict:
    raw_dir = raw_dir or RAW_DIR
    out_dir = out_dir or PROCESSED_DIR

    receipts = {"contributions": [], "loans": [], "other_receipts": []}
    expenditures = []
    unknown = collections.Counter()
    missing = []

    for year in years:
        snapshot = latest_snapshot("contributions", year, raw_dir)
        if snapshot is None:
            missing.append(("contributions", year))
        else:
            tables, unknown_types = normalize_receipts(
                read_snapshot(snapshot), year=year, snapshot=snapshot.name
            )
            for name, rows in tables.items():
                receipts[name].extend(rows)
            # Qualified by dataset, because the same string can be unknown in
            # one extract and ordinary in the other -- and knowing which file
            # to go read is most of the work of chasing one down.
            unknown.update({f"contributions: {t}": n for t, n in unknown_types.items()})

        snapshot = latest_snapshot("expenditures", year, raw_dir)
        if snapshot is None:
            missing.append(("expenditures", year))
        else:
            rows, unknown_types = normalize_expenditures(
                read_snapshot(snapshot), year=year, snapshot=snapshot.name
            )
            expenditures.extend(rows)
            unknown.update({f"expenditures: {t}": n for t, n in unknown_types.items()})

    for name, rows in receipts.items():
        write_table(rows, out_dir / f"{name}.csv", RECEIPT_OUTPUT_COLUMNS)
    write_table(expenditures, out_dir / "expenditures.csv", EXPENDITURE_OUTPUT_COLUMNS)

    independent = [
        row for row in expenditures if row["transaction_type"] == INDEPENDENT_EXPENDITURE_TYPE
    ]
    write_table(independent, out_dir / "independent_expenditures.csv", EXPENDITURE_OUTPUT_COLUMNS)

    counts = {name: len(rows) for name, rows in receipts.items()}
    counts["expenditures"] = len(expenditures)
    counts["independent_expenditures"] = len(independent)

    summary = {
        "built": date.today().isoformat(),
        "years": list(years),
        "row_counts": counts,
        "contribution_total": round(
            sum(
                r["amount"]
                for r in receipts["contributions"]
                if r["include_in_total"] and r["amount"] != ""
            ),
            2,
        ),
        "expenditure_total": round(
            sum(r["amount"] for r in expenditures if r["include_in_total"] and r["amount"] != ""),
            2,
        ),
        "independent_expenditure_total": round(
            sum(r["amount"] for r in independent if r["include_in_total"] and r["amount"] != ""),
            2,
        ),
        "unknown_transaction_types": dict(unknown),
        "missing_snapshots": [f"{d} {y}" for d, y in missing],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years",
        type=int,
        nargs="+",
        default=list(range(2022, date.today().year + 1)),
        help="years to normalize (default: 2022..current year)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    summary = build(args.years)

    for name, count in summary["row_counts"].items():
        print(f"  {name:30} {count:>10,}")
    print(f"  {'contributions total':30} ${summary['contribution_total']:>13,.2f}")
    print(f"  {'expenditures total':30} ${summary['expenditure_total']:>13,.2f}")
    print(f"  {'independent exp. total':30} ${summary['independent_expenditure_total']:>13,.2f}")

    if summary["missing_snapshots"]:
        print(
            "\nNo snapshot on disk for: "
            + ", ".join(summary["missing_snapshots"])
            + "\nRun scripts/download_extracts.py first.",
            file=sys.stderr,
        )
    if summary["unknown_transaction_types"]:
        print("\nUnrecognized transaction types (kept, but unclassified):", file=sys.stderr)
        for name, count in sorted(
            summary["unknown_transaction_types"].items(), key=lambda kv: -kv[1]
        ):
            print(f"  {count:>8,}  {name!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
