from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from validate import SCHEMAS, ValidationError, parse_amount, parse_date, validate_extract

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name):
    return (FIXTURES / name).read_text()


def test_good_contributions_extract_passes():
    report = validate_extract(
        _fixture("contributions_sample.csv"), dataset="contributions", year=2026
    )

    assert report.row_count == 7
    assert report.column_count == 24
    assert report.duplicate_ids == 0
    assert report.identical_rows == 0
    assert report.unparsed_amounts == 0
    assert report.unparsed_dates == 0
    report.raise_if_failing()  # should not raise


def test_good_expenditures_extract_passes():
    report = validate_extract(
        _fixture("expenditures_sample.csv"), dataset="expenditures", year=2026
    )

    assert report.row_count == 6
    assert report.column_count == 28
    # Two IDs are shared across target rows -- legitimate for expenditures, so
    # it warns rather than failing.
    assert report.duplicate_ids == 2
    assert report.identical_rows == 0
    report.raise_if_failing()
    assert any("repeat an earlier row" in w for w in report.warnings)


def test_bad_extract_fails():
    csv_text = _fixture("contributions_bad.csv")
    report = validate_extract(csv_text, dataset="contributions", year=2026)

    # Column count is wrong (20, not the documented 24), which alone is a hard
    # failure. Columns are looked up by name, so the shifted header means the
    # date/amount columns simply aren't found rather than being misread.
    assert report.column_count == 20
    assert report.duplicate_ids == 1  # Receipt ID 200001 repeats

    with pytest.raises(ValidationError):
        report.raise_if_failing()


def test_repeated_ids_are_tolerated_in_both_datasets():
    """Neither ID is a row key, despite both layout PDFs calling them unique.

    Contributions to a slate committee repeat a Receipt ID once per candidate;
    an independent expenditure repeats an Expenditure ID once per target. 2022
    and 2023 contributions both fail if this is treated as a defect.
    """
    for dataset in ("contributions", "expenditures"):
        assert SCHEMAS[dataset].id_is_row_unique is False

    report = validate_extract(
        _fixture("expenditures_sample.csv"), dataset="expenditures", year=2026
    )
    assert report.duplicate_ids == 2
    report.raise_if_failing()  # counted and warned about, but not a failure


def test_repeated_ids_still_fail_a_schema_that_claims_uniqueness():
    # The flag is per-schema, so a dataset whose ID really is a row key is
    # still held to it. Verified through a stand-in rather than a live schema.
    strict = replace(SCHEMAS["contributions"], id_is_row_unique=True)
    report = validate_extract(
        _fixture("contributions_sample.csv"), dataset="contributions", year=2026
    )
    report.duplicate_ids = 3
    with patch.dict(SCHEMAS, {"contributions": strict}):
        with pytest.raises(ValidationError, match="duplicate Receipt IDs"):
            report.raise_if_failing()


def test_required_field_gate_catches_missing_filer_name():
    report = validate_extract(_fixture("contributions_bad.csv"), dataset="contributions", year=2026)

    # row 200002 has a blank Filer Name; 2 of 3 rows filled it in
    assert report.required_field_rates["Filer Name"] == pytest.approx(2 / 3)


def test_empty_csv_is_zero_rows_and_fails():
    report = validate_extract("Receipt ID,Org ID\n", dataset="contributions", year=2026)
    assert report.row_count == 0
    with pytest.raises(ValidationError):
        report.raise_if_failing()


def test_identical_rows_are_a_hard_failure():
    lines = _fixture("expenditures_sample.csv").splitlines()
    doubled = "\n".join(lines + [lines[1]]) + "\n"
    report = validate_extract(doubled, dataset="expenditures", year=2026)
    assert report.identical_rows == 1
    with pytest.raises(ValidationError, match="identical rows"):
        report.raise_if_failing()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("250.00", 250.0),
        ("1,000.00", 1000.0),
        ("$1,234.56", 1234.56),
        ("(500.00)", -500.0),
        ("", None),
        ("not-a-number", None),
    ],
)
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("01/15/2026", "2026-01-15"),
        ("2026-01-15", "2026-01-15"),
        ("not-a-date", None),
        ("", None),
    ],
)
def test_parse_date(raw, expected):
    parsed = parse_date(raw)
    assert (parsed.isoformat() if parsed else None) == expected
