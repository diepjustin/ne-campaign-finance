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
