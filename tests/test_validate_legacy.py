from pathlib import Path

import pytest
from validate_legacy import HEADERS, ValidationError, validate_legacy_form

FIXTURES = Path(__file__).parent / "fixtures"


def test_confirmed_header_passes():
    text = (FIXTURES / "legacy_formb1ab_sample.txt").read_text()
    report = validate_legacy_form(text, form="formb1ab")
    report.raise_if_failing()  # must not raise
    assert report.header == HEADERS["formb1ab"]


def test_wrong_header_fails():
    text = "Wrong|Header\nval1|val2\n"
    report = validate_legacy_form(text, form="formb1ab")
    with pytest.raises(ValidationError, match="header doesn't match"):
        report.raise_if_failing()


def test_unknown_form_fails():
    text = "A|B\n1|2\n"
    report = validate_legacy_form(text, form="formzzz")
    with pytest.raises(ValidationError, match="not a known legacy form"):
        report.raise_if_failing()


def test_duplicate_rows_are_counted_but_not_fatal():
    text = (FIXTURES / "legacy_formb1ab_sample.txt").read_text()
    report = validate_legacy_form(text, form="formb1ab")
    assert report.identical_rows == 1  # the fixture's one intentional repeat
    report.raise_if_failing()  # must not raise despite the duplicate


def test_short_rows_are_counted_but_not_fatal():
    """Real data: several forms drop an always-empty trailing column entirely
    rather than emitting a placeholder pipe (see validate_legacy.py's
    docstring). That's expected, not corruption."""
    text = (FIXTURES / "legacy_formb2a_sample.txt").read_text()
    report = validate_legacy_form(text, form="formb2a")
    assert report.column_count_mismatches == 2  # both fixture rows are short
    report.raise_if_failing()  # must not raise


def test_zero_rows_fails():
    text = "Committee Name|Committee ID\n"
    report = validate_legacy_form(text, form="formb1ab")
    with pytest.raises(ValidationError, match="zero data rows"):
        report.raise_if_failing()


def test_every_needed_form_has_a_header():
    from download_legacy import NEEDED_FORMS

    assert set(NEEDED_FORMS) == set(HEADERS)
