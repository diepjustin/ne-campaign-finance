from pathlib import Path

import build_financial_interests as bfi

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_TEXT = (FIXTURES / "c1_text_sample.txt").read_text(encoding="utf-8")


def test_should_process_year_excludes_pre_freeze_and_ambiguous_2022():
    assert bfi.should_process_year(2021) is False
    assert bfi.should_process_year(2022) is False  # straddles the freeze date
    assert bfi.should_process_year(2023) is True
    assert bfi.should_process_year(2026) is True


def test_split_items_finds_every_schedule_item():
    blocks = bfi.split_items(SAMPLE_TEXT)
    for item_number in ("6", "7", "8", "10", "11"):
        assert item_number in blocks


def test_split_items_stops_at_the_instructions_boilerplate_page():
    """A real filing's trailing 'General Information' page repeats item
    numbers in its own prose ('ITEM 6 in the instructions...') -- those must
    never be parsed as a sixth income-source entry."""
    blocks = bfi.split_items(SAMPLE_TEXT)
    assert "instructions text should never be parsed" not in blocks.get("6", "")


def test_parse_item_block_pairs_numbered_entries_with_their_detail():
    blocks = bfi.split_items(SAMPLE_TEXT)
    entries = bfi.parse_item_block(blocks["6"])
    assert entries == [
        ("Country Partners Cooperative",
         "Farm Income\nP.O. Box 80\nGothenburg NE 69138"),
    ]


def test_parse_item_block_drops_blank_numbered_placeholders():
    """Item 6's fixture has four numbered slots but only the first is filled
    -- "2.) 2a.)" etc must not become three empty rows."""
    blocks = bfi.split_items(SAMPLE_TEXT)
    entries = bfi.parse_item_block(blocks["6"])
    assert len(entries) == 1


def test_parse_item_block_falls_back_to_line_by_line_for_unnumbered_items():
    blocks = bfi.split_items(SAMPLE_TEXT)
    entries = bfi.parse_item_block(blocks["10"])
    assert entries == [
        ("Mazda Capital Services c/o Chase P.O. Box 78232, Phoeniz AZ 85062-8232", ""),
    ]


def test_parse_item_block_filters_the_items_own_all_caps_title():
    """Item 8's block text starts with its own printed title
    ('REAL PROPERTY OF THE FILER IN NEBRASKA') -- that must never be emitted
    as if a filer wrote it."""
    blocks = bfi.split_items(SAMPLE_TEXT)
    entries = bfi.parse_item_block(blocks["8"])
    names = [name for name, _ in entries]
    assert "REAL PROPERTY OF THE FILER IN NEBRASKA" not in names
    assert "Jeffrey Lake Cabin#50 Lot 15, Area 4 S-9(Imp only) 6 B G TP, 50 So." in names


def test_is_boilerplate_line_flags_all_caps_and_known_instructional_phrases():
    assert bfi.is_boilerplate_line("REAL PROPERTY OF THE FILER IN NEBRASKA")
    assert bfi.is_boilerplate_line("If you have nothing to report, write NONE")
    assert bfi.is_boilerplate_line("")
    assert not bfi.is_boilerplate_line("Jeffrey Lake Cabin, Lincoln Co, NE")


def test_rows_from_text_tags_every_row_with_the_ocr_flag():
    rows = bfi.rows_from_text("d1", SAMPLE_TEXT.splitlines(), "https://example.test", "2026-09-15", ocr=True)
    assert rows
    assert all(r["ocr"] is True for r in rows)
    assert all(r["disclosure_id"] == "d1" for r in rows)
    assert all(r["source_url"] == "https://example.test" for r in rows)


def test_rows_from_text_never_rewrites_the_source_text():
    """CLAUDE.md rule 1, carried into this repo: verbatim or not at all."""
    rows = bfi.rows_from_text("d1", SAMPLE_TEXT.splitlines(), "u", "2026-09-15", ocr=False)
    income_row = next(r for r in rows if r["item_type"] == "income_source")
    assert income_row["counterparty_name_raw"] == "Country Partners Cooperative"
    assert "P.O. Box 80" in income_row["detail"]


def test_sha256_and_raw_pdf_path_are_content_addressed(tmp_path):
    data = b"%PDF-1.4 fake content"
    sha = bfi.sha256_bytes(data)
    path = bfi.raw_pdf_path(sha, tmp_path)
    assert path.name == f"{sha}.pdf"


def test_save_raw_pdf_is_immutable_and_never_rewrites_an_existing_capture(tmp_path):
    data = b"%PDF-1.4 fake content"
    sha1 = bfi.save_raw_pdf(data, tmp_path)
    path = bfi.raw_pdf_path(sha1, tmp_path)
    original_mtime = path.stat().st_mtime_ns

    sha2 = bfi.save_raw_pdf(data, tmp_path)  # same bytes, saved again
    assert sha1 == sha2
    assert path.stat().st_mtime_ns == original_mtime  # never rewritten


def test_write_financial_interests_csv_round_trips(tmp_path):
    rows = [{
        "disclosure_id": "d1", "item_type": "gift", "counterparty_name_raw": "Acme",
        "detail": "", "source_url": "u", "retrieved_at": "2026-09-15", "ocr": False,
    }]
    out = tmp_path / "financial_interests.csv"
    count = bfi.write_financial_interests_csv(rows, out)
    assert count == 1
    text = out.read_text(encoding="utf-8")
    assert "disclosure_id" in text.splitlines()[0]
    assert "Acme" in text
