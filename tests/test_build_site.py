"""build_search_index() feeds the 1.3 search page -- privacy-sensitive, since
it is the one place a home street address could leak into a public payload.
"""

import build_site

HEADER = (
    "receipt_id,org_id,filer_type,filer_name,candidate_name,transaction_type,"
    "other_funds_type,receipt_date,amount,description,source_type,"
    "source_last_name,source_first_name,source_middle_name,source_suffix,"
    "address_1,address_2,city,state,zip,filed_date,amended,employer,"
    "occupation,source_name,rows_sharing_id,include_in_total,source_year,"
    "source_snapshot\n"
)


def _write_contributions(tmp_path, rows):
    path = tmp_path / "contributions.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


def test_home_address_never_leaves_the_index(tmp_path, monkeypatch):
    row = (
        "1,100,PAC-Separate Segregated Political Fund,Example PAC,,Monetary,,"
        "2025-01-01,50.0,,Individual,DOE,JANE,,,"
        "123 SECRET ST,APT 4,LINCOLN,NE,68508,2025-02-01,N,,,"
        "JANE DOE,1,True,2025,2025-09-01.csv\n"
    )
    monkeypatch.setattr(build_site, "CONTRIBUTIONS", _write_contributions(tmp_path, [row]))

    contributors, filers, rows_by_contributor = build_site.build_search_index()

    dumped = str(rows_by_contributor)
    assert "SECRET" not in dumped
    assert "APT 4" not in dumped
    # City/state ARE meant to survive -- only the street address is scrubbed.
    assert "LINCOLN" in dumped


def test_individual_vs_organization_kind(tmp_path, monkeypatch):
    rows = (
        "1,100,PAC,Example PAC,,Monetary,,2025-01-01,50.0,,Individual,DOE,"
        "JANE,,,1 ST,,LINCOLN,NE,68508,2025-02-01,N,,,JANE DOE,1,True,2025,"
        "s.csv\n",
        "2,100,PAC,Example PAC,,Monetary,,2025-01-02,500.0,,"
        "Business (For-Profit and Non-Profit entities),,,,,"
        "2 ST,,OMAHA,NE,68102,2025-02-02,N,,,ACME LLC,1,True,2025,s.csv\n",
    )
    monkeypatch.setattr(build_site, "CONTRIBUTIONS", _write_contributions(tmp_path, rows))

    contributors, filers, rows_by_contributor = build_site.build_search_index()

    by_name = {c[0]: c for c in contributors}
    assert by_name["JANE DOE"][3] == "individual"
    assert by_name["ACME LLC"][3] == "organization"


def test_excluded_rows_do_not_count_toward_the_total_but_are_still_listed(tmp_path, monkeypatch):
    """A row with include_in_total=False is a fanned-out repeat of another
    row's amount -- summing it would double-count. But dropping it from the
    per-contributor detail view would make a real filing invisible."""
    rows = (
        "1,100,PAC,Example PAC,,Monetary,,2025-01-01,50.0,,Individual,DOE,"
        "JANE,,,1 ST,,LINCOLN,NE,68508,2025-02-01,N,,,JANE DOE,2,True,2025,"
        "s.csv\n",
        "2,100,PAC,Example PAC,,Monetary,,2025-01-01,50.0,,Individual,DOE,"
        "JANE,,,1 ST,,LINCOLN,NE,68508,2025-02-01,N,,,JANE DOE,2,False,2025,"
        "s.csv\n",
    )
    monkeypatch.setattr(build_site, "CONTRIBUTIONS", _write_contributions(tmp_path, rows))

    contributors, filers, rows_by_contributor = build_site.build_search_index()

    by_name = {c[0]: c for c in contributors}
    assert by_name["JANE DOE"][1] == 50.0   # total: only the included row
    assert by_name["JANE DOE"][2] == 1      # count: only the included row
    assert len(rows_by_contributor["JANE DOE"]) == 2   # detail: both rows kept


def test_filer_totals_key_by_org_id_and_sum_receipts(tmp_path, monkeypatch):
    rows = (
        "1,100,PAC,Example PAC,,Monetary,,2025-01-01,50.0,,Individual,DOE,"
        "JANE,,,1 ST,,LINCOLN,NE,68508,2025-02-01,N,,,JANE DOE,1,True,2025,"
        "s.csv\n",
        "2,100,PAC,Example PAC,,Monetary,,2025-01-02,75.0,,Individual,ROE,"
        "RICK,,,2 ST,,OMAHA,NE,68102,2025-02-02,N,,,RICK ROE,1,True,2025,"
        "s.csv\n",
    )
    monkeypatch.setattr(build_site, "CONTRIBUTIONS", _write_contributions(tmp_path, rows))

    contributors, filers, rows_by_contributor = build_site.build_search_index()

    assert filers == [["Example PAC", "100", 125.0, 2]]


def test_modern_rows_are_tagged_era_modern(tmp_path, monkeypatch):
    row = (
        "1,100,PAC,Example PAC,,Monetary,,2025-01-01,50.0,,Individual,DOE,"
        "JANE,,,1 ST,,LINCOLN,NE,68508,2025-02-01,N,,,JANE DOE,1,True,2025,"
        "s.csv\n"
    )
    monkeypatch.setattr(build_site, "CONTRIBUTIONS", _write_contributions(tmp_path, [row]))

    _, _, rows_by_contributor = build_site.build_search_index()

    assert rows_by_contributor["JANE DOE"][0][-1] == "modern"


EXPENDITURE_HEADER = (
    "expenditure_id,org_id,filer_type,filer_name,candidate_name,transaction_type,"
    "sub_type,expenditure_date,amount,description,payee_type,payee_last_name,"
    "payee_first_name,payee_middle_name,payee_suffix,address_1,address_2,city,"
    "state,zip,filed_date,support_or_oppose,target_name,target_jurisdiction,"
    "amended,employer,occupation,principal_place_of_business,payee_name,"
    "rows_sharing_id,include_in_total,source_year,source_snapshot\n"
)


def _write_expenditures(tmp_path, *rows):
    path = tmp_path / "expenditures.csv"
    path.write_text(EXPENDITURE_HEADER + "".join(rows), encoding="utf-8")
    return path


def test_expenditures_grouped_by_filer_name_and_tagged_modern(tmp_path, monkeypatch):
    row = (
        "1,100,PAC,Example PAC,,Monetary,,2025-03-01,1200.0,Yard signs,"
        "Business (For-Profit and Non-Profit entities),,,,,1 ST,,LINCOLN,NE,"
        "68508,2025-04-01,,,,N,,,,Sign Shop LLC,1,True,2025,s.csv\n"
    )
    monkeypatch.setattr(build_site, "EXPENDITURES", _write_expenditures(tmp_path, row))

    index = build_site.build_expenditures_index()

    txn = index["Example PAC"][0]
    assert txn[1] == 1200.0        # amount
    assert txn[2] == "Sign Shop LLC"  # payee_name
    assert txn[-1] == "modern"     # era


def test_independent_expenditures_csv_never_read(tmp_path, monkeypatch):
    """PLAN.md's own table: independent_expenditures.csv is 'a subset of
    expenditures, not extra rows' -- reading it too would double-count."""
    assert not hasattr(build_site, "INDEPENDENT_EXPENDITURES")


def test_legacy_expenditures_tagged_pre2022(tmp_path, monkeypatch):
    legacy_header = EXPENDITURE_HEADER.strip("\n") + ",era,source_form\n"
    legacy_row = (
        "9,200,PAC,Old Committee,,Monetary,,2018-05-01,300.0,Mailers,"
        "Business (For-Profit and Non-Profit entities),,,,,9 ST,,LINCOLN,NE,"
        "68508,2018-06-01,,,,N,,,,Print Shop,9,True,2018,s.csv,pre2022,formb1d\n"
    )
    legacy_path = tmp_path / "expenditures_legacy.csv"
    legacy_path.write_text(legacy_header + legacy_row, encoding="utf-8")
    monkeypatch.setattr(build_site, "LEGACY_EXPENDITURES", legacy_path)

    index = build_site.build_expenditures_index()

    assert index["Old Committee"][0][-1] == "pre2022"


def test_legacy_rows_join_the_same_detail_list_tagged_pre2022(tmp_path, monkeypatch):
    """The whole point of ne-connect's ask: one name's transaction list spans
    both eras, even though their dollar totals are never summed (see
    LEGACY_CONTRIBUTIONS's own comment)."""
    modern_row = (
        "1,100,PAC,Example PAC,,Monetary,,2025-01-01,50.0,,Individual,DOE,"
        "JANE,,,1 ST,,LINCOLN,NE,68508,2025-02-01,N,,,JANE DOE,1,True,2025,"
        "s.csv\n"
    )
    legacy_header = (
        "receipt_id,org_id,filer_type,filer_name,candidate_name,transaction_type,"
        "other_funds_type,receipt_date,amount,description,source_type,"
        "source_last_name,source_first_name,source_middle_name,source_suffix,"
        "address_1,address_2,city,state,zip,filed_date,amended,employer,"
        "occupation,source_name,rows_sharing_id,include_in_total,source_year,"
        "source_snapshot,era,source_form\n"
    )
    legacy_row = (
        "9,200,PAC,Old Committee,,Monetary,,2018-05-01,25.0,,Individual,DOE,"
        "JANE,,,9 ST,,LINCOLN,NE,68508,2018-06-01,N,,,JANE DOE,9,True,2018,"
        "s.csv,pre2022,formb1ab\n"
    )
    monkeypatch.setattr(build_site, "CONTRIBUTIONS", _write_contributions(tmp_path, [modern_row]))
    legacy_path = tmp_path / "contributions_legacy.csv"
    legacy_path.write_text(legacy_header + legacy_row, encoding="utf-8")
    monkeypatch.setattr(build_site, "LEGACY_CONTRIBUTIONS", legacy_path)

    contributors, filers, rows_by_contributor = build_site.build_search_index()

    txns = rows_by_contributor["JANE DOE"]
    assert len(txns) == 2
    eras = {t[-1] for t in txns}
    assert eras == {"modern", "pre2022"}
    # The legacy row must never inflate the modern-only aggregate total.
    by_name = {c[0]: c for c in contributors}
    assert by_name["JANE DOE"][1] == 50.0
