# Nebraska Campaign Finance

**Status: build phases 1–2 done.** Both bulk extracts download, validate and normalize
across 2022–2026: **207,259 raw rows → 116,982 contributions, 549 loans, 2,780 other
receipts, 86,948 expenditures, of which 5,928 are independent expenditures.** 43 tests.
Phases 3, 5 and 6 (the registry, the site, automation) are still plan. **Phase 4 is
mostly gone** — independent expenditures turned out to be in the bulk extract, not
behind the search UI.

A journalism and accountability tool for collecting and publishing Nebraska campaign
finance records — contributions, loans, expenditures, independent expenditures, and the
committee/candidate registry — from the **FirstTuesday® Campaign Finance System** run by
the [Nebraska Accountability and Disclosure Commission](https://nadc.nebraska.gov/campaign-finance-general-information)
(NADC) at [`nadc-e.nebraska.gov`](https://nadc-e.nebraska.gov/PublicSite/).

Modeled on CalMatters' [`powersearch-download`](https://github.com/CalMatters/powersearch-download):
build a query, fetch, **error out loudly if the data looks wrong**, write it unmodified.
Then, like the sibling `ne-contracts/` project, build a static searchable site from what
was collected, published on GitHub Pages at `diepjustin.github.io/ne-campaign-finance/`.

Same house rules as `ne-contracts/`: plain Python (`requests` + `beautifulsoup4`), no
framework, `index.html` at the folder root because that root is the published URL, raw
data stays out of git, README is the single source of truth.

| | |
|---|---|
| [Scope](#scope) | what's in, what's out |
| [Where the data comes from](#where-the-data-comes-from) | two acquisition tiers |
| [Caveats worth knowing before you quote it](#caveats-worth-knowing-before-you-quote-it) | amendments, snapshots, thresholds |
| [Running it](#running-it) | install, test, download |
| [Architecture](#architecture) | files, schema, build |
| [Guard rails](#guard-rails) | read before changing anything that writes |
| [Build phases](#build-phases) | the plan |
| [Open questions](#open-questions) | decisions still to make |

---

## Scope

**In:**

- **Contributions & loans** — every itemized receipt: monetary, in-kind, loan, refund,
  pledge, etc. (`ContributionLoanExtract`).
- **Expenditures** — committee spending (`ExpenditureExtract`).
- **Independent expenditures** — spending for/against candidates by people and groups
  that aren't the candidate's committee. **In the bulk expenditures extract**, as a
  transaction type plus three dedicated columns (support/oppose, target, jurisdiction).
  An earlier draft of this file said they weren't and would need scraping; that was
  inferred from the download page listing only two datasets, and it was wrong.
- **Committee / candidate / filer registry** — the entity master list (names, IDs,
  addresses, candidate↔committee links, filing history) that everything else joins to.
  *Not in the bulk extracts* — see tier 2 below. The extracts carry `Org ID` and
  `Filer Name`, so a provisional entity list is derivable; the registry adds the rest.

**Out:**

- **Pre-2022 data.** The legacy NADC system (a ~63-file pipe-delimited dump at
  `nebraska.gov/nadc_data/nadc_data.zip`, paper-record data entry, frozen since the 2022
  cutover) is a different schema with different IDs. Deliberately excluded to keep one
  clean model. If it's ever wanted, the Omaha World-Herald already wrote a parser:
  [`OWH-projects/nadc_data`](https://github.com/OWH-projects/nadc_data).
- Personal financial disclosures / statements of financial interest (C-1), lobbying
  reports. Different domain.

## Where the data comes from

### Tier 1 — bulk CSV extracts (easy, do first)

The FirstTuesday [Download Data](https://nadc-e.nebraska.gov/PublicSite/DataDownload.aspx)
page publishes yearly zipped CSVs at stable, unauthenticated URLs:

```
https://nadc-e.nebraska.gov/PublicSite/Docs/BulkDataDownloads/{YEAR}_ContributionLoanExtract.csv.zip
https://nadc-e.nebraska.gov/PublicSite/Docs/BulkDataDownloads/{YEAR}_ExpenditureExtract.csv.zip
```

`{YEAR}` ∈ 2022 … current year. No POST, no `__VIEWSTATE` — plain `GET`. The page shows
an "as of" timestamp and refreshes nightly.

Field layouts are documented:
[Contributions/Loans key (PDF)](https://nadc-e.nebraska.gov/PublicSite/Resources/PublicDocuments/NEContributionsFileLayout.pdf) ·
[Expenditures key (PDF)](https://nadc-e.nebraska.gov/PublicSite/Resources/PublicDocuments/NEExpendituresFileLayout.pdf).

Contributions/loans extract — 24 columns:

| Col | Field | Notes |
|---|---|---|
| A | Receipt ID | unique per receipt/loan |
| B | Org ID | entity **receiving** the money — the join key to the registry |
| C | Filer Type | committee type |
| D | Filer Name | |
| E | Candidate Name | when the recipient is a candidate committee |
| F | Receipt/Contribution Type | Monetary, In-Kind, Loan, Refund, Loan Forgiveness, Other Funds, Pledge, Pledge Payment, Debt Forgiveness, Payment Received for Loan Made, Pledge Made Forgiveness |
| G | Other Funds Type | only for "Other Funds Received" |
| H | Receipt Date | |
| I | Receipt Amount | |
| J | Description | |
| K | Contributor/Source Type | individual, business, committee, … |
| L–O | Last / First / Middle / Suffix | L holds the full name for non-individuals |
| P–T | Address 1 / Address 2 / City / State / Zip | |
| U | Filed Date | |
| V | Amended (Y/N) | **flag only — no link to the prior version** |
| W–X | Employer / Occupation | individuals only, when provided |

### Tier 2 — PublicSite search exports (ASP.NET WebForms, do second)

Independent expenditures and the entity registry aren't in tier 1. They live in the
FirstTuesday [PublicSite search](https://nadc-e.nebraska.gov/PublicSite/Search.aspx),
which the NADC says can export "results from any search." Search types:

| Search | URL (`…/PublicSite/SearchPages/Search.aspx?SearchTypeCodeHook=`) |
|---|---|
| Candidates | `1F26BA5E-71EA-48E4-8D50-C1013E9FE0A7` |
| Committees / Businesses / Others | `4E059E51-A3C3-45F5-A1BC-EA50C2AF9973` |
| Contributions | `F0FEA582-08C3-42BA-B008-0F5067C5791B` |
| Expenditures | `16D3B0E7-88E1-4105-AF0D-F241E7724D6B` |
| Documents (by form type) | `9C7E5A24-7B3D-41D8-8DE2-0AB873E42933` |
| Political Race | `PoliticalRaceSearch.aspx` |
| Statements of Financial Interest (C-1) | `86C705B4-76BE-4FD9-B9A0-B607711F8A3A` |

Entity detail pages are `OrganizationDetail.aspx?OrganizationID={n}` — small integers,
enumerable, with full filing history per entity.

**Recon needed before building tier 2.** WebForms grids are usually driven by
`__doPostBack` with `__VIEWSTATE` / `__EVENTVALIDATION` round-trips, but many of these
vendor portals also expose an `.ashx` / JSON endpoint the grid calls, which is far
easier to hit. First implementation step is a browser-devtools session on the
Committees and Expenditures searches: watch the network tab on search + export + page,
and decide (a) JSON endpoint if one exists, else (b) drive the viewstate + "Export"
button. **Independent expenditures**: confirm how they surface — likely the Expenditure
or Documents search filtered by form type (B-6 / B-9 / independent-spender filings),
not a tab of their own.

## Caveats worth knowing before you quote it

The tool reproduces the state's records faithfully, including their errors.

- **Amendments overwrite; there is no history.** Column V is a bare `Amended Y/N` flag.
  When a committee amends a report, the corrected transactions replace the originals in
  place. Two pulls taken on different dates can disagree with no audit trail. The tool
  archives every dated raw pull so *we* keep the history the state doesn't.
- **Each extract is a snapshot, not an append-only ledger.** Late-entered and corrected
  transactions appear retroactively in prior years. Re-pull **every** year each run; a
  year is never "final."
- **Only itemized transactions appear.** Contributions below the itemization threshold
  (roughly $250 aggregate per contributor per year) are reported only as lump sums on a
  committee's summary page, not as line items. Totals built from line items will
  undercount small money.
- **Coverage is state and local filers who use FirstTuesday.** Federal candidates file
  with the FEC, not here.
- **Summing the amount column over-counts. Filter on `include_in_total`.** The state
  fans one transaction out over several rows that share an ID, repeating the amount on
  each: an independent expenditure against five candidates is five rows, and a
  contribution to a slate committee is one row per candidate it supports. `normalize.py`
  flags exactly one row of each repeated-amount group. It's a small effect that hides in
  aggregate and distorts specifics — 2022 contributions are $68,865,363, not
  $68,955,221; 2026 expenditures $56,147,281, not $56,186,294.
- **Neither ID is a row key, though both layout PDFs say "unique."** `Receipt ID` repeats
  in 2022 (37 IDs) and 2023 (7) but not at all in 2024–2026, so a single recent year is
  not enough to learn this from. `Expenditure ID` repeats in every year.
- **A "Pledge" is a promise, not money.** It's kept out of `contributions.csv` on
  purpose; the cash arrives later as a separate "Pledge Payment Received" row. Counting
  both counts the same dollar twice.
- **Loans are not contributions.** A candidate's $50,000 self-loan is borrowed money, not
  $50,000 of support, so loans get their own table.
- **`independent_expenditures.csv` is a subset of `expenditures.csv`, not extra rows.**
  Adding the two together double-counts.
- **The layout PDFs are wrong about names, and type names drift between years.** The
  expenditures PDF misstates three column headers. Transaction-type values don't match
  the PDFs at all (the PDF's "In-Kind" is really "In-Kind Contribution"), and the state
  renamed one type mid-corpus — "Dissolution Surplus Funds Transfer" became
  "...Transfer and Returns" in 2024. So headers are a hard failure but type values are
  classified-and-reported: an unrecognized type is kept, never dropped, and named at the
  end of the run.
- **The extract isn't UTF-8.** A real 2026 pull has a Windows-1252 smart quote
  (`0x91`), typical of an ASP.NET/SQL Server export. `download_extracts.py` tries
  UTF-8 first and falls back to `cp1252` rather than crashing on filer-entered text.
- **`Org ID` identifies the recipient, not the contributor.** Contributor identity is
  free-text name + address; there is no contributor ID. Any "top donors" analysis needs
  entity resolution (the same problem `ne-contracts/` solves for vendors with
  `suggest_vendor_groups.py`).

## Running it

```
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python -m pytest tests/            # no network, run this first
./venv/bin/python scripts/download_extracts.py             # both extracts, all years
./venv/bin/python scripts/download_extracts.py --years 2026 --datasets expenditures --force
./venv/bin/python scripts/normalize.py                     # -> data/processed/
```

Raw pulls land in `data/raw/{dataset}/{year}/{run-date}.csv` (gitignored), with
provenance in `data/scrape_meta.json` (tracked — small, and it's the receipt for what
was pulled and when, same reasoning as `ne-contracts/data/scrape_meta.json`).
`normalize.py` reads the newest snapshot per year and writes the five canonical tables
plus `summary.json` to `data/processed/` (gitignored).

### The canonical tables

| File | Rows (2022–2026) | What it is |
|---|---:|---|
| `contributions.csv` | 116,982 | money and value received — monetary, in-kind, earmarked, pledge payments |
| `loans.csv` | 549 | receipt-side loan activity, kept separate from contributions |
| `other_receipts.csv` | 2,780 | interest, cash adjustments, anonymous cash, debt forgiveness, bare pledges |
| `expenditures.csv` | 86,948 | every row of the expenditures extract |
| `independent_expenditures.csv` | 5,928 | **a subset** of the above: type = Independent Expenditure |

The three receipt tables partition the contributions extract exactly — every raw row
lands in one of them, none twice. Columns are snake_case, dates ISO, amounts floats,
and every row carries `source_year` and `source_snapshot` so a published number traces
back to a specific pull.

## Architecture

Mirrors `ne-contracts/`.

```
ne-campaign-finance/
  README.md                 # this file — source of truth
  requirements.txt          # requests, beautifulsoup4, pytest (pinned, like ne-contracts)
  index.html                # the published single-page site (built, committed)
  scripts/
    download_extracts.py    # DONE  tier 1: fetch {year}_{dataset}.csv.zip, verify, cache
    validate.py             # DONE  the CalMatters-style integrity gate (details below)
    normalize.py            # DONE  raw CSVs -> the five canonical tables
    scrape_registry.py      # phase 3, tier 2: committees + candidates search export
    build_search_index.py   # canonical tables -> chunked JSON for the site
    build_site.py           # -> index.html
    serve_site.py           # local preview
    ne_format.py            # shared formatting helpers (copy pattern from ne-contracts)
  data/                     # gitignored except a manifest
    raw/                    # cached zips + CSVs, keyed by source + "as of" date
    processed/              # canonical tables (parquet or csv)
    scrape_meta.json        # last "as of" per source, row counts, run timestamps
  tests/
    fixtures/               # tiny real slices of each format
    test_*.py               # no network, no data/ — runs in CI before any scrape
```

**`validate.py` — errors out, never silently ships.** Hard failures:

- header doesn't match the confirmed layout, exactly — a renamed, dropped or added
  column stops the run (the state changing the schema is the likeliest silent breakage,
  and it already bit us: the expenditures PDF misnames three columns)
- a required field (ID, filer, date, amount) falls below 98% non-null
- any two rows are identical in every field
- zero data rows
- row count drops more than 20% vs. the previous run, without `--allow-shrink`

Reported but **not** failures, because they're normal here: repeated transaction IDs,
unparseable dates or amounts, and transaction types outside the known set.

Still open: reconciling per-`Org ID` totals against that committee's FirstTuesday
summary page, which would catch errors none of the above can see.

**Publishing** — same as `ne-contracts`: the Pages workflow builds the payload into the
artifact, so `data/` never has to be committed. `index.html` is committed.

## Guard rails

- **Descriptive `User-Agent`** naming the project and a contact email, configurable and
  tested (`ne-contracts` has `test_user_agent.py` — copy it).
- **Cache every raw pull** under `data/raw/` keyed by the page's "as of" date. Skip the
  download if unchanged unless `--force`. Never re-fetch to "check."
- **Nightly at most.** Extracts refresh nightly; there is no faster signal. This is a
  small state agency — one polite pass, not a crawl.
- **Tier 2 scraping**: rate-limit hard (≥1s between requests), cache aggressively,
  prefer a single wide export over per-entity pagination. Enumerate `OrganizationID`
  only if an export can't give the same coverage.
- **Never commit `data/raw/` or `data/processed/`.** Big and regenerable.
- **Tests run before the scrape in CI** so a broken parser stops the run early.

## Build phases

1. ~~**Scaffold + tier 1 contributions.**~~ **Done.** `download_extracts.py`,
   `validate.py`, fixtures + tests.
2. ~~**Tier 1 expenditures + `normalize.py`.**~~ **Done.** Both extracts, five canonical
   tables, 43 tests, 2022–2026 collected and normalized.
3. **Tier 2 recon + registry.** Devtools session on the PublicSite search; implement
   `scrape_registry.py` for committees + candidates; join `Org ID` → entity in
   `normalize.py`. Still the unknown — sized after recon.
4. ~~**Tier 2 independent expenditures.**~~ **Mostly unnecessary** — IEs are in the bulk
   expenditures extract and already land in `independent_expenditures.csv`. What may
   remain: confirming whether non-committee independent spenders who file only a B-6/B-9
   are represented, since the `*Supplemental Filer` types in the data suggest they are.
5. **Site.** `build_search_index.py` + `build_site.py` + `index.html`, patterned on
   `ne-contracts`. Chunked JSON, client-side search, entity pages.
6. **Automation.** Nightly workflow: test → download → validate → normalize → build →
   deploy, opt-in like `ne-contracts`.

## Open questions

- **IE mechanism** — resolved during phase-3 recon, not now.
- **Site shape** — copy `ne-contracts`' one-big-page model, or per-committee pages given
  campaign finance is more relational? Decide at phase 5.
- **Entity resolution for contributors** — in scope for v1, or ship raw names first and
  add grouping later like `ne-contracts` did?
