"""Build ne-campaign-finance/index.html -- the dataset's landing page and,
as of PLAN.md 1.3, a small search site over it.

Cross-source search lives in ../ne-connect/; this page is where its "search
NADC" links land, and where a reporter arrives directly to look up one name
without caring about contracts or lobbying. Two indices ship inline (a
reporter can search the instant the page loads) and one payload is fetched
lazily the first time a specific contributor is expanded, following the
inline-plus-lazy split ne-connect uses for the same reason: 894 filers and
25,173 contributors are small enough to embed; their ~117k underlying
transaction rows are not.

Numbers come from data/processed/{summary.json,contributions.csv}. If the
data has not been built, the page is not built either.

Privacy (docs/PRIVACY.md in ne-connect, binding on this page too):
  - No address beyond city/state ever leaves this module. address_1/address_2
    (often a home street address for an individual contributor) are read from
    contributions.csv only to be discarded; see build_search_index().
  - Individuals get search results, not an entity page: the JS renders an
    individual contributor's matches as a plain transaction list with no
    aggregate stat card, while an organization or filer gets one. Same
    underlying data, different presentation, on purpose.
  - No reverse-address search, no bulk export, no cross-linking two private
    individuals: none of those are built here.
  - Every total is itemized-only; the caveat is in the page chrome, not a
    tooltip someone can miss.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUMMARY = ROOT / "data" / "processed" / "summary.json"
CONTRIBUTIONS = ROOT / "data" / "processed" / "contributions.csv"
META = ROOT / "data" / "scrape_meta.json"
OUT = ROOT / "index.html"
ROWS_OUT = ROOT / "d" / "rows.json"

# csv defaults to 128 KB; some description fields run long.
csv.field_size_limit(sys.maxsize)

INDIVIDUAL_SOURCE_TYPES = {"Individual", "Self (Candidate)"}

ORG_DETAIL_URL = (
    "https://nadc-e.nebraska.gov/PublicSite/SearchPages/OrganizationDetail.aspx"
    "?OrganizationID={org_id}"
)

STYLE = """
  :root {
    --bg:#fff; --panel:#f6f7f9; --border:#d9dde3; --text:#14181d; --muted:#626b76;
    --accent:#d00000; --accent-soft:#fdecec; --shadow:0 1px 3px rgba(0,0,0,.08);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#14171a; --panel:#1c2025; --border:#2c323a; --text:#e6e9ed;
      --muted:#949dab; --accent:#ff6b6b; --accent-soft:#2a1c1d;
      --shadow:0 1px 3px rgba(0,0,0,.4);
    }
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
    font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  .wrap{max-width:82ch;margin:0 auto;padding:26px 20px 60px}
  h1{margin:0 0 6px;font-size:22px;letter-spacing:-.01em}
  h2{margin:30px 0 10px;font-size:15px;text-transform:uppercase;letter-spacing:.05em;
    color:var(--muted)}
  p{margin:0 0 12px}
  .lede{color:var(--muted)}
  .stats{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}
  .stat{background:var(--panel);border:1px solid var(--border);border-radius:4px;
    padding:8px 12px;min-width:132px;box-shadow:var(--shadow)}
  .stat b{display:block;font-size:18px;letter-spacing:-.02em;
    font-variant-numeric:tabular-nums}
  .stat span{color:var(--muted);font-size:12px}
  table{border-collapse:collapse;width:100%;font-size:14px;margin:8px 0 16px}
  th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--border)}
  th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;
    letter-spacing:.04em}
  td.n{text-align:right;font-variant-numeric:tabular-nums}
  .warn{background:var(--accent-soft);border-left:3px solid var(--accent);
    border-radius:3px;padding:10px 12px;margin:14px 0}
  .warn strong{color:var(--accent)}
  ul{margin:0 0 12px;padding-left:20px}
  li{margin-bottom:7px}
  a{color:var(--accent)}
  footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--border);
    color:var(--muted);font-size:13px}

  /* --- search --- */
  #q{width:100%;padding:10px 12px;font-size:16px;border:1px solid var(--border);
    border-radius:4px;background:var(--panel);color:var(--text)}
  #q:focus{outline:2px solid var(--accent);outline-offset:-1px}
  .hint{color:var(--muted);font-size:12px;margin:6px 0 0}
  #results{margin-top:14px}
  .row{border:1px solid var(--border);border-radius:4px;padding:10px 12px;
    margin-bottom:8px;background:var(--panel);cursor:pointer}
  .row:hover{border-color:var(--accent)}
  .row .name{font-weight:600}
  .row .meta{color:var(--muted);font-size:13px;margin-top:2px}
  .kind{display:inline-block;font-size:11px;text-transform:uppercase;
    letter-spacing:.04em;color:var(--muted);border:1px solid var(--border);
    border-radius:3px;padding:1px 5px;margin-left:6px}
  .detail{margin-top:8px;padding:10px 12px;border:1px dashed var(--border);
    border-radius:4px;font-size:13px}
  .detail table{margin:6px 0 0}
  .empty{color:var(--muted);font-style:italic;padding:8px 0}
  .txn-recipient a{color:inherit;text-decoration:underline dotted}
"""


def _money(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def build_search_index():
    """One pass over contributions.csv -> two inline indices plus the lazy
    per-contributor transaction payload.

    Returns (contributors, filers, rows_by_contributor). `rows_by_contributor`
    already excludes address_1/address_2 -- nothing downstream of this
    function ever sees a home street address.
    """
    contributor_totals = {}   # source_name -> {total, count, kind}
    filer_totals = {}         # filer_name -> {org_id, total, count}
    rows_by_contributor = defaultdict(list)

    if not CONTRIBUTIONS.exists():
        return [], [], {}

    with CONTRIBUTIONS.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            source_name = (row.get("source_name") or "").strip()
            filer_name = (row.get("filer_name") or "").strip()
            included = row.get("include_in_total") == "True"
            amount = _money(row.get("amount"))

            if source_name:
                kind = (
                    "individual"
                    if row.get("source_type") in INDIVIDUAL_SOURCE_TYPES
                    else "organization"
                )
                c = contributor_totals.setdefault(
                    source_name, {"total": 0.0, "count": 0, "kind": kind}
                )
                if included:
                    c["total"] += amount
                    c["count"] += 1
                # Every contribution the source made, whether or not it counts
                # toward the headline total -- a search result that silently
                # dropped the excluded rows would look like the person gave
                # less than the record shows.
                rows_by_contributor[source_name].append(
                    [
                        row.get("receipt_date", ""),
                        round(amount, 2),
                        filer_name,
                        row.get("org_id", ""),
                        (row.get("city") or "").strip(),
                        (row.get("state") or "").strip(),
                        (row.get("description") or "").strip(),
                        included,
                    ]
                )

            if filer_name and included:
                f = filer_totals.setdefault(
                    filer_name, {"org_id": row.get("org_id", ""), "total": 0.0, "count": 0}
                )
                f["total"] += amount
                f["count"] += 1

    contributors = sorted(
        (
            [name, round(v["total"], 2), v["count"], v["kind"]]
            for name, v in contributor_totals.items()
        ),
        key=lambda r: -r[1],
    )
    filers = sorted(
        (
            [name, v["org_id"], round(v["total"], 2), v["count"]]
            for name, v in filer_totals.items()
        ),
        key=lambda r: -r[2],
    )
    return contributors, filers, dict(rows_by_contributor)


def main() -> int:
    if not SUMMARY.exists():
        print("no data/processed/summary.json -- run scripts/normalize.py first")
        return 1
    s = json.loads(SUMMARY.read_text())
    counts = s["row_counts"]
    years = s["years"]

    retrieved = ""
    if META.exists():
        runs = json.loads(META.read_text())
        # Only "contributions"/"expenditures" have the {year: [{"run_date":
        # ...}]} shape this loop expects. download_legacy.py's "legacy" key is
        # a flat dict instead -- see ne-connect's build_site.py, which had the
        # same bug for the same reason.
        stamps = [
            r["run_date"]
            for dataset in ("contributions", "expenditures")
            for rs in runs.get(dataset, {}).values()
            for r in rs
        ]
        retrieved = max(stamps) if stamps else ""

    contributors, filers, rows_by_contributor = build_search_index()

    ROWS_OUT.parent.mkdir(parents=True, exist_ok=True)
    rows_payload = json.dumps(rows_by_contributor, separators=(",", ":"))
    ROWS_OUT.write_text(rows_payload, encoding="utf-8")

    contributors_json = json.dumps(contributors, separators=(",", ":"))
    filers_json = json.dumps(filers, separators=(",", ":"))

    rows_table = "".join(
        f"<tr><td>{label}</td><td class='n'>{counts[key]:,}</td><td>{note}</td></tr>"
        for key, label, note in (
            ("contributions", "Contributions", "money and value received"),
            ("loans", "Loans", "borrowed, not given"),
            ("other_receipts", "Other receipts", "interest, refunds, adjustments, pledges"),
            ("expenditures", "Expenditures", "every row of the spending extract"),
            (
                "independent_expenditures",
                "Independent expenditures",
                "<strong>a subset of expenditures, not extra rows</strong>",
            ),
        )
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nebraska Campaign Finance</title>
<meta name="description" content="Search Nebraska campaign contributions {years[0]}-{years[-1]}, collected from the NADC FirstTuesday bulk extracts.">
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  <h1>Nebraska Campaign Finance</h1>
  <p class="lede">Contributions, loans and spending reported to the
  <a href="https://nadc.nebraska.gov/campaign-finance-general-information">Nebraska
  Accountability and Disclosure Commission</a>, {years[0]}&ndash;{years[-1]},
  collected from the FirstTuesday bulk extracts. Cross-source search across
  contracts, contributions and lobbying is in the
  <a href="../ne-connect/">Public Records Hub</a>.</p>

  <input id="q" type="search" placeholder="Search a contributor, filer, PAC or candidate committee&hellip;" autocomplete="off">
  <p class="hint" id="hint">{len(contributors):,} contributors &middot; {len(filers):,} filers,
  {years[0]}&ndash;{years[-1]}. Only itemized transactions are searchable here
  &mdash; contributions at or below the roughly $250 aggregate threshold are
  reported as lump sums on a committee's summary page, not as line items, so a
  small donor's total here is often an undercount, never an overcount.</p>
  <div id="results"></div>

  <h2>The tables</h2>
  <table>
    <tr><th>Table</th><th class="n">Rows</th><th>What it holds</th></tr>
    {rows_table}
  </table>

  <h2>Read this before quoting it</h2>
  <div class="warn">
    <strong>Summing the amount column over-counts.</strong> The state fans one
    transaction out over several rows that share an ID &mdash; an independent
    expenditure against five candidates is five rows, a contribution to a slate
    committee is one row per candidate &mdash; repeating the amount on each. Every
    row carries an <code>include_in_total</code> flag; this page's totals sum
    only where it is true. For 2022 contributions alone that is the difference
    between $68,865,363 and $68,955,221.
  </div>
  <ul>
    <li><strong>Only itemized transactions appear.</strong> Contributions below the
    roughly $250 aggregate threshold are reported as lump sums on a committee's
    summary page, never as line items. Totals built from line items undercount
    small money.</li>
    <li><strong>Amendments overwrite; there is no history.</strong> The amended
    column is a bare Y/N flag. A corrected filing replaces the original in place,
    so two pulls on different dates can disagree with no audit trail. Every dated
    pull is archived here because the state keeps no such record.</li>
    <li><strong>Each extract is a snapshot, not a ledger.</strong> Late and
    corrected transactions appear retroactively in prior years. No year is ever
    final.</li>
    <li><strong>A loan is not a contribution,</strong> and a bare pledge is not
    money &mdash; both are kept out of the contributions table on purpose. A
    $50,000 self-loan is borrowed, not raised.</li>
    <li><strong>{years[0]}&ndash;{years[-1]} only.</strong> This is the
    FirstTuesday era. Filings from 1985&ndash;2021 sit in a separate, frozen state
    dataset that is not collected here yet.</li>
    <li><strong>Federal candidates file with the FEC,</strong> not the NADC.</li>
    <li><strong>Home addresses are never shown here.</strong> The state's raw
    extract carries a street address for individual contributors; this page
    shows city and state only. The state's own site has the rest, for anyone
    who wants to look a filer up there directly.</li>
  </ul>

  <h2>Provenance</h2>
  <p>Downloaded from the NADC's
  <a href="https://nadc-e.nebraska.gov/PublicSite/DataDownload.aspx">bulk data
  page</a>{f", most recently {retrieved}" if retrieved else ""}. The state
  publishes column layouts, but the expenditures layout misnames three of its own
  columns and the transaction-type lists in both PDFs do not match the data, so
  the scraper checks every header against a confirmed layout and fails loudly
  rather than guessing.</p>

  <footer>
    Built {date.today().isoformat()}. Method, caveats and open work are in the
    <a href="https://github.com/diepjustin/diepjustin.github.io/tree/main/ne-campaign-finance">project README</a>.
    No analytics, no tracking, nothing loads from a third party.
  </footer>
</div>
<script>
// Inline: every contributor and filer, small enough to ship with the page.
// [name, total, count, kind] / [name, org_id, total, count]
const CONTRIBUTORS = {contributors_json};
const FILERS = {filers_json};
const ORG_DETAIL = {json.dumps(ORG_DETAIL_URL)};

// Lazy: one contributor's transaction rows, fetched only once someone expands
// that contributor. [date, amount, filer_name, org_id, city, state, description, included]
let ROWS = null;
function loadRows() {{
  if (ROWS) return Promise.resolve(ROWS);
  return fetch('d/rows.json').then(r => r.json()).then(j => {{ ROWS = j; return j; }});
}}

const money = n => '$' + n.toLocaleString(undefined, {{maximumFractionDigits: 0}});
const esc = s => String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));

function renderContributorDetail(name, kind) {{
  return loadRows().then(rows => {{
    const txns = (rows[name] || []).slice().sort((a, b) => (b[0] || '').localeCompare(a[0] || ''));
    if (!txns.length) return '<p class="empty">No itemized transactions on record.</p>';
    // Individuals: a plain transaction list, no aggregate stat card above it
    // -- see docs/PRIVACY.md, "search results, not a profile page."
    const header = kind === 'individual'
      ? ''
      : `<p><b>${{money(txns.filter(t => t[7]).reduce((s, t) => s + t[1], 0))}}</b> across
         ${{txns.filter(t => t[7]).length}} itemized transaction(s).</p>`;
    const rowsHtml = txns.slice(0, 200).map(t => {{
      const [dateStr, amount, filerName, orgId, city, state, desc, included] = t;
      const recipient = orgId
        ? `<a href="${{ORG_DETAIL.replace('{{org_id}}', encodeURIComponent(orgId))}}" target="_blank" rel="noopener">${{esc(filerName)}}</a>`
        : esc(filerName);
      const place = [city, state].filter(Boolean).join(', ');
      return `<tr${{included ? '' : ' style="opacity:.55" title="not counted in the total -- see include_in_total"'}}>
        <td>${{esc(dateStr)}}</td><td class="n">${{money(amount)}}</td>
        <td class="txn-recipient">${{recipient}}</td><td>${{esc(place)}}</td>
        <td>${{esc(desc)}}</td></tr>`;
    }}).join('');
    const more = txns.length > 200 ? `<p class="hint">${{txns.length - 200}} more not shown.</p>` : '';
    return header + `<table><tr><th>Date</th><th>Amount</th><th>To</th><th>City, State</th><th>Description</th></tr>${{rowsHtml}}</table>${{more}}`;
  }});
}}

function renderRow(kind, name, extra) {{
  const div = document.createElement('div');
  div.className = 'row';
  const label = kind === 'filer' ? 'filer'
    : extra.kind === 'individual' ? 'individual' : 'organization';
  div.innerHTML = `<div class="name">${{esc(name)}}<span class="kind">${{label}}</span></div>
    <div class="meta">${{money(extra.total)}} &middot; ${{extra.count}} itemized ${{kind === 'filer' ? 'received' : 'given'}}</div>`;
  const detail = document.createElement('div');
  detail.hidden = true;
  div.appendChild(detail);
  div.addEventListener('click', () => {{
    if (!detail.hidden) {{ detail.hidden = true; return; }}
    detail.hidden = false;
    if (detail.dataset.loaded) return;
    detail.dataset.loaded = '1';
    detail.className = 'detail';
    detail.innerHTML = '<p class="hint">Loading&hellip;</p>';
    if (kind === 'filer') {{
      detail.innerHTML = `<p>Full filing history on the state's own site:
        <a href="${{ORG_DETAIL.replace('{{org_id}}', encodeURIComponent(extra.orgId))}}" target="_blank" rel="noopener">
        OrganizationDetail.aspx?OrganizationID=${{esc(extra.orgId)}}</a></p>`;
    }} else {{
      renderContributorDetail(name, extra.kind).then(h => {{ detail.innerHTML = h; }});
    }}
  }});
  return div;
}}

function search(term) {{
  const results = document.getElementById('results');
  results.innerHTML = '';
  const t = term.trim().toUpperCase();
  if (t.length < 2) {{
    document.getElementById('hint').hidden = false;
    return;
  }}
  const cMatches = CONTRIBUTORS.filter(c => c[0].toUpperCase().includes(t)).slice(0, 30);
  const fMatches = FILERS.filter(f => f[0].toUpperCase().includes(t)).slice(0, 30);
  if (!cMatches.length && !fMatches.length) {{
    results.innerHTML = '<p class="empty">No match. Try a shorter or differently spelled name.</p>';
    return;
  }}
  fMatches.forEach(([name, orgId, total, count]) => {{
    results.appendChild(renderRow('filer', name, {{orgId, total, count}}));
  }});
  cMatches.forEach(([name, total, count, kind]) => {{
    results.appendChild(renderRow('contributor', name, {{total, count, kind}}));
  }});
}}

const qInput = document.getElementById('q');
qInput.addEventListener('input', () => {{
  const url = new URL(location.href);
  if (qInput.value) url.searchParams.set('q', qInput.value); else url.searchParams.delete('q');
  history.replaceState(null, '', url);
  search(qInput.value);
}});

const initial = new URL(location.href).searchParams.get('q');
if (initial) {{
  qInput.value = initial;
  search(initial);
}}
</script>
</body>
</html>
"""
    OUT.write_text(html, encoding="utf-8")
    print(f"  contributors  {len(contributors):>8,}")
    print(f"  filers        {len(filers):>8,}")
    print(f"  d/rows.json   {len(rows_payload) / 1024:>8,.0f} KB")
    print(f"  -> {OUT.name} ({len(html) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
