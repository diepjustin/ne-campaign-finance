"""Integrity gate for NADC bulk extracts.

CalMatters' rule for `powersearch-download` is to error out loudly if the
downloaded data looks wrong rather than silently shipping it. This module is
that gate for Nebraska's FirstTuesday bulk CSV extracts.

Header text for both datasets is locked to real pulls (confirmed 2026-09-08):
a renamed, dropped, or added column is a hard failure, on the theory that the
state changing this schema out from under us is the thing most likely to
break the whole pipeline silently.

Transaction TYPE values are deliberately not locked the same way. The layout
PDFs list idealized names that the data does not actually use -- the PDF says
"In-Kind", the extract says "In-Kind Contribution"; the PDF says "Pledge
Payment", the extract says "Pledge Payment Received"; and the extract carries
"Earmarked Monetary"/"Earmarked In-Kind" the contributions PDF never mentions.
So normalize.py classifies known values and warns on unknown ones rather than
failing, which keeps a newly-introduced type visible instead of dropped.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime

# Confirmed against a real download (2026-09-08). Column L's parenthetical
# ("Individual Last Name") is the state's own annotation, not a typo.
CONTRIBUTIONS_COLUMNS = [
    "Receipt ID",
    "Org ID",
    "Filer Type",
    "Filer Name",
    "Candidate Name",
    "Receipt Transaction/Contribution Type",
    "Other Funds Type",
    "Receipt Date",
    "Receipt Amount",
    "Description",
    "Contributor or Transaction Source Type",
    "Contributor or Source Name (Individual Last Name)",
    "First Name",
    "Middle Name",
    "Suffix",
    "Address 1",
    "Address 2",
    "City",
    "State",
    "Zip",
    "Filed Date",
    "Amended",
    "Employer",
    "Occupation",
]

# A-AB per NEExpendituresFileLayout.pdf, but with the header text taken from a
# real 2026 pull -- the PDF is wrong in three places, so trusting it would have
# failed every run: the payee column has no "(Individual Last Name)"
# parenthetical (unlike its contributions counterpart, which does), "Support Or
# Oppose" capitalizes the "Or", and the jurisdiction column spaces out its
# hyphens.
#
# Wider than contributions because the independent-expenditure fields (Support
# Or Oppose / Candidate Name or Ballot Issue / Jurisdiction - Office -
# District) ride in this same file rather than a separate dataset.
EXPENDITURES_COLUMNS = [
    "Expenditure ID",
    "Org ID",
    "Filer Type",
    "Filer Name",
    "Candidate Name",
    "Expenditure Transaction Type",
    "Expenditure Sub Type",
    "Expenditure Date",
    "Expenditure Amount",
    "Description",
    "Payee or Recipient or In-Kind Contributor Type",
    "Payee or Recipient or In-Kind Contributor Name",
    "First Name",
    "Middle Name",
    "Suffix",
    "Address 1",
    "Address 2",
    "City",
    "State",
    "Zip",
    "Filed Date",
    "Support Or Oppose",
    "Candidate Name or Ballot Issue",
    "Jurisdiction - Office - District or Ballot Description",
    "Amended",
    "Employer",
    "Occupation",
    "Principal Place of Business",
]

MIN_REQUIRED_FIELD_RATE = 0.98
MAX_ROW_COUNT_DROP_FRACTION = 0.20  # vs previous run, before --allow-shrink

_AMOUNT_RE = re.compile(r"^\(?-?\$?[\d,]+(\.\d+)?\)?$")
_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y")


class ValidationError(Exception):
    """Raised when an extract fails the integrity gate."""


@dataclass(frozen=True)
class Schema:
    """What one dataset's extract is supposed to look like."""

    dataset: str
    columns: list
    id_column: str
    date_column: str
    amount_column: str
    type_column: str
    org_column: str
    filer_column: str
    # Whether id_column identifies a ROW. False for both datasets: the state
    # fans one transaction out over an associated dimension and repeats the
    # ID. An independent expenditure against five candidates is five rows
    # sharing an Expenditure ID; a contribution to a slate committee is one
    # row per candidate the committee supports, sharing a Receipt ID.
    #
    # Both PDFs call these IDs "unique". Neither is. 2026 contributions have
    # no repeats at all, which is exactly why this was set to True on the
    # first pass -- 2022 (37 repeated IDs) and 2023 (7) then failed the gate.
    # Kept as a per-schema flag rather than deleted, because a dataset whose
    # ID really is a row key should still be held to that.
    id_is_row_unique: bool = True

    @property
    def required_columns(self) -> list:
        # The four a row is useless without: who filed it, when, how much, and
        # the ID we dedupe and cross-reference on.
        return [self.id_column, self.filer_column, self.date_column, self.amount_column]


SCHEMAS = {
    "contributions": Schema(
        dataset="contributions",
        columns=CONTRIBUTIONS_COLUMNS,
        id_column="Receipt ID",
        date_column="Receipt Date",
        amount_column="Receipt Amount",
        type_column="Receipt Transaction/Contribution Type",
        org_column="Org ID",
        filer_column="Filer Name",
        id_is_row_unique=False,
    ),
    "expenditures": Schema(
        dataset="expenditures",
        columns=EXPENDITURES_COLUMNS,
        id_column="Expenditure ID",
        date_column="Expenditure Date",
        amount_column="Expenditure Amount",
        type_column="Expenditure Transaction Type",
        org_column="Org ID",
        filer_column="Filer Name",
        id_is_row_unique=False,
    ),
}


@dataclass
class ValidationReport:
    dataset: str
    year: int
    row_count: int
    column_count: int
    header: list
    required_field_rates: dict
    duplicate_ids: int
    identical_rows: int
    unparsed_dates: int
    unparsed_amounts: int
    warnings: list = field(default_factory=list)

    def raise_if_failing(self) -> None:
        schema = SCHEMAS[self.dataset]
        problems = []
        if self.header != schema.columns:
            problems.append(
                f"header doesn't match the confirmed layout -- "
                f"got {self.header!r}, expected {schema.columns!r}"
            )
        for name, rate in self.required_field_rates.items():
            if rate < MIN_REQUIRED_FIELD_RATE:
                problems.append(
                    f"required field {name!r} only {rate:.1%} non-null "
                    f"(floor {MIN_REQUIRED_FIELD_RATE:.0%})"
                )
        if schema.id_is_row_unique and self.duplicate_ids:
            problems.append(f"{self.duplicate_ids} duplicate {schema.id_column}s")
        # A row repeated in every single field is the state emitting the same
        # record twice -- unlike a shared ID, that is never meaningful.
        if self.identical_rows:
            problems.append(f"{self.identical_rows} fully identical rows")
        if self.row_count == 0:
            problems.append("zero data rows")
        if problems:
            raise ValidationError(f"{self.dataset} {self.year}: " + "; ".join(problems))


def parse_amount(raw: str):
    """Dollar string -> float, or None if it isn't one. Parens mean negative."""
    raw = (raw or "").strip()
    if not raw or not _AMOUNT_RE.match(raw):
        return None
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = raw.strip("()").lstrip("-$").replace(",", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def parse_date(raw: str):
    """Extract date string -> date, or None. The extracts use M/D/YYYY."""
    raw = (raw or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def validate_extract(csv_text: str, *, dataset: str, year: int) -> ValidationReport:
    """Run the integrity gate over one raw extract.

    Returns a report even when the data is bad, so a caller can log the whole
    picture; call ``report.raise_if_failing()`` to enforce the gate.

    Columns are looked up by NAME, not position. A shifted or renamed header
    therefore shows up as an empty column rather than silently reading the
    wrong field -- and the header check fails the run regardless.
    """
    schema = SCHEMAS[dataset]
    reader = csv.DictReader(io.StringIO(csv_text))
    header = reader.fieldnames or []
    rows = list(reader)

    required_hits = {name: 0 for name in schema.required_columns}
    seen_ids = set()
    seen_rows = set()
    duplicate_ids = 0
    identical_rows = 0
    unparsed_dates = 0
    unparsed_amounts = 0
    warnings = []

    for row in rows:
        fingerprint = tuple(row.get(col) for col in header)
        if fingerprint in seen_rows:
            identical_rows += 1
        seen_rows.add(fingerprint)

        for name in schema.required_columns:
            if (row.get(name) or "").strip():
                required_hits[name] += 1

        row_id = (row.get(schema.id_column) or "").strip()
        if row_id:
            if row_id in seen_ids:
                duplicate_ids += 1
            seen_ids.add(row_id)

        raw_date = (row.get(schema.date_column) or "").strip()
        if raw_date and parse_date(raw_date) is None:
            unparsed_dates += 1

        raw_amount = (row.get(schema.amount_column) or "").strip()
        if raw_amount and parse_amount(raw_amount) is None:
            unparsed_amounts += 1

    row_count = len(rows)
    required_field_rates = {
        name: (hits / row_count if row_count else 0.0) for name, hits in required_hits.items()
    }

    if unparsed_dates:
        warnings.append(f"{unparsed_dates} rows had an unparseable {schema.date_column}")
    if unparsed_amounts:
        warnings.append(f"{unparsed_amounts} rows had an unparseable {schema.amount_column}")
    # Expected for expenditures, so not a failure -- but worth saying out loud,
    # because it is exactly the thing that makes SUM(amount) wrong.
    if duplicate_ids and not schema.id_is_row_unique:
        warnings.append(
            f"{duplicate_ids} rows repeat an earlier row's {schema.id_column} "
            "(one transaction fanned out over several candidates) -- "
            "see normalize.py's include_in_total"
        )

    return ValidationReport(
        dataset=dataset,
        year=year,
        row_count=row_count,
        column_count=len(header),
        header=header,
        required_field_rates=required_field_rates,
        duplicate_ids=duplicate_ids,
        identical_rows=identical_rows,
        unparsed_dates=unparsed_dates,
        unparsed_amounts=unparsed_amounts,
        warnings=warnings,
    )
