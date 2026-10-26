import pandas as pd
import pytest

from services.content_service import PreparationError, content_hash, prepare_content

URL = "https://example.com/admin/new"


def _csv(tmp_path, rows, cols=("website_url", "title", "content", "category", "tags", "images", "publish_mode")):
    p = tmp_path / "in.csv"
    pd.DataFrame(rows, columns=list(cols)).to_csv(p, index=False)
    return p


def test_valid_rows_are_normalized_and_all_processed(tmp_path, png):
    p = _csv(tmp_path, [
        [URL, "  Benefits   of AI ", "AI improves\r\nefficiency.", "technology", "AI; Automation|ai, ", "images/automation.png", ""],
        [URL, "Second", "Body two", "", "", "", "PUBLISH"],
    ])
    r = prepare_content(p)
    assert len(r.items) == 2 and not r.errors
    a, b = r.items
    assert a.title == "Benefits of AI" and a.content == "AI improves\nefficiency."
    assert a.category == "Technology" and a.tags == ["AI", "Automation"]  # deduped case-insensitively
    assert a.images == ["images/automation.png"] and a.image_paths[0].endswith("automation.png")
    assert a.publish_mode == "draft" and a.validation_status == "passed" and len(a.content_hash) == 64
    assert (a.row_number, b.row_number) == (2, 3) and b.publish_mode == "publish" and b.category is None


def test_invalid_rows_reported_with_row_numbers_and_skipped_safely(tmp_path):
    p = _csv(tmp_path, [
        [URL, "Good", "Body", "", "", "", "draft"],
        ["ftp://bad", "", "", "", "", "missing.png", "later"],
        [URL, "Also good", "Body 2", "", "", "", "draft"],
    ])
    r = prepare_content(p)
    assert [i.row_number for i in r.items] == [2, 4]
    assert len(r.errors) == 1 and r.errors[0].row_number == 3
    text = " ".join(r.errors[0].errors)
    for needle in ("website_url", "title is empty", "content is empty", "publish_mode", "image not found"):
        assert needle in text


def test_duplicate_rows_in_file_flagged(tmp_path):
    p = _csv(tmp_path, [[URL, "Same", "Body", "", "", "", ""], [URL, " same ", "body", "", "", "", ""]])
    r = prepare_content(p)
    assert len(r.items) == 1 and "duplicate of row 2" in r.errors[0].errors[0]


def test_hash_depends_on_site_title_content():
    h = content_hash(URL, "T", "C")
    assert h == content_hash(URL + "/", " t ", "c")
    assert h != content_hash("https://other.com/admin/new", "T", "C")


def test_excel_and_extra_columns(tmp_path):
    p = tmp_path / "in.xlsx"
    pd.DataFrame([{"Website URL": URL, "Title": "X", "Content": "Y", "author": "Ann"}]).to_excel(p, index=False)
    r = prepare_content(p)
    assert r.items[0].metadata == {"author": "Ann"} and r.items[0].tags == []


def test_file_level_errors(tmp_path):
    with pytest.raises(PreparationError, match="not found"):
        prepare_content(tmp_path / "nope.csv")
    bad = tmp_path / "a.txt"; bad.write_text("x")
    with pytest.raises(PreparationError, match="Unsupported"):
        prepare_content(bad)
    p = tmp_path / "m.csv"; p.write_text("title,content\nA,B\n")
    with pytest.raises(PreparationError, match="website_url"):
        prepare_content(p)


def test_local_file_urls_need_opt_in(tmp_path):
    p = _csv(tmp_path, [["file:///tmp/x.html", "T", "C", "", "", "", ""]])
    assert prepare_content(p).errors and prepare_content(p, allow_local=True).items
