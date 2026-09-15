"""normalize_legacy.py: the three real classification decisions (independent
expenditures buried in a contributions form, loans buried in a contributions
form, undocumented expenditure codes), the Report-ID positional-read fix, and
the dedup/sentinel-date safeguards.
"""

from pathlib import Path

import normalize_legacy as nl
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _unknown():
    from collections import Counter

    return Counter()


# --- formb73: independent expenditures must NOT become contributions -------


def test_formb73_independent_expenditure_goes_to_expenditures_not_contributions():
    row = {
        "Committee Name": "Example Corp PAC", "Committee ID": "99CUA00004",
        "Date Received": "07/13/2000", "Contributor ID": "99BQC00038",
        "Contribution Date": "05/09/2000", "Amount": "807.81",
        "Nature Of Contribution": "E", "Support/Oppose": "SUPPORT",
        "Description": "mailer", "Microfilm Number": "5960193",
        "Contributor Name": "BUILD IT OMAHA COMMITTEE",
    }
    out = nl._normalize_formb73([row], year=2000, snapshot="s", unknown=_unknown())
    assert len(out) == 1
    table, result = out[0]
    assert table == "expenditures"
    assert result["transaction_type"] == "Independent Expenditure"
    assert result["target_name"] == "BUILD IT OMAHA COMMITTEE"
    assert result["support_or_oppose"] == "SUPPORT"
    assert result["amount"] == 807.81


def test_formb73_in_kind_stays_a_contribution():
    row = {
        "Committee Name": "Example", "Committee ID": "1", "Date Received": "01/01/2000",
        "Contributor ID": "2", "Contribution Date": "01/01/2000", "Amount": "100",
        "Nature Of Contribution": "I", "Support/Oppose": "", "Description": "",
        "Microfilm Number": "", "Contributor Name": "A Donor",
    }
    out = nl._normalize_formb73([row], year=2000, snapshot="s", unknown=_unknown())
    assert out == [("contributions", out[0][1])]
    assert out[0][1]["transaction_type"] == "In-Kind Contribution"


# --- formb5: loans must NOT become contributions ----------------------------


def test_formb5_loan_goes_to_loans_not_contributions():
    row = {
        "Committee Name": "Example", "Committee ID": "1", "Date Received": "01/01/2000",
        "Date Last Revised": "", "Last Revised By": "", "Postmark Date": "",
        "Microfilm Number": "", "Contributor ID": "2", "Type of Contributor": "B",
        "Nature of Contribution": "L", "Date of Contribution": "01/01/2000",
        "Amount": "50000", "Occupation": "", "Employer": "", "Place of Business": "",
        "Contributor Name": "A Bank",
    }
    out = nl._normalize_formb5([row], year=2000, snapshot="s", unknown=_unknown())
    assert len(out) == 1
    table, result = out[0]
    assert table == "loans"
    assert result["transaction_type"] == "Loan"
    assert result["amount"] == 50000.0


def test_formb5_monetary_stays_a_contribution():
    row = {
        "Committee Name": "Example", "Committee ID": "1", "Date Received": "01/01/2000",
        "Date Last Revised": "", "Last Revised By": "", "Postmark Date": "",
        "Microfilm Number": "", "Contributor ID": "2", "Type of Contributor": "B",
        "Nature of Contribution": "M", "Date of Contribution": "01/01/2000",
        "Amount": "100", "Occupation": "", "Employer": "", "Place of Business": "",
        "Contributor Name": "A Donor",
    }
    out = nl._normalize_formb5([row], year=2000, snapshot="s", unknown=_unknown())
    assert out[0][0] == "contributions"


# --- formb4b1: undocumented codes are kept and flagged, never dropped ------


def test_formb4b1_unknown_code_is_kept_not_dropped():
    row = {
        "Form ID Number": "1", "Committee ID": "1", "Date Received": "01/01/2000",
        "Committee Expenditure ID": "2", "Support/Oppose": "0", "Nature of Expenditure": "A",
        "Expenditure Date": "01/01/2000", "Amount": "1000", "Expense Category": "026",
        "Expenditure Committee Name": "Example",
    }
    unknown = _unknown()
    out = nl._normalize_formb4b1([row], year=2000, snapshot="s", unknown=unknown)
    assert len(out) == 1
    table, result = out[0]
    assert table == "expenditures"  # kept, not dropped
    assert result["transaction_type"] == "A"  # raw code, not guessed at
    assert unknown["formb4b1 Nature of Expenditure: 'A'"] == 1


def test_formb4b1_documented_codes_are_not_flagged():
    unknown = _unknown()
    for code in ("D", "I", "E", "L"):
        row = {
            "Form ID Number": "1", "Committee ID": "1", "Date Received": "01/01/2000",
            "Committee Expenditure ID": "2", "Support/Oppose": "0",
            "Nature of Expenditure": code, "Expenditure Date": "01/01/2000",
            "Amount": "1000", "Expense Category": "026", "Expenditure Committee Name": "Example",
        }
        nl._normalize_formb4b1([row], year=2000, snapshot="s", unknown=unknown)
    assert len(unknown) == 0


# --- Report ID positional read (the 6 "short" forms) ------------------------


def test_short_form_reads_contributor_name_not_report_id():
    """The real bug this guards against: right-padding a missing trailing
    field would put the Contributor Name value into "Report ID" and leave
    "Contributor Name" empty, silently losing every name in six forms."""
    text = (
        "Committee ID|Date Received|Contributor ID|Contribution Date|"
        "Cash Contribution|In-Kind Contribution|Unpaid Pledges|Report ID|Contributor Name\n"
        "10PPC00067|10/09/2012|99PAC00069|08/20/2012|70|||"
        "NE STATE EDUCATION ASSOCIATION PAC\n"
    )
    header = nl._effective_header("formb2a")
    import csv
    import io

    row = list(csv.reader(io.StringIO(text), delimiter="|"))[1]
    parsed = dict(zip(header, row))
    assert parsed["Contributor Name"] == "NE STATE EDUCATION ASSOCIATION PAC"
    assert "Report ID" not in header  # dropped, not defaulted to ""


# --- sentinel dates ----------------------------------------------------------


@pytest.mark.parametrize("sentinel", ["12/31/9999", "01/01/0001", "01/01/0900"])
def test_sentinel_dates_are_dropped_not_shipped_as_real_dates(sentinel):
    counter = _unknown()
    result = nl._legacy_date(sentinel, counter)
    assert result == ""
    assert counter["sentinel_dates_dropped"] == 1


def test_real_date_passes_through():
    counter = _unknown()
    assert nl._legacy_date("01/05/2000", counter) == "2000-01-05"
    assert counter["sentinel_dates_dropped"] == 0


# --- end to end: dedup and every table gets written -------------------------

_MINIMAL_FORMS = {
    "formb1ab": (
        "Committee Name|Committee ID|Date Received|Type of Contributor|Contributor ID|"
        "Contribution Date|Cash Contribution|In-Kind Contribution|Unpaid Pledges|"
        "Contributor Last Name|Contributor First Name|Contributor Middle Initial|"
        "Contributor Organization Name|Contributor Address|Contributor City|"
        "Contributor State|Contributor Zipcode\n"
        "Test Committee|1|01/01/2000|C|2|01/01/2000|100|0|0||||Test Org|1 St|Lincoln|NE|68508\n"
        "Test Committee|1|01/01/2000|C|2|01/01/2000|100|0|0||||Test Org|1 St|Lincoln|NE|68508\n"
    ),
    "formb2a": (
        "Committee ID|Date Received|Contributor ID|Contribution Date|Cash Contribution|"
        "In-Kind Contribution|Unpaid Pledges|Report ID|Contributor Name\n"
        "1|01/01/2000|2|01/01/2000|50|||A PAC\n"
    ),
    "formb4a": (
        "Committee ID|Date Received|Contributor ID|Contribution Date|Cash Contribution|"
        "In-Kind Contribution|Unpaid Pledges|Report ID|Contributor Name\n"
        "1|01/01/2000|2|01/01/2000|50|||A PAC\n"
    ),
    "formb5": (
        "Committee Name|Committee ID|Date Received|Date Last Revised|Last Revised By|"
        "Postmark Date|Microfilm Number|Contributor ID|Type of Contributor|"
        "Nature of Contribution|Date of Contribution|Amount|Occupation|Employer|"
        "Place of Business|Contributor Name\n"
        "Test|1|01/01/2000|01/01/2000|x|01/01/2000|1|2|B|L|01/01/2000|5000|||A Bank\n"
    ),
    "formb72": (
        "Committee Name|Committee ID|Date Received|Contributor ID|Contribution Date|"
        "Amount|Microfilm Number|Report ID|Contributor Name\n"
        "Test|1|01/01/2000|2|01/01/2000|25|1|A Donor\n"
    ),
    "formb73": (
        "Committee Name|Committee ID|Date Received|Contributor ID|Contribution Date|"
        "Amount|Nature Of Contribution|Support/Oppose|Description|Microfilm Number|"
        "Report ID|Contributor Name\n"
        "Test|1|01/01/2000|2|01/01/2000|500|E|SUPPORT|ad|1|A Target\n"
    ),
    "formb1d": (
        "Committee Name|Committee ID|Date Received|Payee Name|Payee Address|"
        "Expenditure Purpose|Expenditure Date|Amount|In-Kind\n"
        "Test|1|01/01/2000|A Payee|1 St Lincoln NE|expense|01/01/2000|75|0\n"
    ),
    "formb2b": (
        "Committee ID|Date Received|Committee ID Expenditure is For|Support/Oppose|"
        "Nature of Expenditure|Expenditure Date|Amount|Description|Line ID|"
        "Report ID|Committee Name Expenditure is For\n"
        "1|01/01/2000|2|0|D|01/01/2000|10||001|Target Committee\n"
    ),
    "formb4b1": (
        "Form ID Number|Committee ID|Date Received|Committee Expenditure ID|"
        "Support/Oppose|Nature of Expenditure|Expenditure Date|Amount|"
        "Expense Category|Report ID|Expenditure Committee Name\n"
        "1|1|01/01/2000|2|0|A|01/01/2000|999|026|Target Committee\n"
    ),
}


@pytest.fixture
def built(tmp_path):
    legacy_dir = tmp_path / "raw" / "2026-01-01"
    legacy_dir.mkdir(parents=True)
    for form, text in _MINIMAL_FORMS.items():
        (legacy_dir / f"{form}.txt").write_text(text)
    out = tmp_path / "processed"
    summary = nl.build(raw_dir=tmp_path / "raw", out_dir=out)
    return summary, out


def test_build_writes_every_legacy_table(built):
    summary, out = built
    for name in ("contributions", "loans", "other_receipts", "expenditures"):
        assert (out / f"{name}_legacy.csv").exists()


def test_build_drops_exact_duplicate_rows(built):
    summary, _ = built
    assert summary["duplicate_rows_dropped"]["formb1ab"] == 1


def test_build_routes_the_three_decisions_correctly(built):
    summary, out = built
    import csv as _csv

    def _read(name):
        with (out / name).open() as fh:
            return list(_csv.DictReader(fh))

    loans = _read("loans_legacy.csv")
    assert len(loans) == 1 and loans[0]["source_form"] == "formb5"

    expenditures = _read("expenditures_legacy.csv")
    formb73_rows = [r for r in expenditures if r["source_form"] == "formb73"]
    assert len(formb73_rows) == 1
    assert formb73_rows[0]["transaction_type"] == "Independent Expenditure"

    unclassified = [r for r in expenditures if r["source_form"] == "formb4b1"]
    assert len(unclassified) == 1
    assert unclassified[0]["transaction_type"] == "A"


def test_every_output_row_carries_era_and_source_form(built):
    _, out = built
    import csv as _csv

    for name in ("contributions_legacy.csv", "loans_legacy.csv", "expenditures_legacy.csv"):
        with (out / name).open() as fh:
            for row in _csv.DictReader(fh):
                assert row["era"] == "pre2022"
                assert row["source_form"]
