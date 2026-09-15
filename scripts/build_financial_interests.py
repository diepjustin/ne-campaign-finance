"""PDF pipeline for C-1/C-2 filings: c1_filings.csv -> financial_interests.csv.

Scope, per PLAN.md 1.5: only filings after the 2022-07-11 legacy-zip freeze
date are handled here. `nadc_data.zip`'s formc1.txt/formc1inc.txt already
covers everything before it (see normalize_legacy_c1.py). A Filing Year of
2022 straddles the freeze, so those rows are only processed if their own
`filed_date` (the online grid's "Date Filed" column is NOT what gates this --
`disclosure_id`/filing metadata in c1_filings.csv doesn't carry the underlying
filing period, only the year and when the report was filed) falls after the
freeze; scrape_c1.py already writes `year` as the Filing Year the state
assigns, so the simple and defensible rule used here is `year >= 2023`
processed unconditionally, `year == 2022` processed only when retrieved_at (or
a future filed_date column) proves it, and `year <= 2021` left to the legacy
pipeline entirely. See `should_process_year()`.

Two very different PDF populations, confirmed empirically 2026-09-14:

  Manual (scanned paper filings, direct PFD_FilingID link): sampled 4 real
  2023 filings, 3 of 4 (75%) extract ZERO characters -- scanned images with no
  text layer. OCR is the PRIMARY path for these, not a fallback.

  Electronic (resolved via scrape_c1.resolve_electronic_pdf's postback replay):
  every one of these is a server-rendered, born-digital PDF -- the one sample
  pulled this way (John Abeggien, 2023) extracted cleanly with pypdf, no OCR
  needed. This makes sense: the state only has an image to scan for a filing
  that arrived on paper.

Every row in the output carries `ocr` (True/False) precisely so a downstream
reader can tell "the state's own typed text" from "a machine's best guess at a
photograph of a form" apart, per this task's instruction not to present them
at equal confidence. Text is never rewritten or summarized (ne-connect's
CLAUDE.md rule 1 applies here too) -- item extraction below only *segments*
the extracted/OCR'd text into per-schedule-item rows; it does not paraphrase.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
from pathlib import Path

from pypdf import PdfReader
from pdfminer.high_level import extract_text as pdfminer_text

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape_c1 import Fetcher, resolve_electronic_pdf  # noqa: E402

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROCESSED_DIR = DATA_DIR / "processed"
RAW_C1_DIR = DATA_DIR / "raw" / "c1"

MIN_CHARS_PER_PAGE = 50  # same threshold ne-contracts/scripts/extract_text.py uses

FIRST_ONLINE_YEAR_UNCONDITIONAL = 2023

# NADC Form C-1 item numbers -> the kind of thing they disclose. Items 1-5
# (name/address/office/office sought/period) and 12-13 (supplemental note,
# signature) aren't financial-interest items and are never emitted as rows.
ITEM_TYPES = {
    "6": "income_source",
    "7": "business_association",
    "8": "real_property",
    "9": "other_financial_interest",
    "10": "creditor",
    "11": "gift",
}

_ITEM_HEADER = re.compile(r"ITEM\s+(\d{1,2})\b")
_BOILERPLATE_PAGE = re.compile(r"General Information\s*-\s*Filing Requirements", re.I)
_NUMBERED_ENTRY = re.compile(
    r"(\d+)[a-z]?\.\)\s*(.*?)(?=\d+[a-z]?\.\)|\Z)", re.S
)
_BOILERPLATE_LINE = re.compile(
    r"^\s*(none|choose value:?|if you have nothing to report.*|note:.*|"
    r"[a-d]\)\s*\$[\d,.]+.*|the monetary value.*)\s*$",
    re.I,
)


def is_boilerplate_line(line: str) -> bool:
    """True for an item's own printed title/instructions rather than
    something a filer wrote. Two signals, both seen in real filings:
    an explicit instructional phrase (_BOILERPLATE_LINE), or a line that is
    entirely upper-case -- item titles and column headers print in caps
    ("REAL PROPERTY OF THE FILER IN NEBRASKA"), while a filer's own answers
    -- a name, an address, a business -- do not, even when short."""
    stripped = line.strip()
    if not stripped:
        return True
    if _BOILERPLATE_LINE.match(stripped):
        return True
    letters = [c for c in stripped if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters) and len(letters) > 3

FIELDNAMES = ["disclosure_id", "item_type", "counterparty_name_raw", "detail",
              "source_url", "retrieved_at", "ocr"]


def should_process_year(year: int) -> bool:
    """See module docstring: 2022 is genuinely ambiguous without a filed_date
    column, so it is excluded rather than guessed at -- an omission is
    recoverable (rerun once that column exists), a wrongly-merged row is not.
    """
    return year >= FIRST_ONLINE_YEAR_UNCONDITIONAL


def read_filings(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def raw_pdf_path(sha256: str, raw_dir: Path = RAW_C1_DIR) -> Path:
    return raw_dir / f"{sha256}.pdf"


def save_raw_pdf(data: bytes, raw_dir: Path = RAW_C1_DIR) -> str:
    """Content-addressed, immutable -- same convention as every other raw
    capture in this project. A sha already on disk is never rewritten."""
    sha = sha256_bytes(data)
    path = raw_pdf_path(sha, raw_dir)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return sha


def fetch_manual_pdf(fetcher: Fetcher, document_url: str) -> bytes:
    resp = fetcher.session.get(document_url, timeout=60)
    resp.raise_for_status()
    return resp.content


def extract_text_pages(pdf_bytes: bytes) -> tuple[list[str], bool]:
    """(page_texts, is_scanned). Mirrors ne-contracts/scripts/extract_text.py's
    pypdf-then-pdfminer approach and its MIN_CHARS_PER_PAGE scanned heuristic."""
    from io import BytesIO
    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        texts = [p.extract_text() or "" for p in reader.pages]
    except Exception:
        texts = None

    if texts is not None:
        chars = sum(len(t) for t in texts)
        avg = chars / max(len(texts), 1)
        if avg >= MIN_CHARS_PER_PAGE:
            return texts, False

    try:
        text = pdfminer_text(BytesIO(pdf_bytes))
    except Exception:
        return (texts or [""]), True

    pages = text.split("\f")
    chars = len(text)
    avg = chars / max(len(pages), 1)
    if avg >= MIN_CHARS_PER_PAGE:
        return pages, False
    return (texts if texts is not None else pages), True


def ocr_pdf(pdf_bytes: bytes, dpi: int = 200) -> list[str]:
    """Render each page to an image and run tesseract. Only reached for
    scanned PDFs -- confirmed the common case for Manual filings (75% in a
    real sample), so this is the primary path for that population, not a
    hedge. OCR quality on a photographed, hand-filled government form is not
    verified here beyond "tesseract produced characters" -- see this
    project's report on the ocr=True column for why every row using this path
    is flagged, not trusted at face value.
    """
    import fitz  # PyMuPDF
    import pytesseract
    from PIL import Image
    import io

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)
    texts = []
    for page in doc:
        pix = page.get_pixmap(matrix=matrix)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        texts.append(pytesseract.image_to_string(img))
    return texts


def split_items(full_text: str) -> dict[str, str]:
    """Full document text -> {item number: block text}, stopping at the
    boilerplate "General Information" instructions page so its own "ITEM 6"
    cross-references in the instructions text don't get parsed as data."""
    cut = _BOILERPLATE_PAGE.search(full_text)
    if cut:
        full_text = full_text[: cut.start()]

    headers = list(_ITEM_HEADER.finditer(full_text))
    blocks = {}
    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(full_text)
        blocks.setdefault(m.group(1), "")
        blocks[m.group(1)] += full_text[m.end():end]
    return blocks


def parse_item_block(text: str) -> list[tuple[str, str]]:
    """One item's block -> [(counterparty_name_raw, detail), ...], verbatim.

    Two shapes seen in real filings: numbered entries ("1.) Name 1a.) detail",
    common in Items 6-7 and 11), and unnumbered lines (Items 8, 9, 10 -- one
    filing per line). Both are handled; neither rewrites the state's words,
    only segments them. This is a best-effort segmentation, not a full
    field-level parse -- see the module docstring's confidence note.
    """
    entries = list(_NUMBERED_ENTRY.finditer(text))
    if entries:
        # Pair "N" (name) with "Na" (detail) by numeric prefix, in order.
        by_number: dict[str, list[str]] = {}
        order: list[str] = []
        for m in entries:
            label = m.group(0)
            num_match = re.match(r"(\d+)", label)
            num = num_match.group(1)
            body = m.group(2).strip()
            if num not in by_number:
                by_number[num] = []
                order.append(num)
            by_number[num].append(body)
        results = []
        for num in order:
            parts = [p for p in by_number[num] if p and not is_boilerplate_line(p)]
            if not parts:
                continue
            name = parts[0]
            detail = " ".join(parts[1:]) if len(parts) > 1 else ""
            results.append((name, detail))
        return results

    lines = [ln.strip() for ln in text.splitlines()]
    results = []
    for line in lines:
        if is_boilerplate_line(line):
            continue
        results.append((line, ""))
    return results


def rows_from_text(disclosure_id: str, page_texts: list[str], source_url: str,
                    retrieved_at: str, ocr: bool) -> list[dict]:
    full_text = "\n".join(page_texts)
    blocks = split_items(full_text)
    rows = []
    for item_number, item_type in ITEM_TYPES.items():
        block = blocks.get(item_number, "")
        if not block.strip():
            continue
        for name, detail in parse_item_block(block):
            rows.append({
                "disclosure_id": disclosure_id,
                "item_type": item_type,
                "counterparty_name_raw": name,
                "detail": detail,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
                "ocr": ocr,
            })
    return rows


def process_filing(fetcher: Fetcher, filing: dict, raw_dir: Path = RAW_C1_DIR) -> list[dict]:
    if filing["filed_method"] == "Manual":
        pdf_bytes = fetch_manual_pdf(fetcher, filing["document_url"])
    else:
        # Split "LAST FIRST MIDDLE" style raw name back into what
        # resolve_electronic_pdf expects: it re-does the exact search that
        # produced this row, so it needs the same fields verbatim.
        pdf_bytes = resolve_electronic_pdf(
            fetcher, int(filing["year"]), filing["filer_name_raw"],
            filing["filer_office"], filing.get("filed_date", ""),
        )
        if pdf_bytes is None:
            return []

    save_raw_pdf(pdf_bytes, raw_dir)
    page_texts, scanned = extract_text_pages(pdf_bytes)
    if scanned:
        page_texts = ocr_pdf(pdf_bytes)

    return rows_from_text(
        filing["disclosure_id"], page_texts, filing["document_url"],
        filing["retrieved_at"], ocr=scanned,
    )


def write_financial_interests_csv(rows: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Parse C-1/C-2 PDFs into financial_interests.csv.")
    parser.add_argument("--filings", type=Path, default=PROCESSED_DIR / "c1_filings.csv")
    parser.add_argument("--out", type=Path, default=PROCESSED_DIR / "financial_interests.csv")
    parser.add_argument("--limit", type=int, default=None,
                         help="stop after this many filings (for sampling before a full run)")
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args()

    filings = [f for f in read_filings(args.filings) if should_process_year(int(f["year"]))]
    if args.limit:
        filings = filings[: args.limit]

    fetcher = Fetcher(delay=args.delay)
    all_rows = []
    scanned_count = 0
    for i, filing in enumerate(filings, 1):
        print(f"[{i}/{len(filings)}] {filing['disclosure_id']} ({filing['filed_method']})")
        try:
            rows = process_filing(fetcher, filing)
        except Exception as exc:
            print(f"  error: {exc}", file=sys.stderr)
            continue
        if any(r["ocr"] for r in rows):
            scanned_count += 1
        all_rows.extend(rows)

    count = write_financial_interests_csv(all_rows, args.out)
    print(f"\nWrote {count} items from {len(filings)} filings to {args.out}")
    print(f"{scanned_count} filings required OCR")


if __name__ == "__main__":
    main()
