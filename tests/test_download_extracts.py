import io
import json
import zipfile
from datetime import date
from pathlib import Path

import download_extracts as de
import pytest
from validate import ValidationError

FIXTURES = Path(__file__).parent / "fixtures"


def _zip_of(csv_paths):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, path in csv_paths.items():
            zf.writestr(name, path.read_text())
    return buf.getvalue()


def test_extract_url_pattern():
    assert de.extract_url(2024, "contributions") == (
        "https://nadc-e.nebraska.gov/PublicSite/Docs/BulkDataDownloads/"
        "2024_ContributionLoanExtract.csv.zip"
    )
    assert de.extract_url(2024, "expenditures") == (
        "https://nadc-e.nebraska.gov/PublicSite/Docs/BulkDataDownloads/"
        "2024_ExpenditureExtract.csv.zip"
    )


def test_csv_text_from_zip_happy_path():
    sample = FIXTURES / "contributions_sample.csv"
    zip_bytes = _zip_of({"2026_ContributionLoanExtract.csv": sample})
    text = de.csv_text_from_zip(zip_bytes)
    assert text == sample.read_text()


def test_csv_text_from_zip_rejects_wrong_member_count():
    sample = FIXTURES / "contributions_sample.csv"
    empty_zip = io.BytesIO()
    with zipfile.ZipFile(empty_zip, "w"):
        pass
    with pytest.raises(ValidationError):
        de.csv_text_from_zip(empty_zip.getvalue())

    two_csvs = _zip_of({"a.csv": sample, "b.csv": sample})
    with pytest.raises(ValidationError):
        de.csv_text_from_zip(two_csvs)


def test_previous_row_count_reads_last_run():
    meta = {"contributions": {"2026": [{"row_count": 10}, {"row_count": 15}]}}
    assert de.previous_row_count(meta, "contributions", 2026) == 15
    assert de.previous_row_count(meta, "contributions", 2099) is None


def test_parse_args_default_years_span_first_year_to_today():
    args = de.parse_args([])
    assert args.years[0] == de.FIRST_YEAR
    assert args.years[-1] == date.today().year
    assert args.datasets == de.DATASETS  # both extracts by default
    assert args.force is False


def test_downloader_and_validator_agree_on_dataset_names():
    # download_extracts.py raises at import if these drift apart; assert the
    # invariant here too so the reason is visible in the suite.
    assert set(de.DATASETS) == set(de.SCHEMAS)


def test_download_year_writes_snapshot_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(de, "DATA_DIR", tmp_path)
    monkeypatch.setattr(de, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(de, "META_PATH", tmp_path / "scrape_meta.json")

    sample_csv = (FIXTURES / "contributions_sample.csv").read_text()
    monkeypatch.setattr(
        de, "fetch_zip", lambda url, **kw: _zip_of({"x.csv": FIXTURES / "contributions_sample.csv"})
    )

    result = de.download_year(
        2026,
        "contributions",
        user_agent="test-agent",
        force=False,
        allow_shrink=False,
        run_date=date(2026, 9, 8),
    )

    expected_rows = len(sample_csv.strip().splitlines()) - 1
    assert result == {"year": 2026, "skipped": False, "row_count": expected_rows}

    out_path = tmp_path / "raw" / "contributions" / "2026" / "2026-09-08.csv"
    assert out_path.read_text() == sample_csv

    meta = json.loads((tmp_path / "scrape_meta.json").read_text())
    run = meta["contributions"]["2026"][0]
    assert run["row_count"] == expected_rows
    assert run["column_count"] == 24


def test_download_year_skips_when_already_pulled_today(tmp_path, monkeypatch):
    monkeypatch.setattr(de, "DATA_DIR", tmp_path)
    monkeypatch.setattr(de, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(de, "META_PATH", tmp_path / "scrape_meta.json")

    out_dir = tmp_path / "raw" / "contributions" / "2026"
    out_dir.mkdir(parents=True)
    (out_dir / "2026-09-08.csv").write_text("already here")

    def _boom(*a, **k):  # fetch_zip must not be called
        raise AssertionError("should have skipped the network call")

    monkeypatch.setattr(de, "fetch_zip", _boom)

    result = de.download_year(
        2026,
        "contributions",
        user_agent="test-agent",
        force=False,
        allow_shrink=False,
        run_date=date(2026, 9, 8),
    )
    assert result == {"year": 2026, "skipped": True}


def test_download_year_rejects_large_row_count_drop(tmp_path, monkeypatch):
    monkeypatch.setattr(de, "DATA_DIR", tmp_path)
    monkeypatch.setattr(de, "RAW_DIR", tmp_path / "raw")
    meta_path = tmp_path / "scrape_meta.json"
    monkeypatch.setattr(de, "META_PATH", meta_path)
    meta_path.write_text(json.dumps({"contributions": {"2026": [{"row_count": 100}]}}))

    monkeypatch.setattr(
        de, "fetch_zip", lambda url, **kw: _zip_of({"x.csv": FIXTURES / "contributions_sample.csv"})
    )

    with pytest.raises(ValidationError, match="row count dropped"):
        de.download_year(
            2026,
            "contributions",
            user_agent="test-agent",
            force=False,
            allow_shrink=False,
            run_date=date(2026, 9, 8),
        )
