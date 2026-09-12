"""Integrity gate for the pre-2022 NADC legacy files.

Same philosophy as validate.py: error out loudly if a file doesn't look like
what was confirmed against a real download, rather than silently normalizing
something that shifted shape underneath us. Headers below are transcribed
directly from a real fetch (2026-09-11, sha256 d08d542233...32dee225) via
`head -1 <form>.txt`, not from `nadc_tables.rtf`'s prose descriptions --
same reasoning as validate.py's own comment about the modern extracts: a
schema doc and a real file can disagree, and the real file is what gets
normalized.

Unlike validate.py, an exact-duplicate row here is expected and NOT fatal --
these are paper records re-entered by hand over two decades, and 198-574
byte-identical rows show up in a real pull of the biggest forms. They are
counted and dropped by normalize_legacy.py, not treated as a data-quality
failure the way a header mismatch is.

A short row is ALSO not fatal, for a related reason found the same way: in six
of the thirteen forms (formb2a, formb4a, formb72, formb73, formb2b, formb4b1),
every single data row is one field short of its own header -- "Report ID" (or
the equivalent trailing-ish column) is essentially never populated in these
forms, and the export drops it rather than emitting an empty field between two
pipes. Confirmed with a direct byte read of real rows, not inferred: e.g.
formb2a's header lists 9 columns but `10PPC00067|10/09/2012|99PAC00069|
08/20/2012|70|||NE STATE EDUCATION ASSOCIATION PAC` only has 8 values. This is
consistent per-form (either every row is short by exactly one, or none are),
not random corruption, so it is reported (`column_count_mismatches`) rather
than failing the gate. `normalize_legacy.py` needs to read these rows
positionally with a missing trailing field defaulting to empty, not with a
strict `dict(zip(header, row))` that would silently misalign every later
column.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

DELIMITER = "|"

# Confirmed against the real pull named in this module's docstring. A form
# name here must match a key in download_legacy.NEEDED_FORMS.
HEADERS = {
    "formb1ab": [
        "Committee Name", "Committee ID", "Date Received", "Type of Contributor",
        "Contributor ID", "Contribution Date", "Cash Contribution", "In-Kind Contribution",
        "Unpaid Pledges", "Contributor Last Name", "Contributor First Name",
        "Contributor Middle Initial", "Contributor Organization Name", "Contributor Address",
        "Contributor City", "Contributor State", "Contributor Zipcode",
    ],
    "formb2a": [
        "Committee ID", "Date Received", "Contributor ID", "Contribution Date",
        "Cash Contribution", "In-Kind Contribution", "Unpaid Pledges", "Report ID",
        "Contributor Name",
    ],
    "formb4a": [
        "Committee ID", "Date Received", "Contributor ID", "Contribution Date",
        "Cash Contribution", "In-Kind Contribution", "Unpaid Pledges", "Report ID",
        "Contributor Name",
    ],
    "formb5": [
        "Committee Name", "Committee ID", "Date Received", "Date Last Revised",
        "Last Revised By", "Postmark Date", "Microfilm Number", "Contributor ID",
        "Type of Contributor", "Nature of Contribution", "Date of Contribution", "Amount",
        "Occupation", "Employer", "Place of Business", "Contributor Name",
    ],
    "formb72": [
        "Committee Name", "Committee ID", "Date Received", "Contributor ID",
        "Contribution Date", "Amount", "Microfilm Number", "Report ID", "Contributor Name",
    ],
    "formb73": [
        "Committee Name", "Committee ID", "Date Received", "Contributor ID",
        "Contribution Date", "Amount", "Nature Of Contribution", "Support/Oppose",
        "Description", "Microfilm Number", "Report ID", "Contributor Name",
    ],
    "formb1d": [
        "Committee Name", "Committee ID", "Date Received", "Payee Name", "Payee Address",
        "Expenditure Purpose", "Expenditure Date", "Amount", "In-Kind",
    ],
    "formb2b": [
        "Committee ID", "Date Received", "Committee ID Expenditure is For", "Support/Oppose",
        "Nature of Expenditure", "Expenditure Date", "Amount", "Description", "Line ID",
        "Report ID", "Committee Name Expenditure is For",
    ],
    "formb4b1": [
        "Form ID Number", "Committee ID", "Date Received", "Committee Expenditure ID",
        "Support/Oppose", "Nature of Expenditure", "Expenditure Date", "Amount",
        "Expense Category", "Report ID", "Expenditure Committee Name",
    ],
    "formc1": [
        "Candidate First Name", "Candidate Middle Initial", "Candidate Last Name",
        "Candidate Address", "Candidate City", "Candidate State", "Candidate Zip",
        "Office Held", "Subdivision", "Candidate ID", "Date Last Revised", "Last Revised By",
        "Date Received", "Postmark Date", "Microfilm Number", "Form Filed", "Elective Office",
        "Annual or Employee Report", "Left Office", "New Appt", "Primary Result",
        "General Result", "Reason for Leaving", "Office Code", "Preceding Year",
        "Date Left Office", "New Filer", "Hearing", "Elected Postion", "Amended", "Notes",
        "No Exceptions", "Add Exceptions", "Delete Exceptions", "Change Exceptions",
        "Required Filing",
    ],
    "formc1inc": [
        "Candidate First Name", "Candidate Last Name", "Candidate ID", "Date Received",
        "Income Source Name", "Income Source Address", "Income Reason", "Type of Inocome",
    ],
    "formc1prop": [
        "Candidate Last Name", "Candidate First Name", "Candidate ID", "Date Received",
        "Real Property", "Other Property",
    ],
    "formc2": [
        "Candidate ID Number", "Date Received", "Last Revised By", "Last Revised Date",
        "Postmark Date", "Microfilm Number", "Candidate Last Name", "Candidate First Name",
        "Candidate Middle Initial", "Candidate Phone", "Candidate Address", "Candidate City",
        "Candidate State", "Candidate Zip", "County", "Title", "Agency", "Agency Address",
        "Agency Phone", "Immediate Supervisor", "Supervisor Title", "Conflict",
        "Nature of Benefit", "Candidate Receive Benefit", "Family Receive Benefit",
        "Name of Family Memeber", "Business Receive Benefit", "Business Name",
        "Member of Legislature Only",
    ],
}


class ValidationError(Exception):
    """Raised when a legacy form fails the integrity gate."""


@dataclass
class ValidationReport:
    form: str
    row_count: int
    column_count: int
    header: list
    identical_rows: int
    column_count_mismatches: int
    warnings: list = field(default_factory=list)

    def raise_if_failing(self) -> None:
        expected = HEADERS.get(self.form)
        problems = []
        if expected is None:
            problems.append(f"{self.form!r} is not a known legacy form -- add it to HEADERS first")
        elif self.header != expected:
            problems.append(
                f"header doesn't match the confirmed layout -- "
                f"got {self.header!r}, expected {expected!r}"
            )
        if self.row_count == 0:
            problems.append("zero data rows")
        if problems:
            raise ValidationError(f"{self.form}: " + "; ".join(problems))


def validate_legacy_form(text: str, *, form: str) -> ValidationReport:
    """Run the integrity gate over one extracted legacy `.txt` file.

    Returns a report even when the file is bad, so a caller can log the whole
    picture; call ``report.raise_if_failing()`` to enforce the gate.

    Exact-duplicate rows are counted but never fail the gate here -- see this
    module's docstring for why that differs from validate.py's modern-extract
    behavior. normalize_legacy.py decides whether and how to drop them.
    """
    reader = csv.reader(io.StringIO(text), delimiter=DELIMITER)
    rows = list(reader)
    header = rows[0] if rows else []
    data_rows = rows[1:]

    column_count = len(header)
    column_count_mismatches = sum(1 for row in data_rows if len(row) != column_count)

    seen = set()
    identical_rows = 0
    for row in data_rows:
        fingerprint = tuple(row)
        if fingerprint in seen:
            identical_rows += 1
        seen.add(fingerprint)

    warnings = []
    if identical_rows:
        warnings.append(
            f"{identical_rows} exact-duplicate rows -- paper records re-entered more than "
            "once are expected here; dropped by normalize_legacy.py, not a failure"
        )

    return ValidationReport(
        form=form,
        row_count=len(data_rows),
        column_count=column_count,
        header=header,
        identical_rows=identical_rows,
        column_count_mismatches=column_count_mismatches,
        warnings=warnings,
    )
