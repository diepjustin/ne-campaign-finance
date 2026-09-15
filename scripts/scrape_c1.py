"""Scraper for NADC's C-1/C-2 Statement of Financial Interests search page.

Source: https://nadc-e.nebraska.gov/PublicSite/SearchPages/Search.aspx
        ?SearchTypeCodeHook=86C705B4-76BE-4FD9-B9A0-B607711F8A3A

Classic ASP.NET WebForms, not a JSON API: every search and every page turn is a
POST carrying __VIEWSTATE/__EVENTVALIDATION and an __EVENTTARGET, verified
2026-09-14 by replaying real form submissions with `requests` and cross-checking
against a live browser session. The Filing Year dropdown only goes back to
2018; 2018 through 2022-07-11 overlaps `nadc_data.zip`'s formc1.txt (see
download_legacy.py / normalize_legacy_c1.py), so this scraper writes a
`filing_year` column and leaves the freeze-date dedup to the caller rather than
guessing it here -- a filing dated after 2022-07-11 in a `filing_year=2022`
search is real and belongs to the online era.

THE __doPostBack MYSTERY, SOLVED (recon 2026-09-14): every row's "View" link
looks identical in the anchor text, but the control id it is attached to comes
in exactly two flavours, and they are not interchangeable:

    colActions_sub_actManualView0N  ->  href is a plain
        javascript:Configurable.handlePopup('../Reporting/DocumentImagePopup.aspx
        ?PFD_FilingID=<guid>') -- a real, stable, unauthenticated GET URL to a
        PDF the state already has sitting on disk (a scanned paper filing).

    colActions_sub_actView0N  ->  href is __doPostBack(<that same control id>, '').
        Filtering the live search page to Filed Method=Electronic (confirmed
        live 2026-09-14) shows EVERY row on such a filter uses this control --
        so the split is exactly Manual (scanned, static file) vs Electronic
        (typed, no static file). Clicking it in a real browser proved why: the
        server does a full, synchronous postback (not the async/UpdatePanel
        partial postback `X-MicrosoftAjax: Delta=true` this project's Fetcher
        pattern would normally reach for) and re-renders the ENTIRE search page
        with a Crystal-Reports-style rendered C-1, embedded inline as
        `<iframe src="data:application/pdf;base64,...">` inside a
        `<div class="pdfReportWindow">`. There is no server-side URL for this
        PDF at all -- it is generated fresh on every postback and only ever
        exists as bytes in that one response. `resolve_electronic_pdf()` below
        replays that exact postback (full form-field replay, not the async
        variant, which 500s -- confirmed) and decodes the data: URI.

Because an electronic filing has no stable state-hosted URL, `document_url` in
`c1_filings.csv` for these rows points at the search page itself (the same
"stopgap door" pattern PLAN.md's 0.7 already established for campaign-finance
entities before their own page existed) rather than a fabricated per-filing
link. `filed_method` records which kind a row is, so a caller can tell "here
is the PDF" from "search NADC for this name and year" apart. The PDF pipeline
(build_financial_interests.py) resolves electronic filings on demand by
replaying the search for that filer/year and triggering the matching row's
postback -- an expensive per-filing operation, which is exactly why it is not
done here.

Guard rails, same spirit as ne-lobbying/scripts/lobby.py's Fetcher: one request
every DEFAULT_DELAY seconds, retry-with-backoff on 429/5xx and on a dropped
connection, a descriptive User-Agent with a contact address. Unlike lobby.py,
raw responses are NOT cached by request bytes -- ASP.NET embeds a fresh
__VIEWSTATE in every single response, so two fetches of "the same" page are
never byte-identical and a content-addressed cache would never hit. Instead,
parsed *rows* are cached per (year, filed_method, page) under
data/raw/c1_search_cache/, which is the level at which two fetches really are
the same answer.
"""

from __future__ import annotations

import argparse
import csv
import html as html_lib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import requests

BASE = "https://nadc-e.nebraska.gov/PublicSite"
SEARCH_TYPE_HOOK = "86C705B4-76BE-4FD9-B9A0-B607711F8A3A"
SEARCH_URL = f"{BASE}/SearchPages/Search.aspx?SearchTypeCodeHook={SEARCH_TYPE_HOOK}"
DOC_POPUP_URL = f"{BASE}/Reporting/DocumentImagePopup.aspx"

# The Configurable-framework control tree, transcribed from a live page pull.
# Lower-cased guid: confirmed the control ids use the hook's lowercase form.
PREFIX = f"ctl00$Content${SEARCH_TYPE_HOOK.lower()}$mgrPFDSearch"
GRID_CONTROL = f"{PREFIX}$mgrSearchResults$mgrReportViewer$grdPFD$ctl01"
SEARCH_BUTTON = f"{PREFIX}$btnSearch$ctl01"

USER_AGENT = (
    "ne-campaign-finance-scraper/0.1 "
    "(https://github.com/diepjustin/diepjustin.github.io; contact: sdiepxj367@gmail.com)"
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROCESSED_DIR = DATA_DIR / "processed"
PAGE_CACHE_DIR = DATA_DIR / "raw" / "c1_search_cache"

DEFAULT_DELAY = 2.0
MAX_RETRIES = 4

# Real number seen live for a Nebraska filing year is in the low thousands of
# filers; this is a safety stop so a pager that never terminates (a parsing
# bug, not a real dataset) cannot loop forever, mirroring lobby.py's
# MAX_PAGES_PER_BILL.
MAX_PAGES_PER_YEAR = 2000

FILING_YEARS = list(range(2018, date.today().year + 1))

_TAG = re.compile(r"<[^>]+>")
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_MANUAL_LINK = re.compile(
    r"Configurable\.handlePopup\(&#39;\.\./Reporting/DocumentImagePopup\.aspx"
    r"\?PFD_FilingID=([0-9a-fA-F-]{36})&#39;\)"
)
_POSTBACK_TARGET = re.compile(
    r"__doPostBack\(&#39;([^&]*colActions_sub_actView\d+)&#39;,\s*&#39;&#39;\)"
)
_PAGE_LINK = re.compile(r"__doPostBack\(&#39;[^&]*grdPFD\$ctl01&#39;,\s*&#39;Page\$(\d+)&#39;\)")
_HIDDEN = re.compile(r'id="(__VIEWSTATE|__VIEWSTATEGENERATOR|__EVENTVALIDATION|'
                      r'ctl00_ToolkitScriptManager1_HiddenField)" value="([^"]*)"')
_PDF_DATA_URI = re.compile(r"<iframe src=['\"](data:application/pdf;base64,[^'\"]+)['\"]")


def _clean(fragment: str) -> str:
    return html_lib.unescape(_TAG.sub(" ", fragment)).strip()


class RateLimited(Exception):
    """The server (or an ASP.NET session hiccup) asked us to stop."""


class Unreachable(RateLimited):
    """The network gave out. Same clean-stop treatment as a 429 -- see lobby.py."""


@dataclass
class Fetcher:
    """Polite, retrying HTTP for a stateful ASP.NET WebForms session.

    Deliberately does not cache raw responses (see module docstring): every
    response carries a unique __VIEWSTATE, so a byte-content cache would never
    hit. Callers that want to skip re-fetching identical *results* use
    PageCache instead.
    """

    delay: float = DEFAULT_DELAY
    session: requests.Session = field(default_factory=requests.Session)
    requests_made: int = 0
    rate_limit_waits: int = 0
    network_retries: int = 0

    def __post_init__(self):
        self.session.headers["User-Agent"] = USER_AGENT

    def get(self, url: str) -> str:
        return self._fetch(url, data=None)

    def post(self, url: str, data: dict) -> str:
        return self._fetch(url, data=data)

    def _fetch(self, url: str, data=None) -> str:
        if self.requests_made:
            time.sleep(self.delay)

        unreachable = None
        for attempt in range(MAX_RETRIES):
            try:
                response = (
                    self.session.post(url, data=data, timeout=45)
                    if data is not None
                    else self.session.get(url, timeout=45)
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                unreachable = exc
                wait = self.delay * (2 ** (attempt + 1))
                self.network_retries += 1
                print(f"    {type(exc).__name__} -- waiting {wait:.0f}s (attempt {attempt + 1})",
                      file=sys.stderr)
                time.sleep(wait)
                continue

            unreachable = None
            if response.status_code != 429:
                response.raise_for_status()
                self.requests_made += 1
                return response.text

            wait = float(response.headers.get("Retry-After") or self.delay * (2 ** (attempt + 1)))
            self.rate_limit_waits += 1
            print(f"    429 -- waiting {wait:.0f}s (attempt {attempt + 1})", file=sys.stderr)
            time.sleep(wait)

        if unreachable is not None:
            raise Unreachable(
                f"still unreachable after {MAX_RETRIES} attempts "
                f"({type(unreachable).__name__}): {url}"
            ) from unreachable
        raise RateLimited(f"still rate limited after {MAX_RETRIES} attempts: {url}")


def extract_hidden(page: str) -> dict:
    """The four ASP.NET postback fields every subsequent request must replay."""
    hidden = {name: "" for name in
              ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
               "ctl00_ToolkitScriptManager1_HiddenField")}
    for name, value in _HIDDEN.findall(page):
        hidden[name] = html_lib.unescape(value)
    return hidden


def build_post_data(hidden: dict, search_params: dict, event_target: str,
                     event_argument: str = "") -> dict:
    """A full ASP.NET form submission: the real form always resubmits every
    field currently in the DOM, not just the one that changed -- confirmed by
    replaying a live postback (a partial/async body 500s; this shape works)."""
    return {
        "ctl00_ToolkitScriptManager1_HiddenField": hidden["ctl00_ToolkitScriptManager1_HiddenField"],
        "__EVENTTARGET": event_target,
        "__EVENTARGUMENT": event_argument,
        "__LASTFOCUS": "",
        "__VIEWSTATE": hidden["__VIEWSTATE"],
        "__VIEWSTATEGENERATOR": hidden["__VIEWSTATEGENERATOR"],
        f"{PREFIX}$mgrSearchParams$LastName$ctl01": search_params.get("last_name", ""),
        f"{PREFIX}$mgrSearchParams$FilingYear$ctl01": str(search_params["year"]),
        f"{PREFIX}$mgrSearchParams$OfficeHeld$ctl01": search_params.get("office_held", ""),
        f"{PREFIX}$mgrSearchParams$OfficeSought$ctl01": search_params.get("office_sought", ""),
        f"{PREFIX}$mgrSearchParams$FiledMethod$ctl01": search_params.get("filed_method", "All"),
        "__EVENTVALIDATION": hidden["__EVENTVALIDATION"],
    }


def parse_rows(page: str) -> list[dict]:
    """Grid rows -> filing dicts. Header and pager rows are skipped by shape,
    not by isolating the grid table first (fragile against markup drift) --
    real data rows are exactly 7 <td> cells with an Actions cell that matches
    one of the two known View-link shapes; header cells are <th>, and the
    pager sits in a single colspan="7" <td> with no such match.
    """
    rows = []
    for row_html in _ROW.findall(page):
        cells = _CELL.findall(row_html)
        if len(cells) != 7:
            continue
        filer_name_raw = _clean(cells[0])
        if not filer_name_raw:
            continue
        actions = cells[6]
        manual = _MANUAL_LINK.search(actions)
        postback = _POSTBACK_TARGET.search(actions)
        if not manual and not postback:
            continue

        row = {
            "filer_name_raw": filer_name_raw,
            "year": _clean(cells[1]),
            "filing_reason": _clean(cells[2]),
            "filer_office": _clean(cells[3]),
            "office_sought": _clean(cells[4]),
            "filed_date": _clean(cells[5]),
        }
        if manual:
            row["filed_method"] = "Manual"
            row["guid"] = manual.group(1)
        else:
            row["filed_method"] = "Electronic"
            row["guid"] = ""
        rows.append(row)
    return rows


def max_page_number(page: str) -> int:
    pages = [int(n) for n in _PAGE_LINK.findall(page)]
    return max(pages) if pages else 1


def disclosure_id(row: dict) -> str:
    """The natural key PLAN.md's dedup table calls for.

    A Manual filing's guid IS the state's own identifier -- stable forever.
    An Electronic filing has no id anywhere in the grid (recon confirmed: the
    postback control id like "ctl02" is a page position, not an identity --
    it is reused by whatever row sorts into that slot and is not safe to
    persist). So its key is a deterministic hash of the fields that actually
    identify one filing: nothing about this filer/year/office/date combination
    is expected to repeat, and if the state ever lets someone file the same
    report twice, that is a real duplicate worth seeing, not one to hide.
    """
    if row["filed_method"] == "Manual" and row.get("guid"):
        return row["guid"]
    import hashlib
    key = "|".join([row["filer_name_raw"], row["year"], row["filer_office"], row["filed_date"]])
    return "e:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def document_url_for(row: dict) -> str:
    """A working link for every row -- see the module docstring's mystery
    writeup for why Electronic rows point at the search page rather than a
    fabricated per-filing URL: the state genuinely does not host one."""
    if row["filed_method"] == "Manual" and row.get("guid"):
        return f"{DOC_POPUP_URL}?PFD_FilingID={row['guid']}"
    return SEARCH_URL


class PageCache:
    """Parsed-rows cache keyed by (year, filed_method, page) -- see module
    docstring for why this sits above Fetcher rather than inside it."""

    def __init__(self, cache_dir: Path = None):
        self.cache_dir = Path(cache_dir or PAGE_CACHE_DIR)

    def _path(self, year: int, filed_method: str, page: int) -> Path:
        return self.cache_dir / f"{year}-{filed_method}-p{page}.json"

    def get(self, year: int, filed_method: str, page: int):
        path = self._path(year, filed_method, page)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def put(self, year: int, filed_method: str, page: int, rows: list[dict]):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._path(year, filed_method, page).write_text(
            json.dumps(rows, indent=2), encoding="utf-8"
        )


def scrape_year(fetcher: Fetcher, year: int, filed_method: str = "All",
                cache: PageCache = None, refresh: bool = False,
                max_pages: int = None) -> list[dict]:
    """Every C-1/C-2 filing indexed for one Filing Year. Resumable at the page
    level via PageCache; a run interrupted mid-year can be rerun and will only
    refetch pages it never finished."""
    search_params = {"year": year, "filed_method": filed_method}
    cache = cache or PageCache()
    ceiling = max_pages or MAX_PAGES_PER_YEAR

    cached_first = None if refresh else cache.get(year, filed_method, 1)
    if cached_first is not None:
        rows = list(cached_first)
    else:
        page0 = fetcher.get(SEARCH_URL)
        hidden = extract_hidden(page0)
        data = build_post_data(hidden, search_params, SEARCH_BUTTON)
        page1 = fetcher.post(SEARCH_URL, data)
        hidden = extract_hidden(page1)
        rows = parse_rows(page1)
        cache.put(year, filed_method, 1, rows)
        _last_page_html = page1

    if not rows:
        return []

    # Need hidden state to keep paging even on a cache hit for page 1 -- so a
    # fresh GET+search is required whenever any later page isn't cached either.
    all_rows = list(rows)
    page_number = 1
    last_page = 1
    hidden = None

    def ensure_live_state():
        nonlocal hidden, last_page
        if hidden is not None:
            return
        page0 = fetcher.get(SEARCH_URL)
        hidden = extract_hidden(page0)
        data = build_post_data(hidden, search_params, SEARCH_BUTTON)
        page1 = fetcher.post(SEARCH_URL, data)
        hidden = extract_hidden(page1)
        last_page = max_page_number(page1)

    while page_number < ceiling:
        cached_next = None if refresh else cache.get(year, filed_method, page_number + 1)
        if cached_next is not None:
            if not cached_next:
                break
            all_rows.extend(cached_next)
            page_number += 1
            continue

        ensure_live_state()
        if page_number >= last_page:
            break
        page_number += 1
        data = build_post_data(hidden, search_params, GRID_CONTROL, f"Page${page_number}")
        page_html = fetcher.post(SEARCH_URL, data)
        hidden = extract_hidden(page_html)
        last_page = max(last_page, max_page_number(page_html))
        new_rows = parse_rows(page_html)
        cache.put(year, filed_method, page_number, new_rows)
        if not new_rows:
            break
        all_rows.extend(new_rows)

    return all_rows


def resolve_electronic_pdf(fetcher: Fetcher, year: int, filer_name_raw: str,
                            filer_office: str, filed_date: str) -> bytes | None:
    """Replay the search for one Electronic filing and trigger its postback to
    get the state-rendered PDF bytes. Expensive (2+ requests per filing), so
    only the PDF pipeline calls this, never the index build.

    Matches by last name (server-side filter, cheap) then by the same fields
    that make up its synthetic disclosure_id, so this always resolves the
    filing the caller actually asked for even if the last-name filter returns
    several rows.
    """
    last_name = filer_name_raw.strip().split()[-1] if filer_name_raw.strip() else ""
    search_params = {"year": year, "filed_method": "Electronic", "last_name": last_name}
    page0 = fetcher.get(SEARCH_URL)
    hidden = extract_hidden(page0)
    data = build_post_data(hidden, search_params, SEARCH_BUTTON)
    page1 = fetcher.post(SEARCH_URL, data)
    hidden = extract_hidden(page1)

    target = None
    for row_html in _ROW.findall(page1):
        cells = _CELL.findall(row_html)
        if len(cells) != 7:
            continue
        if (_clean(cells[0]) != filer_name_raw
                or _clean(cells[3]) != filer_office
                or _clean(cells[5]) != filed_date):
            continue
        m = _POSTBACK_TARGET.search(cells[6])
        if m:
            target = m.group(1)
            break
    if target is None:
        return None

    view_data = build_post_data(hidden, search_params, target)
    view_page = fetcher.post(SEARCH_URL, view_data)
    m = _PDF_DATA_URI.search(view_page)
    if not m:
        return None
    import base64
    b64 = m.group(1).split(",", 1)[1]
    return base64.b64decode(b64)


# disclosure_id, year, filer_name_raw, filer_office, document_url, retrieved_at
# are the columns PLAN.md 1.5 specifies. filed_method, filing_reason and
# filed_date are kept alongside them: filed_method/filed_date are what
# build_financial_interests.py needs to re-resolve an Electronic filing's PDF
# (see scrape_c1.resolve_electronic_pdf and its module-docstring writeup of
# why Electronic rows have no static document_url of their own), and
# filing_reason is free from the same grid row at zero extra cost.
FIELDNAMES = ["disclosure_id", "year", "filer_name_raw", "filer_office",
              "filed_method", "filing_reason", "filed_date", "document_url",
              "retrieved_at"]


def rows_to_filings(rows: list[dict], retrieved_at: str) -> list[dict]:
    out = []
    for row in rows:
        out.append({
            "disclosure_id": disclosure_id(row),
            "year": row["year"],
            "filer_name_raw": row["filer_name_raw"],
            "filer_office": row["filer_office"],
            "filed_method": row["filed_method"],
            "filing_reason": row["filing_reason"],
            "filed_date": row["filed_date"],
            "document_url": document_url_for(row),
            "retrieved_at": retrieved_at,
        })
    return out


def write_filings_csv(filings: list[dict], path: Path):
    """Full rewrite, sorted by disclosure_id -- PLAN.md's dedup table calls
    C-1 a 'rewrite from raw captures' source, same as the legacy tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    deduped = {f["disclosure_id"]: f for f in filings}
    ordered = [deduped[k] for k in sorted(deduped)]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(ordered)
    return len(ordered)


def main():
    parser = argparse.ArgumentParser(description="Scrape NADC's C-1/C-2 search grid.")
    parser.add_argument("--years", type=int, nargs="+", default=None,
                         help="filing years to scrape (default: 2018..this year)")
    parser.add_argument("--filed-method", choices=("All", "Electronic", "Manual"), default="All")
    parser.add_argument("--max-pages", type=int, default=None,
                         help="stop each year after this many result pages (for sampling)")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--refresh", action="store_true", help="ignore the page cache")
    parser.add_argument("--out", type=Path, default=PROCESSED_DIR / "c1_filings.csv")
    args = parser.parse_args()

    years = args.years or FILING_YEARS
    fetcher = Fetcher(delay=args.delay)
    cache = PageCache()
    all_rows = []
    for year in years:
        print(f"Filing Year {year} ({args.filed_method})...")
        try:
            rows = scrape_year(fetcher, year, args.filed_method, cache,
                                refresh=args.refresh, max_pages=args.max_pages)
        except RateLimited as exc:
            print(f"  stopping politely -- {exc}", file=sys.stderr)
            break
        print(f"  {len(rows)} filings")
        all_rows.extend(rows)

    retrieved_at = date.today().isoformat()
    filings = rows_to_filings(all_rows, retrieved_at)
    count = write_filings_csv(filings, args.out)
    print(f"\nWrote {count} filings to {args.out}")
    print(f"requests made: {fetcher.requests_made}, rate-limit waits: {fetcher.rate_limit_waits}, "
          f"network retries: {fetcher.network_retries}")


if __name__ == "__main__":
    main()
