"""Pre-2022 NADC legacy forms -> contributions_legacy.csv, loans_legacy.csv,
other_receipts_legacy.csv, expenditures_legacy.csv.

Reads the 9 contribution/expenditure forms download_legacy.py already pulled
(CONTRIBUTION_FORMS + EXPENDITURE_FORMS) into data/raw/legacy/<date>/ and maps
each into the same canonical columns normalize.py uses for the modern extract
(normalize.RECEIPT_OUTPUT_COLUMNS / EXPENDITURE_OUTPUT_COLUMNS), plus two
columns every legacy row carries that no modern row needs:

  era          always "pre2022" here -- lets a consumer that unions both
               eras (Phase 1.4) tell them apart without inspecting dates.
  source_form  which of the 9 raw forms this row came from (e.g. "formb73")
               -- several forms feed more than one output table (see below),
               so this is the only way to trace a row back to its filing.

NEVER writes into contributions.csv, expenditures.csv, or loans.csv -- those
are the modern tables and this module only ever touches the four *_legacy.csv
files above. C-1/C-2 (formc1/formc1inc/formc1prop/formc2) are Phase 1.5's
job, not this module's; this module only reads CONTRIBUTION_FORMS and
EXPENDITURE_FORMS from download_legacy.py.

Three real classification decisions, made 2026-09-14 by the project owner
after `validate_legacy.py` and manual review of nadc_tables.rtf surfaced them
(see PLAN.md 1.2's recon notes for the counts and reasoning) -- NOT default
choices this module invented on its own:

1. formb73's Nature Of Contribution == "E" (Independent Expenditure, 635 of
   7,328 rows) is the FILER's own spending, not money it received. These rows
   go to expenditures_legacy.csv, never contributions_legacy.csv.
2. formb5's Nature of Contribution == "L" (Loan, 110 of 4,712 rows) is
   borrowed money, not a contribution -- same rule the modern pipeline
   already applies. These rows go to a new loans_legacy.csv, matching
   normalize.py's own loans.csv treatment.
3. formb4b1's Nature of Expenditure has 346 "A" and 51 "B" rows -- codes not
   in the documented set (D/I/L/E). Kept, not dropped or guessed at: the row
   lands in expenditures_legacy.csv with the raw code as transaction_type,
   and the count is surfaced in the summary the same way normalize.py already
   surfaces an unrecognized modern transaction type.

One more finding acted on here without needing a judgment call, because it is
a straightforward data-quality fix rather than a financial classification:
sentinel dates (12/31/9999, 01/01/0001, 01/01/0900 all seen in the raw data)
parse to real-but-absurd date objects under validate.py's parse_date, which
would otherwise silently ship a fake receipt date. _legacy_date() rejects
anything outside a sane range and reports how many it dropped, the same way
an unrecognized transaction type is reported rather than silently miscounted.
"""

from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from download_legacy import CONTRIBUTION_FORMS, EXPENDITURE_FORMS  # noqa: E402
from normalize import (  # noqa: E402
    EXPENDITURE_OUTPUT_COLUMNS,
    RECEIPT_OUTPUT_COLUMNS,
    display_name,
    write_table,
)
from validate import parse_amount, parse_date  # noqa: E402
from validate_legacy import HEADERS, validate_legacy_form  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RAW_DIR = DATA_DIR / "raw" / "legacy"
PROCESSED_DIR = DATA_DIR / "processed"
META_PATH = DATA_DIR / "scrape_meta.json"

DELIMITER = "|"
ERA = "pre2022"

# Confirmed against a real pull (see validate_legacy.py's docstring): every
# row in these six forms is missing "Report ID" specifically, not merely
# "one trailing field" -- it is the second-to-last column, not the last. The
# concrete example there (formb2a) has the real value that belongs to
# "Contributor Name" landing in the last raw position while "Report ID" gets
# no slot in the row at all, which only lines up if "Report ID" is dropped
# from the header before zipping, not padded at the end. Verified by hand
# against real extracted lines from all six forms while writing this module.
FORMS_MISSING_REPORT_ID = {
    "formb2a", "formb4a", "formb72", "formb73", "formb2b", "formb4b1",
}

LEGACY_RECEIPT_COLUMNS = RECEIPT_OUTPUT_COLUMNS + ["era", "source_form"]
LEGACY_EXPENDITURE_COLUMNS = EXPENDITURE_OUTPUT_COLUMNS + ["era", "source_form"]

# A sane year range for a receipt/expenditure/filing date. Outside this, a
# parsed date is almost certainly one of the sentinel values NADC's paper
# system used for "no date entered" (12/31/9999, 01/01/0001, 01/01/0900 all
# seen) rather than a real date -- see module docstring.
_MIN_YEAR = 1985  # Nebraska's campaign finance system predates this era
_MAX_YEAR = date.today().year + 1


def _legacy_date(raw: str, counter: collections.Counter) -> str:
    parsed = parse_date(raw)
    if parsed is None:
        return ""
    if not (_MIN_YEAR <= parsed.year <= _MAX_YEAR):
        counter["sentinel_dates_dropped"] += 1
        return ""
    return parsed.isoformat()


def _legacy_amount(raw: str):
    value = parse_amount(raw)
    return value if value is not None else ""


def _effective_header(form: str) -> list:
    header = HEADERS[form]
    if form in FORMS_MISSING_REPORT_ID:
        return [h for h in header if h != "Report ID"]
    return header


def read_legacy_form(path: Path, form: str):
    """Parse one legacy form, gated by validate_legacy_form(), deduplicating
    exact-repeat rows (paper records re-entered more than once -- see
    validate_legacy.py's docstring; these are counted and dropped here, not
    treated as a data-quality failure).

    Returns (rows, duplicate_count) where each row is a dict keyed by the
    form's real column names (Report ID always "" for the six forms that
    never populate it).
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    validate_legacy_form(text, form=form).raise_if_failing()

    header = _effective_header(form)
    reader = csv.reader(io.StringIO(text), delimiter=DELIMITER)
    all_rows = list(reader)
    data_rows = all_rows[1:]

    seen = set()
    duplicates = 0
    parsed = []
    for raw_row in data_rows:
        fingerprint = tuple(raw_row)
        if fingerprint in seen:
            duplicates += 1
            continue
        seen.add(fingerprint)
        # Defensive only -- validate_legacy_form()'s gate plus the header
        # adjustment above should make every row exactly the right length.
        # Never silently misalign columns if a row somehow isn't.
        if len(raw_row) != len(header):
            raw_row = (raw_row + [""] * len(header))[: len(header)]
        row = dict(zip(header, raw_row))
        if form in FORMS_MISSING_REPORT_ID:
            row["Report ID"] = ""
        parsed.append(row)
    return parsed, duplicates


def _new_row(columns: list, *, source_form: str, year, snapshot: str) -> dict:
    row = {col: "" for col in columns}
    row["era"] = ERA
    row["source_form"] = source_form
    row["source_year"] = year
    row["source_snapshot"] = snapshot
    row["include_in_total"] = True
    row["rows_sharing_id"] = 1
    return row


def _receipt_row(*, source_form, year, snapshot, receipt_id, org_id, filer_name,
                  transaction_type, receipt_date, filed_date, amount, description,
                  source_name, source_type, address_1="", city="", state="", zip_="",
                  employer="", occupation="") -> dict:
    row = _new_row(LEGACY_RECEIPT_COLUMNS, source_form=source_form, year=year, snapshot=snapshot)
    row.update(
        receipt_id=receipt_id, org_id=org_id, filer_name=filer_name,
        transaction_type=transaction_type, receipt_date=receipt_date, filed_date=filed_date,
        amount=amount, description=description, source_name=source_name,
        source_type=source_type, address_1=address_1, city=city, state=state, zip=zip_,
        employer=employer, occupation=occupation,
    )
    return row


def _expenditure_row(*, source_form, year, snapshot, expenditure_id, org_id, filer_name,
                      transaction_type, expenditure_date, filed_date, amount, description,
                      payee_name="", target_name="", support_or_oppose="", sub_type="") -> dict:
    row = _new_row(
        LEGACY_EXPENDITURE_COLUMNS, source_form=source_form, year=year, snapshot=snapshot
    )
    row.update(
        expenditure_id=expenditure_id, org_id=org_id, filer_name=filer_name,
        transaction_type=transaction_type, expenditure_date=expenditure_date,
        filed_date=filed_date, amount=amount, description=description, payee_name=payee_name,
        target_name=target_name, support_or_oppose=support_or_oppose, sub_type=sub_type,
    )
    return row


# --- Per-form normalizers ----------------------------------------------------
# Each returns a list of (table, row) pairs -- table in
# {"contributions", "loans", "other_receipts", "expenditures"} -- since
# several of these forms fan one raw row out into more than one canonical
# row (a legacy row can report cash AND in-kind AND a pledge amount all on
# one line), or route to different tables depending on a nature/type code.


def _normalize_formb1ab(rows, *, year, snapshot, unknown):
    """Committee-received contributions with full contributor detail. Cash,
    In-Kind and Unpaid Pledges are three separate amount columns on one row
    -- up to three canonical rows come out of one raw row (PLAN.md 1.2
    finding #3). A bare pledge is a promise, not money, same rule the modern
    pipeline applies to a Pledge transaction type."""
    out = []
    for i, r in enumerate(rows):
        org_name = r["Contributor Organization Name"].strip()
        if org_name:
            source_name, source_type = org_name, "Organization"
        else:
            source_name = display_name(
                r["Contributor Last Name"], r["Contributor First Name"],
                r["Contributor Middle Initial"], "",
            )
            source_type = "Individual" if source_name else ""

        base = dict(
            source_form="formb1ab", year=year, snapshot=snapshot,
            org_id=f"legacy:{r['Committee ID']}", filer_name=r["Committee Name"],
            receipt_date=_legacy_date(r["Contribution Date"], unknown),
            filed_date=_legacy_date(r["Date Received"], unknown),
            source_name=source_name, source_type=source_type,
            address_1=r["Contributor Address"], city=r["Contributor City"],
            state=r["Contributor State"], zip_=r["Contributor Zipcode"],
        )
        cash = _legacy_amount(r["Cash Contribution"])
        if cash:
            out.append(("contributions", _receipt_row(
                **base, receipt_id=f"legacy:formb1ab:{i}:cash",
                transaction_type="Monetary", amount=cash, description="",
            )))
        inkind = _legacy_amount(r["In-Kind Contribution"])
        if inkind:
            out.append(("contributions", _receipt_row(
                **base, receipt_id=f"legacy:formb1ab:{i}:inkind",
                transaction_type="In-Kind Contribution", amount=inkind, description="",
            )))
        pledge = _legacy_amount(r["Unpaid Pledges"])
        if pledge:
            out.append(("other_receipts", _receipt_row(
                **base, receipt_id=f"legacy:formb1ab:{i}:pledge",
                transaction_type="Pledge", amount=pledge, description="",
            )))
    return out


def _normalize_cash_inkind_pledge_minimal(rows, *, form, year, snapshot, unknown):
    """formb2a and formb4a: same three-amount-column fan-out as formb1ab, but
    with no contributor detail beyond one name field and no address at all
    -- these forms simply don't carry it. Committee Name isn't on this form
    either, so filer_name is genuinely blank rather than guessed."""
    out = []
    for i, r in enumerate(rows):
        base = dict(
            source_form=form, year=year, snapshot=snapshot,
            org_id=f"legacy:{r['Committee ID']}", filer_name="",
            receipt_date=_legacy_date(r["Contribution Date"], unknown),
            filed_date=_legacy_date(r["Date Received"], unknown),
            source_name=r["Contributor Name"].strip(), source_type="",
        )
        cash = _legacy_amount(r["Cash Contribution"])
        if cash:
            out.append(("contributions", _receipt_row(
                **base, receipt_id=f"legacy:{form}:{i}:cash",
                transaction_type="Monetary", amount=cash, description="",
            )))
        inkind = _legacy_amount(r["In-Kind Contribution"])
        if inkind:
            out.append(("contributions", _receipt_row(
                **base, receipt_id=f"legacy:{form}:{i}:inkind",
                transaction_type="In-Kind Contribution", amount=inkind, description="",
            )))
        pledge = _legacy_amount(r["Unpaid Pledges"])
        if pledge:
            out.append(("other_receipts", _receipt_row(
                **base, receipt_id=f"legacy:{form}:{i}:pledge",
                transaction_type="Pledge", amount=pledge, description="",
            )))
    return out


# formb5's Nature of Contribution code -> (table, transaction_type). Real
# distribution confirmed in PLAN.md 1.2 recon: M 4266, I 247, L 110, blank 65,
# P 24. "L" (Loan) routes to loans_legacy.csv per the 2026-09-14 decision.
FORMB5_NATURE = {
    "M": ("contributions", "Monetary"),
    "I": ("contributions", "In-Kind Contribution"),
    "L": ("loans", "Loan"),
    "P": ("other_receipts", "Pledge"),
}


def _normalize_formb5(rows, *, year, snapshot, unknown):
    out = []
    for i, r in enumerate(rows):
        nature = r["Nature of Contribution"].strip()
        table, transaction_type = FORMB5_NATURE.get(nature, (None, None))
        if table is None:
            unknown[f"formb5 Nature of Contribution: {nature!r}"] += 1
            table, transaction_type = "other_receipts", nature or "(blank)"
        amount = _legacy_amount(r["Amount"])
        if not amount:
            continue
        out.append((table, _receipt_row(
            source_form="formb5", year=year, snapshot=snapshot,
            receipt_id=f"legacy:formb5:{i}",
            org_id=f"legacy:{r['Committee ID']}", filer_name=r["Committee Name"],
            transaction_type=transaction_type,
            receipt_date=_legacy_date(r["Date of Contribution"], unknown),
            filed_date=_legacy_date(r["Date Received"], unknown),
            amount=amount, description="",
            source_name=r["Contributor Name"].strip(), source_type="",
            employer=r["Employer"], occupation=r["Occupation"],
        )))
    return out


def _normalize_formb72(rows, *, year, snapshot, unknown):
    """Direct contributions, amount only, no nature field -- confirmed clean
    in PLAN.md 1.2 recon (every row is genuinely a contribution)."""
    out = []
    for i, r in enumerate(rows):
        amount = _legacy_amount(r["Amount"])
        if not amount:
            continue
        out.append(("contributions", _receipt_row(
            source_form="formb72", year=year, snapshot=snapshot,
            receipt_id=f"legacy:formb72:{i}",
            org_id=f"legacy:{r['Committee ID']}", filer_name=r["Committee Name"],
            transaction_type="Monetary",
            receipt_date=_legacy_date(r["Contribution Date"], unknown),
            filed_date=_legacy_date(r["Date Received"], unknown),
            amount=amount, description="",
            source_name=r["Contributor Name"].strip(), source_type="",
        )))
    return out


def _normalize_formb73(rows, *, year, snapshot, unknown):
    """Mixes contributions and the filer's OWN spending on one table.
    Nature Of Contribution 'E' (Independent Expenditure, PLAN.md 1.2 finding
    #1) is the filer's own money going OUT, not money it received -- those
    rows go to expenditures_legacy.csv. For an 'E' row, "Contributor Name" is
    actually the candidate/ballot issue the filer targeted (the Support/
    Oppose and Description fields, which are otherwise meaningless for a
    contribution row, confirm this), so it maps to target_name there, not
    source_name."""
    out = []
    for i, r in enumerate(rows):
        nature = r["Nature Of Contribution"].strip()
        amount = _legacy_amount(r["Amount"])
        if not amount:
            continue
        receipt_date = _legacy_date(r["Contribution Date"], unknown)
        filed_date = _legacy_date(r["Date Received"], unknown)
        if nature == "E":
            out.append(("expenditures", _expenditure_row(
                source_form="formb73", year=year, snapshot=snapshot,
                expenditure_id=f"legacy:formb73:{i}",
                org_id=f"legacy:{r['Committee ID']}", filer_name=r["Committee Name"],
                transaction_type="Independent Expenditure",
                expenditure_date=receipt_date, filed_date=filed_date, amount=amount,
                description=r["Description"].strip(),
                target_name=r["Contributor Name"].strip(),
                support_or_oppose=r["Support/Oppose"].strip(),
            )))
            continue
        table, transaction_type = {
            "I": ("contributions", "In-Kind Contribution"),
            "P": ("other_receipts", "Pledge"),
        }.get(nature, (None, None))
        if table is None:
            unknown[f"formb73 Nature Of Contribution: {nature!r}"] += 1
            table, transaction_type = "other_receipts", nature or "(blank)"
        out.append((table, _receipt_row(
            source_form="formb73", year=year, snapshot=snapshot,
            receipt_id=f"legacy:formb73:{i}",
            org_id=f"legacy:{r['Committee ID']}", filer_name=r["Committee Name"],
            transaction_type=transaction_type, receipt_date=receipt_date,
            filed_date=filed_date, amount=amount, description=r["Description"].strip(),
            source_name=r["Contributor Name"].strip(), source_type="",
        )))
    return out


def _normalize_formb1d(rows, *, year, snapshot, unknown):
    """Plain expenditures, no nature field beyond a Cash/In-Kind flag --
    confirmed clean in PLAN.md 1.2 recon. Payee Address is one free-text
    field ("RT 1 BOX 266, PALMYRA NE 68418"), not separate city/state/zip
    columns like the modern extract -- reliably splitting a street address
    out of that string is not attempted here, and a payee is sometimes a
    private individual (e.g. a candidate's own vendor payment), so the whole
    field is DROPPED rather than risk shipping an unparsed home address.
    docs/PRIVACY.md's address rule is applied conservatively: omit, don't
    guess."""
    out = []
    for i, r in enumerate(rows):
        amount = _legacy_amount(r["Amount"])
        if not amount:
            continue
        in_kind = _legacy_amount(r["In-Kind"])
        transaction_type = "In-Kind Expenditure" if in_kind else "Campaign Expense"
        out.append(("expenditures", _expenditure_row(
            source_form="formb1d", year=year, snapshot=snapshot,
            expenditure_id=f"legacy:formb1d:{i}",
            org_id=f"legacy:{r['Committee ID']}", filer_name=r["Committee Name"],
            transaction_type=transaction_type,
            expenditure_date=_legacy_date(r["Expenditure Date"], unknown),
            filed_date=_legacy_date(r["Date Received"], unknown),
            amount=amount, description=r["Expenditure Purpose"].strip(),
            payee_name=r["Payee Name"].strip(),
        )))
    return out


# formb2b's Nature of Expenditure code -> transaction_type. Confirmed clean
# in PLAN.md 1.2 recon: this is the committee's OWN Form B-2 expenditure
# schedule, so every nature here is genuinely an expenditure (unlike
# formb73's corporate-filer ambiguity).
FORMB2B_NATURE = {
    "D": "Direct contribution",
    "K": "In-Kind Expenditure",
    "I": "Independent Expenditure",
}


def _normalize_formb2b(rows, *, year, snapshot, unknown):
    out = []
    for i, r in enumerate(rows):
        amount = _legacy_amount(r["Amount"])
        if not amount:
            continue
        nature = r["Nature of Expenditure"].strip()
        transaction_type = FORMB2B_NATURE.get(nature)
        if transaction_type is None:
            unknown[f"formb2b Nature of Expenditure: {nature!r}"] += 1
            transaction_type = nature or "(blank)"
        out.append(("expenditures", _expenditure_row(
            source_form="formb2b", year=year, snapshot=snapshot,
            expenditure_id=f"legacy:formb2b:{i}",
            org_id=f"legacy:{r['Committee ID']}", filer_name="",
            transaction_type=transaction_type,
            expenditure_date=_legacy_date(r["Expenditure Date"], unknown),
            filed_date=_legacy_date(r["Date Received"], unknown),
            amount=amount, description=r["Description"].strip(),
            target_name=r["Committee Name Expenditure is For"].strip(),
            support_or_oppose=r["Support/Oppose"].strip(),
            sub_type=r["Line ID"].strip(),
        )))
    return out


# formb4b1's Nature of Expenditure -> transaction_type, for the three
# documented codes plus "L" (only 2 rows; this form is expenditure-shaped, so
# an "L" here reads as a loan PAYMENT -- money going out to pay down a debt,
# which normalize.py's own EXPENDITURE_TYPES_SEEN already treats as a genuine
# expenditure type -- not a loan RECEIVED, which would be receipt-side).
# "A" (346 rows) and "B" (51 rows) are NOT in nadc_tables.rtf's documented
# set at all -- per the 2026-09-14 decision, kept and flagged, never dropped
# or guessed at.
FORMB4B1_NATURE = {
    "D": "Direct contribution",
    "I": "In-Kind Expenditure",
    "E": "Independent Expenditure",
    "L": "Loan Payment",
}


def _normalize_formb4b1(rows, *, year, snapshot, unknown):
    out = []
    for i, r in enumerate(rows):
        amount = _legacy_amount(r["Amount"])
        if not amount:
            continue
        nature = r["Nature of Expenditure"].strip()
        transaction_type = FORMB4B1_NATURE.get(nature)
        if transaction_type is None:
            unknown[f"formb4b1 Nature of Expenditure: {nature!r}"] += 1
            transaction_type = nature or "(blank)"
        out.append(("expenditures", _expenditure_row(
            source_form="formb4b1", year=year, snapshot=snapshot,
            expenditure_id=f"legacy:formb4b1:{i}",
            org_id=f"legacy:{r['Committee ID']}", filer_name="",
            transaction_type=transaction_type,
            expenditure_date=_legacy_date(r["Expenditure Date"], unknown),
            filed_date=_legacy_date(r["Date Received"], unknown),
            amount=amount, description="",
            target_name=r["Expenditure Committee Name"].strip(),
            support_or_oppose=r["Support/Oppose"].strip(),
            sub_type=r["Expense Category"].strip(),
        )))
    return out


NORMALIZERS = {
    "formb1ab": _normalize_formb1ab,
    "formb2a": lambda rows, **kw: _normalize_cash_inkind_pledge_minimal(rows, form="formb2a", **kw),
    "formb4a": lambda rows, **kw: _normalize_cash_inkind_pledge_minimal(rows, form="formb4a", **kw),
    "formb5": _normalize_formb5,
    "formb72": _normalize_formb72,
    "formb73": _normalize_formb73,
    "formb1d": _normalize_formb1d,
    "formb2b": _normalize_formb2b,
    "formb4b1": _normalize_formb4b1,
}

assert set(NORMALIZERS) == set(CONTRIBUTION_FORMS + EXPENDITURE_FORMS), (
    "NORMALIZERS must cover exactly the forms download_legacy.py fetches for "
    "contributions/expenditures -- add or remove a normalizer to match"
)


def latest_legacy_dir(raw_dir: Path = None) -> Path:
    raw_dir = raw_dir or RAW_DIR
    dated = sorted(p for p in raw_dir.glob("*") if p.is_dir())
    if not dated:
        raise FileNotFoundError(
            f"no dated pull under {raw_dir} -- run scripts/download_legacy.py first"
        )
    return dated[-1]


def build(*, raw_dir: Path = None, out_dir: Path = None) -> dict:
    legacy_dir = latest_legacy_dir(raw_dir)
    out_dir = out_dir or PROCESSED_DIR

    tables = {"contributions": [], "loans": [], "other_receipts": [], "expenditures": []}
    unknown = collections.Counter()
    duplicates_dropped = collections.Counter()
    year = int(legacy_dir.name[:4])
    snapshot = f"legacy:{legacy_dir.name}"

    for form in CONTRIBUTION_FORMS + EXPENDITURE_FORMS:
        path = legacy_dir / f"{form}.txt"
        rows, duplicates = read_legacy_form(path, form)
        duplicates_dropped[form] = duplicates
        for table, row in NORMALIZERS[form](rows, year=year, snapshot=snapshot, unknown=unknown):
            tables[table].append(row)

    write_table(tables["contributions"], out_dir / "contributions_legacy.csv", LEGACY_RECEIPT_COLUMNS)
    write_table(tables["loans"], out_dir / "loans_legacy.csv", LEGACY_RECEIPT_COLUMNS)
    write_table(tables["other_receipts"], out_dir / "other_receipts_legacy.csv", LEGACY_RECEIPT_COLUMNS)
    write_table(tables["expenditures"], out_dir / "expenditures_legacy.csv", LEGACY_EXPENDITURE_COLUMNS)

    counts = {name: len(rows) for name, rows in tables.items()}
    summary = {
        "built": date.today().isoformat(),
        "source_snapshot": legacy_dir.name,
        "row_counts": counts,
        "contribution_total": round(
            sum(r["amount"] for r in tables["contributions"] if r["amount"] != ""), 2
        ),
        "loan_total": round(sum(r["amount"] for r in tables["loans"] if r["amount"] != ""), 2),
        "expenditure_total": round(
            sum(r["amount"] for r in tables["expenditures"] if r["amount"] != ""), 2
        ),
        "duplicate_rows_dropped": dict(duplicates_dropped),
        "unclassified_codes": dict(unknown),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary_legacy.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    return parser.parse_args(argv)


def main(argv=None) -> int:
    parse_args(argv)
    summary = build()

    for name, count in summary["row_counts"].items():
        print(f"  {name:20} {count:>10,}")
    print(f"  {'contributions total':20} ${summary['contribution_total']:>13,.2f}")
    print(f"  {'loans total':20} ${summary['loan_total']:>13,.2f}")
    print(f"  {'expenditures total':20} ${summary['expenditure_total']:>13,.2f}")

    if any(summary["duplicate_rows_dropped"].values()):
        print("\nExact-duplicate rows dropped (paper records re-entered):", file=sys.stderr)
        for form, n in summary["duplicate_rows_dropped"].items():
            if n:
                print(f"  {form:12} {n:>6,}", file=sys.stderr)

    if summary["unclassified_codes"]:
        print("\nUnclassified codes (kept, flagged, not guessed):", file=sys.stderr)
        for name, count in sorted(summary["unclassified_codes"].items(), key=lambda kv: -kv[1]):
            print(f"  {count:>6,}  {name}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
