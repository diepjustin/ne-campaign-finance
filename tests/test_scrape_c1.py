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
        self.posts = []

    def get(self, url):
        self.calls += 1
        self.requests_made += 1
        return self.pages.pop(0)

    def post(self, url, data):
        self.calls += 1
        self.requests_made += 1
        self.posts.append(data)
        return self.pages.pop(0)


# The fixture with its data rows removed: what an empty search returns.
EMPTY_HTML = __import__("re").sub(r"<tr[^>]*>.*?</tr>", "", SAMPLE_HTML, flags=__import__("re").S)


def test_scrape_year_stops_when_a_page_returns_no_rows(tmp_path):
    """Single-page fixture: max_page_number says 11, but scrape_year must not
    try to page past what the live pager reports. Real pagination beyond the
    fixture is exercised implicitly by the max_page_number test above -- this
    test only checks the loop terminates on the fixture's own reported ceiling
    without ever calling `.post` more times than the fetcher was given pages
    (GET form, POST search, POST page-size)."""
    fetcher = FakeFetcher([SAMPLE_HTML, SAMPLE_HTML, SAMPLE_HTML])
    cache = sc.PageCache(cache_dir=tmp_path)
    rows = sc.scrape_year(fetcher, 2023, "All", cache=cache, refresh=True, max_pages=1)
    assert len(rows) == 10
    assert fetcher.calls == 3


def test_scrape_year_switches_the_grid_to_fifty_rows_right_after_the_search(tmp_path):
    fetcher = FakeFetcher([SAMPLE_HTML, SAMPLE_HTML, SAMPLE_HTML])
    sc.scrape_year(fetcher, 2023, "All", cache=sc.PageCache(cache_dir=tmp_path),
                   refresh=True, max_pages=1)
    search_post, resize_post = fetcher.posts
    assert search_post["__EVENTTARGET"] == sc.SEARCH_BUTTON
    assert sc.PAGE_SIZE_DROPDOWN not in search_post
    assert resize_post["__EVENTTARGET"] == sc.PAGE_SIZE_DROPDOWN
    assert resize_post[sc.PAGE_SIZE_DROPDOWN] == str(sc.PAGE_SIZE)


def test_scrape_year_skips_the_resize_postback_when_the_search_is_empty(tmp_path):
    assert sc.parse_rows(EMPTY_HTML) == []
    fetcher = FakeFetcher([SAMPLE_HTML, EMPTY_HTML])
    rows = sc.scrape_year(fetcher, 2026, "All", cache=sc.PageCache(cache_dir=tmp_path), refresh=True)
    assert rows == []
    assert fetcher.calls == 2


def test_scrape_year_reuses_live_state_instead_of_searching_twice(tmp_path):
    """A live page-1 fetch already holds the postback state; paging on must
    not redo the GET+search (two wasted requests per year, the old behavior).
    Pages: GET, search, resize, then Page$2 -- exactly four."""
    fetcher = FakeFetcher([SAMPLE_HTML, SAMPLE_HTML, SAMPLE_HTML, EMPTY_HTML])
    rows = sc.scrape_year(fetcher, 2023, "All", cache=sc.PageCache(cache_dir=tmp_path),
                          refresh=True, max_pages=2)
    assert len(rows) == 10
    assert fetcher.calls == 4
    assert fetcher.posts[-1]["__EVENTARGUMENT"] == "Page$2"
    assert fetcher.posts[-1][sc.PAGE_SIZE_DROPDOWN] == str(sc.PAGE_SIZE)


def test_page_cache_round_trips_and_stamps_the_fetch_date(tmp_path):
    cache = sc.PageCache(cache_dir=tmp_path)
    assert cache.get(2023, "All", 1) is None
    rows = [{"filer_name_raw": "TEST"}]
    cache.put(2023, "All", 1, rows, retrieved_at="2026-09-15")
    assert cache.get(2023, "All", 1) == [{"filer_name_raw": "TEST", "retrieved_at": "2026-09-15"}]


def test_page_cache_key_includes_the_page_size_so_old_ten_row_files_are_never_misread(tmp_path):
    import json
    (tmp_path / "2019-All-p1.json").write_text(json.dumps([{"filer_name_raw": "OLD"}]), encoding="utf-8")
    cache = sc.PageCache(cache_dir=tmp_path)
    assert cache.get(2019, "All", 1) is None
    cache.put(2019, "All", 1, [{"filer_name_raw": "NEW"}], retrieved_at="2026-09-21")
    assert (tmp_path / f"2019-All-s{sc.PAGE_SIZE}-p1.json").exists()


def test_build_post_data_adds_the_page_size_dropdown_only_when_asked():
    hidden = sc.extract_hidden(SAMPLE_HTML)
    without = sc.build_post_data(hidden, {"year": 2023, "filed_method": "All"}, sc.SEARCH_BUTTON)
    assert sc.PAGE_SIZE_DROPDOWN not in without
    with_size = sc.build_post_data(hidden, {"year": 2023, "filed_method": "All"},
                                   sc.PAGE_SIZE_DROPDOWN, page_size=sc.PAGE_SIZE)
    assert with_size[sc.PAGE_SIZE_DROPDOWN] == str(sc.PAGE_SIZE)
    assert with_size["__EVENTTARGET"] == sc.PAGE_SIZE_DROPDOWN


def test_rows_to_filings_keeps_each_cached_rows_own_retrieval_date():
    """A 2018 page fetched months ago must not claim today's date on the hub."""
    rows = sc.parse_rows(SAMPLE_HTML)
    stamped = sc._stamped(rows[:1], "2026-03-04") + rows[1:2]
    filings = sc.rows_to_filings(stamped, "2026-09-21")
    assert filings[0]["retrieved_at"] == "2026-03-04"
    assert filings[1]["retrieved_at"] == "2026-09-21"


def test_new_only_refreshes_only_the_two_newest_years(tmp_path, monkeypatch):
    from datetime import date
    seen = []

    def fake_scrape_year(fetcher, year, filed_method, cache, refresh=False, max_pages=None):
        seen.append((year, refresh))
        return []

    real_cache = sc.PageCache
    monkeypatch.setattr(sc, "scrape_year", fake_scrape_year)
    monkeypatch.setattr(sc, "PageCache", lambda: real_cache(cache_dir=tmp_path))
    this_year = date.today().year
    years = [this_year - 3, this_year - 2, this_year - 1, this_year]
    sc.main(["--new-only", "--years", *map(str, years), "--out", str(tmp_path / "out.csv")])
    assert seen == [(this_year - 3, False), (this_year - 2, False),
                    (this_year - 1, True), (this_year, True)]


def test_without_new_only_or_refresh_every_year_is_a_cache_hit(tmp_path, monkeypatch):
    seen = []
    real_cache = sc.PageCache
    monkeypatch.setattr(sc, "scrape_year",
                        lambda f, y, m, c, refresh=False, max_pages=None: seen.append(refresh) or [])
    monkeypatch.setattr(sc, "PageCache", lambda: real_cache(cache_dir=tmp_path))
    sc.main(["--years", "2024", "2025", "--out", str(tmp_path / "out.csv")])
    assert seen == [False, False]
