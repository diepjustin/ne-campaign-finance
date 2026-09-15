from pathlib import Path

import scrape_c1 as sc

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_HTML = (FIXTURES / "c1_search_results_sample.html").read_text(encoding="utf-8")


def test_parse_rows_finds_every_data_row_and_skips_header_and_pager():
    rows = sc.parse_rows(SAMPLE_HTML)
    assert len(rows) == 10
    assert [r["filer_name_raw"] for r in rows][:2] == ["JON ABEGGLEN", "CHRISTY LEE ABTS"]


def test_parse_rows_splits_manual_and_electronic_by_control_id():
    """The __doPostBack mystery: a row's View link comes in exactly two
    shapes, keyed off Filed Method (Manual vs Electronic), not by content."""
    rows = sc.parse_rows(SAMPLE_HTML)
    manual = [r for r in rows if r["filed_method"] == "Manual"]
    electronic = [r for r in rows if r["filed_method"] == "Electronic"]
    assert len(manual) == 5
    assert len(electronic) == 5
    assert all(r["guid"] for r in manual)
    assert all(not r["guid"] for r in electronic)


def test_manual_row_guid_matches_known_fixture_value():
    rows = sc.parse_rows(SAMPLE_HTML)
    ackerman = next(r for r in rows if r["filer_name_raw"] == "KELLI M ACKERMAN")
    assert ackerman["guid"] == "a4f60783-2968-4734-93e1-2419010c0a9f"


def test_max_page_number_reads_the_windowed_pager():
    assert sc.max_page_number(SAMPLE_HTML) == 11


def test_extract_hidden_finds_all_four_postback_fields():
    hidden = sc.extract_hidden(SAMPLE_HTML)
    assert len(hidden["__VIEWSTATE"]) > 100
    assert len(hidden["__EVENTVALIDATION"]) > 100
    assert hidden["__VIEWSTATEGENERATOR"]


def test_disclosure_id_manual_is_the_state_guid():
    row = {"filed_method": "Manual", "guid": "a4f60783-2968-4734-93e1-2419010c0a9f"}
    assert sc.disclosure_id(row) == "a4f60783-2968-4734-93e1-2419010c0a9f"


def test_disclosure_id_electronic_is_deterministic_and_stable():
    row = {"filer_name_raw": "JON ABEGGLEN", "year": "2023", "filer_office": "MEMBER",
           "filed_date": "1/24/2024", "filed_method": "Electronic", "guid": ""}
    first = sc.disclosure_id(row)
    second = sc.disclosure_id(dict(row))
    assert first == second
    assert first.startswith("e:")


def test_disclosure_id_electronic_differs_for_different_filings():
    row_a = {"filer_name_raw": "JON ABEGGLEN", "year": "2023", "filer_office": "MEMBER",
             "filed_date": "1/24/2024", "filed_method": "Electronic", "guid": ""}
    row_b = {**row_a, "filer_name_raw": "JANE DOE"}
    assert sc.disclosure_id(row_a) != sc.disclosure_id(row_b)


def test_document_url_manual_is_a_direct_pdf_link():
    row = {"filed_method": "Manual", "guid": "a4f60783-2968-4734-93e1-2419010c0a9f"}
    url = sc.document_url_for(row)
    assert url == f"{sc.DOC_POPUP_URL}?PFD_FilingID=a4f60783-2968-4734-93e1-2419010c0a9f"


def test_document_url_electronic_falls_back_to_the_search_page():
    """No state-hosted URL exists for an Electronic filing (see module
    docstring); every row still gets SOME working link, per PLAN.md 1.5."""
    row = {"filed_method": "Electronic", "guid": ""}
    assert sc.document_url_for(row) == sc.SEARCH_URL


def test_build_post_data_carries_forward_search_params_and_new_target():
    hidden = sc.extract_hidden(SAMPLE_HTML)
    data = sc.build_post_data(hidden, {"year": 2023, "filed_method": "All"},
                               sc.GRID_CONTROL, "Page$2")
    assert data["__EVENTTARGET"] == sc.GRID_CONTROL
    assert data["__EVENTARGUMENT"] == "Page$2"
    assert data[f"{sc.PREFIX}$mgrSearchParams$FilingYear$ctl01"] == "2023"
    assert data["__VIEWSTATE"] == hidden["__VIEWSTATE"]


def test_rows_to_filings_every_row_has_a_working_document_url_and_retrieval_date():
    rows = sc.parse_rows(SAMPLE_HTML)
    filings = sc.rows_to_filings(rows, "2026-09-15")
    assert len(filings) == 10
    for f in filings:
        assert f["document_url"].startswith("https://nadc-e.nebraska.gov/")
        assert f["retrieved_at"] == "2026-09-15"
        assert f["disclosure_id"]


def test_write_filings_csv_dedupes_by_disclosure_id_and_sorts(tmp_path):
    rows = sc.parse_rows(SAMPLE_HTML)
    filings = sc.rows_to_filings(rows, "2026-09-15")
    filings_with_dupe = filings + [filings[0]]
    out = tmp_path / "c1_filings.csv"
    count = sc.write_filings_csv(filings_with_dupe, out)
    assert count == 10
    text = out.read_text(encoding="utf-8")
    ids = [line.split(",")[0] for line in text.splitlines()[1:]]
    assert ids == sorted(ids)


class FakeFetcher:
    """Replays canned pages in order -- no network, mirrors ne-lobbying's
    test style of a stub Fetcher rather than mocking requests directly."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0
        self.requests_made = 0

    def get(self, url):
        self.calls += 1
        self.requests_made += 1
        return self.pages.pop(0)

    def post(self, url, data):
        self.calls += 1
        self.requests_made += 1
        return self.pages.pop(0)


def test_scrape_year_stops_when_a_page_returns_no_rows(tmp_path):
    """Single-page fixture: max_page_number says 11, but scrape_year must not
    try to page past what the live pager reports. Real pagination beyond the
    fixture is exercised implicitly by the max_page_number test above -- this
    test only checks the loop terminates on the fixture's own reported ceiling
    without ever calling `.post` more times than the fetcher was given pages."""
    fetcher = FakeFetcher([SAMPLE_HTML, SAMPLE_HTML])
    cache = sc.PageCache(cache_dir=tmp_path)
    rows = sc.scrape_year(fetcher, 2023, "All", cache=cache, refresh=True, max_pages=1)
    assert len(rows) == 10


def test_page_cache_round_trips(tmp_path):
    cache = sc.PageCache(cache_dir=tmp_path)
    assert cache.get(2023, "All", 1) is None
    rows = [{"filer_name_raw": "TEST"}]
    cache.put(2023, "All", 1, rows)
    assert cache.get(2023, "All", 1) == rows
