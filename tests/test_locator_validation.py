from browser.locator_validator import LocatorValidator
from llm.schemas import Status


def _v(mgr, url):
    mgr.navigate(url)
    return LocatorValidator(mgr.page)


def test_valid_selectors_pass(browser_page, mock_cms_url):
    v = _v(browser_page, mock_cms_url)
    assert v.validate_one("title", "#headline").status == Status.PASS
    assert v.validate_one("content", "#article-body").status == Status.PASS
    assert v.validate_one("category", "select[name='category']").status == Status.PASS
    assert v.validate_one("image", "input[type='file']").status == Status.PASS
    assert v.validate_one("save_draft", "button:has-text('Save Draft')").status == Status.PASS
    assert v.validate_one("publish", "#publish").status == Status.PASS


def test_invalid_selector_rejected(browser_page, mock_cms_url):
    item = _v(browser_page, mock_cms_url).validate_one("title", "input[[[")
    assert item.status == Status.FAIL and "Invalid selector" in item.notes[0]


def test_nonexistent_selector_reported_missing(browser_page, mock_cms_url):
    item = _v(browser_page, mock_cms_url).validate_one("title", "#does-not-exist")
    assert item.status == Status.NOT_FOUND and item.match_count == 0


def test_ambiguous_selector_needs_review(browser_page, mock_cms_url):
    item = _v(browser_page, mock_cms_url).validate_one("title", "input[type='text']")
    assert item.status == Status.NEEDS_REVIEW and item.match_count == 2
    assert "Ambiguous" in item.notes[0]


def test_wrong_element_kind_fails(browser_page, mock_cms_url):
    item = _v(browser_page, mock_cms_url).validate_one("image", "#headline")
    assert item.status == Status.FAIL and item.interactable is False


def test_null_selector_reported_not_found(browser_page, mock_cms_url):
    item = _v(browser_page, mock_cms_url).validate_one("tags", None, "no tag control observed")
    assert item.status == Status.NOT_FOUND and "no tag control" in item.notes[0]


def test_hidden_element_needs_review(browser_page, mock_cms_url):
    v = _v(browser_page, mock_cms_url)
    browser_page.page.evaluate("document.getElementById('tags').style.display='none'")
    item = v.validate_one("tags", "#tags")
    assert item.status == Status.NEEDS_REVIEW and item.visible is False
