import hashlib
import io
import json
import zipfile
from datetime import date
from pathlib import Path

import download_legacy as dl
import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _sample_zip(members: dict) -> bytes:
    """members: {in-zip name (no prefix) -> text content}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in members.items():
            zf.writestr(f"{dl.ZIP_PREFIX}{name}", text)
    return buf.getvalue()


def _full_zip() -> bytes:
    """A zip containing every member download_legacy.py expects, so tests
    don't need to hand-list all 15 form/doc files."""
    stripped = {name[len(dl.ZIP_PREFIX) :]: "H1|H2\nv1|v2\n" for name in dl.needed_members()}
    return _sample_zip(stripped)


def test_needed_members_covers_every_needed_form_and_doc():
    members = dl.needed_members()
    assert len(members) == len(dl.NEEDED_FORMS) + len(dl.DOC_MEMBERS)
    assert f"{dl.ZIP_PREFIX}formc1.txt" in members
    assert f"{dl.ZIP_PREFIX}nadc_tables.rtf" in members


def test_extract_members_writes_files_and_reports_sizes(tmp_path):
    zip_bytes = _full_zip()
    extracted = dl.extract_members(zip_bytes, tmp_path)
    assert set(extracted) == {m[len(dl.ZIP_PREFIX) :] for m in dl.needed_members()}
    assert (tmp_path / "formc1.txt").read_text() == "H1|H2\nv1|v2\n"
    assert extracted["formc1.txt"] == len("H1|H2\nv1|v2\n".encode("utf-8"))


def test_extract_members_raises_on_missing_member(tmp_path):
    zip_bytes = _sample_zip({"formc1.txt": "H1|H2\nv1|v2\n"})  # missing everything else
    with pytest.raises(ValueError, match="missing expected member"):
        dl.extract_members(zip_bytes, tmp_path)


def test_download_legacy_first_run_writes_dated_dir_and_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dl, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(dl, "META_PATH", tmp_path / "scrape_meta.json")

    zip_bytes = _full_zip()
    monkeypatch.setattr(dl, "fetch_zip", lambda url, **kw: zip_bytes)

    result = dl.download_legacy(user_agent="test-agent", run_date=date(2026, 9, 11))

    expected_sha = hashlib.sha256(zip_bytes).hexdigest()
    assert result == {
        "skipped": False,
        "sha256": expected_sha,
        "path": str(tmp_path / "raw" / "legacy" / "2026-09-11"),
    }
    assert (tmp_path / "raw" / "legacy" / "2026-09-11" / "formc1.txt").exists()

    meta = json.loads((tmp_path / "scrape_meta.json").read_text())
    assert meta["legacy"]["sha256"] == expected_sha
    assert meta["legacy"]["retrieved_at"] == "2026-09-11"
    assert meta["legacy"]["source_url"] == dl.LEGACY_URL


def test_download_legacy_skips_extraction_when_sha_unchanged_and_files_present(
    tmp_path, monkeypatch
):
    """The normal repeat-run case: a prior local run (or a cache hit) left
    the extracted files on this machine, so a matching sha really does mean
    there's nothing new to do."""
    monkeypatch.setattr(dl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dl, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(dl, "META_PATH", tmp_path / "scrape_meta.json")

    zip_bytes = _full_zip()
    sha = hashlib.sha256(zip_bytes).hexdigest()
    extracted = dl.extract_members(zip_bytes, tmp_path / "raw" / "legacy" / "2026-09-01")
    dl.save_meta({
        "legacy": {
            "sha256": sha, "retrieved_at": "2026-09-01",
            "path": "raw/legacy/2026-09-01", "members": extracted,
        }
    })
    monkeypatch.setattr(dl, "fetch_zip", lambda url, **kw: zip_bytes)

    result = dl.download_legacy(user_agent="test-agent", run_date=date(2026, 9, 11))

    assert result == {"skipped": True, "sha256": sha}
    assert not (tmp_path / "raw" / "legacy" / "2026-09-11").exists()


def test_download_legacy_reextracts_when_sha_matches_but_files_are_missing(
    tmp_path, monkeypatch, capsys
):
    """The real bug found running this on a fresh CI runner: scrape_meta.json
    is committed (a receipt), but the extracted files under data/raw/legacy/
    are gitignored -- a fresh checkout/cache can have matching metadata and
    zero actual files. Skipping in that case leaves normalize_legacy.py with
    nothing to read. A matching sha must not skip unless the files are
    actually here."""
    monkeypatch.setattr(dl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dl, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(dl, "META_PATH", tmp_path / "scrape_meta.json")

    zip_bytes = _full_zip()
    sha = hashlib.sha256(zip_bytes).hexdigest()
    # Metadata claims a prior capture, but raw/legacy/2026-09-01/ was never
    # actually created on this machine -- e.g. a fresh CI cache miss.
    dl.save_meta({
        "legacy": {
            "sha256": sha, "retrieved_at": "2026-09-01",
            "path": "raw/legacy/2026-09-01",
            "members": {"formc1.txt": 123},
        }
    })
    monkeypatch.setattr(dl, "fetch_zip", lambda url, **kw: zip_bytes)

    result = dl.download_legacy(user_agent="test-agent", run_date=date(2026, 9, 11))

    assert result["skipped"] is False
    assert (tmp_path / "raw" / "legacy" / "2026-09-11" / "formc1.txt").exists()
    assert "isn't present on this machine" in capsys.readouterr().out


def test_download_legacy_warns_and_recaptures_on_changed_sha(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dl, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(dl, "META_PATH", tmp_path / "scrape_meta.json")

    dl.save_meta({"legacy": {"sha256": "old" * 20, "retrieved_at": "2026-01-01"}})
    zip_bytes = _full_zip()
    monkeypatch.setattr(dl, "fetch_zip", lambda url, **kw: zip_bytes)

    result = dl.download_legacy(user_agent="test-agent", run_date=date(2026, 9, 11))

    assert result["skipped"] is False
    assert (tmp_path / "raw" / "legacy" / "2026-09-11").exists()
    assert "WARNING" in capsys.readouterr().err


def test_download_legacy_force_recaptures_even_when_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "DATA_DIR", tmp_path)
    monkeypatch.setattr(dl, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(dl, "META_PATH", tmp_path / "scrape_meta.json")

    zip_bytes = _full_zip()
    sha = hashlib.sha256(zip_bytes).hexdigest()
    dl.save_meta({"legacy": {"sha256": sha, "retrieved_at": "2026-09-01"}})
    monkeypatch.setattr(dl, "fetch_zip", lambda url, **kw: zip_bytes)

    result = dl.download_legacy(user_agent="test-agent", force=True, run_date=date(2026, 9, 11))

    assert result["skipped"] is False
    assert (tmp_path / "raw" / "legacy" / "2026-09-11").exists()
