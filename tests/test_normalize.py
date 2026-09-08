import csv
import json
from pathlib import Path

import normalize
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _rows(name):
    with (FIXTURES / name).open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _read(path):
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _receipts():
    return normalize.normalize_receipts(
        _rows("contributions_sample.csv"), year=2026, snapshot="2026-09-08.csv"
    )


def _expenditures():
    return normalize.normalize_expenditures(
        _rows("expenditures_sample.csv"), year=2026, snapshot="2026-09-08.csv"
    )


# --- receipts ---------------------------------------------------------------


def test_receipts_partition_every_input_row():
    """Nothing is dropped and nothing is counted twice."""
    source = _rows("contributions_sample.csv")
    tables, _ = _receipts()
    assert sum(len(rows) for rows in tables.values()) == len(source)

    ids = [row["receipt_id"] for rows in tables.values() for row in rows]
    assert sorted(ids) == sorted(r["Receipt ID"] for r in source)
    assert len(set(ids)) == len(ids)


def test_loans_are_not_counted_as_contributions():
    tables, _ = _receipts()
    assert [r["receipt_id"] for r in tables["loans"]] == ["100004"]
    assert "100004" not in {r["receipt_id"] for r in tables["contributions"]}


def test_bare_pledge_is_not_a_contribution():
    # A pledge is a promise; the money arrives later as "Pledge Payment
    # Received". Counting both would double-count the same dollar.
    tables, _ = _receipts()
    assert "100006" in {r["receipt_id"] for r in tables["other_receipts"]}
    assert "100006" not in {r["receipt_id"] for r in tables["contributions"]}


def test_unknown_receipt_type_is_kept_and_reported():
    tables, unknown = _receipts()
    assert unknown == {"Brand New Receipt Type": 1}
    assert "100007" in {r["receipt_id"] for r in tables["other_receipts"]}


def test_receipts_are_normalized():
    tables, _ = _receipts()
    row = next(r for r in tables["contributions"] if r["receipt_id"] == "100002")
    assert row["amount"] == 1000.0  # was the string "1,000.00"
    assert row["receipt_date"] == "2026-02-01"  # was "02/01/2026"
    assert row["source_year"] == 2026
    assert row["source_snapshot"] == "2026-09-08.csv"


def test_display_name_handles_people_and_organizations():
    # An organization's whole name sits in the last-name column.
    assert normalize.display_name("Test PAC", "", "", "") == "Test PAC"
    assert normalize.display_name("Smith", "John", "Q", "Jr") == "John Q Smith Jr"
    assert normalize.display_name("Doe", "Jane", "", "") == "Jane Doe"
    assert normalize.display_name("", "", "", "") == ""


# --- expenditures -----------------------------------------------------------


def test_repeated_amount_across_targets_counts_once():
    """One $1,200 mailer aimed at two candidates is $1,200, not $2,400."""
    rows, _ = _expenditures()
    group = [r for r in rows if r["expenditure_id"] == "900002"]
    assert len(group) == 2
    assert all(r["rows_sharing_id"] == 2 for r in group)
    assert [r["include_in_total"] for r in group] == [True, False]
    assert sum(r["amount"] for r in group if r["include_in_total"]) == 1200.0


def test_split_amount_across_targets_counts_every_row():
    """A buy the state already split 300/700 is $1,000 across both rows."""
    rows, _ = _expenditures()
    group = [r for r in rows if r["expenditure_id"] == "900003"]
    assert len(group) == 2
    assert all(r["include_in_total"] for r in group)
    assert sum(r["amount"] for r in group if r["include_in_total"]) == 1000.0


def test_single_row_expenditure_always_counts():
    rows, _ = _expenditures()
    row = next(r for r in rows if r["expenditure_id"] == "900001")
    assert row["rows_sharing_id"] == 1
    assert row["include_in_total"] is True


def test_unknown_expenditure_type_is_kept_and_reported():
    rows, unknown = _expenditures()
    assert unknown == {"Brand New Type The State Invented": 1}
    assert any(r["expenditure_id"] == "900004" for r in rows)


def test_independent_expenditure_fields_survive():
    rows, _ = _expenditures()
    row = next(r for r in rows if r["expenditure_id"] == "900002")
    assert row["support_or_oppose"] == "SUPPORT"
    assert row["target_name"] == "Test Candidate"
    assert row["target_jurisdiction"] == "Legislature - District 1"


# --- end to end -------------------------------------------------------------


@pytest.fixture
def built(tmp_path):
    raw = tmp_path / "raw"
    for dataset, fixture in (
        ("contributions", "contributions_sample.csv"),
        ("expenditures", "expenditures_sample.csv"),
    ):
        year_dir = raw / dataset / "2026"
        year_dir.mkdir(parents=True)
        (year_dir / "2026-09-08.csv").write_text((FIXTURES / fixture).read_text())
    out = tmp_path / "processed"
    return normalize.build([2026], raw_dir=raw, out_dir=out), out


def test_build_writes_every_table(built):
    summary, out = built
    for name in (
        "contributions",
        "loans",
        "other_receipts",
        "expenditures",
        "independent_expenditures",
    ):
        assert (out / f"{name}.csv").exists()
        assert summary["row_counts"][name] == len(_read(out / f"{name}.csv"))
    assert json.loads((out / "summary.json").read_text())["years"] == [2026]


def test_independent_expenditures_are_a_subset_not_an_addition(built):
    _, out = built
    expenditures = _read(out / "expenditures.csv")
    independent = _read(out / "independent_expenditures.csv")
    keys = {(r["expenditure_id"], r["target_name"]) for r in expenditures}
    for row in independent:
        assert (row["expenditure_id"], row["target_name"]) in keys


def test_build_totals_respect_include_in_total(built):
    summary, _ = built
    # 500 + 1200 (counted once, not twice) + 300 + 700 + 42
    assert summary["expenditure_total"] == 2742.00
    # 250 + 1000 + 75.50, with the loan, pledge, interest and unknown excluded
    assert summary["contribution_total"] == 1325.50
    assert summary["independent_expenditure_total"] == 2200.00


def test_build_reports_missing_snapshots(tmp_path):
    summary = normalize.build([2099], raw_dir=tmp_path / "raw", out_dir=tmp_path / "processed")
    assert sorted(summary["missing_snapshots"]) == [
        "contributions 2099",
        "expenditures 2099",
    ]


def test_latest_snapshot_picks_the_newest_pull(tmp_path):
    year_dir = tmp_path / "raw" / "contributions" / "2026"
    year_dir.mkdir(parents=True)
    for name in ("2026-09-01.csv", "2026-09-08.csv", "2026-08-15.csv"):
        (year_dir / name).write_text("")
    picked = normalize.latest_snapshot("contributions", 2026, tmp_path / "raw")
    assert picked.name == "2026-09-08.csv"
