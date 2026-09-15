import csv
from pathlib import Path

import normalize_legacy_c1 as nc1

FIXTURES = Path(__file__).parent / "fixtures" / "legacy_c1_sample"


def test_build_writes_one_row_per_disclosed_item(tmp_path):
    summary = nc1.build(raw_dir=FIXTURES, out_dir=tmp_path)
    assert summary["rows_written"] == 5  # 2 income (I/F) + 1 unknown (Z) + 1 property + 1 C-2
    assert summary["c1_filers"] == 1


def test_build_reports_unknown_income_type_codes_rather_than_dropping_them(tmp_path):
    summary = nc1.build(raw_dir=FIXTURES, out_dir=tmp_path)
    assert summary["unknown_income_codes"] == {"Z": 1}


def test_income_type_codes_match_nadc_tables_rtf():
    assert nc1.INCOME_TYPE_CODES == {
        "I": "income_source",
        "B": "business_association",
        "F": "financial_institution",
        "S": "stock",
        "C": "creditor",
        "G": "gift",
    }


def test_output_never_carries_a_street_address_column(tmp_path):
    """docs/PRIVACY.md rule 2 / ne-connect CLAUDE.md rule 1: home addresses
    never reach a displayed or written artifact."""
    nc1.build(raw_dir=FIXTURES, out_dir=tmp_path)
    out = tmp_path / "financial_interests_legacy.csv"
    with out.open(encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert "Candidate Address" not in header
    assert "address" not in " ".join(header).lower()


def test_strip_to_city_state_zip_removes_the_street_address_only():
    row = {"Candidate Address": "123 MAIN ST", "Candidate City": "LINCOLN",
           "Candidate State": "NE", "Candidate Zip": "68508"}
    stripped = nc1.strip_to_city_state_zip(row)
    assert "Candidate Address" not in stripped
    assert stripped["Candidate City"] == "LINCOLN"
    assert stripped["Candidate State"] == "NE"
    assert stripped["Candidate Zip"] == "68508"


def test_strip_to_city_state_zip_does_not_mutate_the_input():
    row = {"Candidate Address": "123 MAIN ST", "Candidate City": "LINCOLN"}
    nc1.strip_to_city_state_zip(row)
    assert row["Candidate Address"] == "123 MAIN ST"  # original untouched


def test_disclosure_id_joins_formc1_and_formc1inc_on_candidate_id_and_date_received(tmp_path):
    nc1.build(raw_dir=FIXTURES, out_dir=tmp_path)
    out = tmp_path / "financial_interests_legacy.csv"
    with out.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    income_rows = [r for r in rows if r["source_form"] == "formc1inc"]
    assert all(r["disclosure_id"] == "legacy:formc1:99FIS00001:03/13/2000" for r in income_rows)


def test_c2_row_is_its_own_disclosure_with_the_conflict_text_verbatim(tmp_path):
    nc1.build(raw_dir=FIXTURES, out_dir=tmp_path)
    out = tmp_path / "financial_interests_legacy.csv"
    with out.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    c2_row = next(r for r in rows if r["source_form"] == "formc2")
    assert c2_row["disclosure_id"] == "legacy:formc2:06FIS10677:04/06/2006"
    assert c2_row["item_type"] == "potential_conflict_of_interest"
    assert c2_row["detail"] == "The city acquired a lot and I intend to bid on it."


def test_property_row_maps_real_property_field_to_real_property_item_type(tmp_path):
    nc1.build(raw_dir=FIXTURES, out_dir=tmp_path)
    out = tmp_path / "financial_interests_legacy.csv"
    with out.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    prop_row = next(r for r in rows if r["source_form"] == "formc1prop")
    assert prop_row["item_type"] == "real_property"
    assert prop_row["counterparty_name_raw"] == "40 Acres Farmland Cass Co NE"


def test_every_row_is_tagged_pre2022_era_and_never_ocr(tmp_path):
    nc1.build(raw_dir=FIXTURES, out_dir=tmp_path)
    out = tmp_path / "financial_interests_legacy.csv"
    with out.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows
    assert all(r["era"] == "pre2022" for r in rows)
    assert all(r["ocr"] == "False" for r in rows)


def test_output_matches_the_shape_build_financial_interests_writes(tmp_path):
    """Same columns plus era/source_form, per module docstring -- a caller
    that unions both eras only ever needs to check one column."""
    import build_financial_interests as bfi

    nc1.build(raw_dir=FIXTURES, out_dir=tmp_path)
    out = tmp_path / "financial_interests_legacy.csv"
    with out.open(encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert set(bfi.FIELDNAMES).issubset(set(header))
