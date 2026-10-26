import json

from browser.page_inspector import PageInspector


def _inspect(mgr, url):
    mgr.navigate(url)
    return PageInspector(mgr.page).inspect()


def test_detects_all_mock_cms_fields(browser_page, mock_cms_url):
    obs = _inspect(browser_page, mock_cms_url)
    by_id = {e.id: e for e in obs.elements}
    assert by_id["headline"].kind == "input" and by_id["headline"].labels == ["Article Title"]
    assert by_id["headline"].placeholder == "Enter headline"
    assert by_id["article-body"].kind == "contenteditable"
    assert by_id["article-body"].labels == ["Article Body"]
    assert by_id["category"].kind == "select" and "News" in by_id["category"].options
    assert by_id["tags"].kind == "input"
    assert by_id["cover"].kind == "file" and by_id["cover"].accept == "image/*"
    assert by_id["save-draft"].kind == "button" and by_id["save-draft"].text == "Save Draft"
    assert by_id["publish"].text == "Publish"


def test_structure_title_url_links_sections(browser_page, mock_cms_url):
    obs = _inspect(browser_page, mock_cms_url)
    assert obs.title == "Mock CMS - New Article"
    assert obs.url == mock_cms_url
    assert {l.text for l in obs.links} == {"Posts", "Media"}
    assert any(s.tag == "form" for s in obs.sections)
    json.dumps(obs.model_dump())  # serializable


def test_dynamic_element_is_waited_for(browser_page, mock_cms_url):
    mgr = browser_page
    mgr.navigate(mock_cms_url)
    mgr.page.wait_for_selector("#preview", timeout=5000)
    obs = PageInspector(mgr.page).inspect()
    assert any(e.id == "preview" for e in obs.elements)


def test_sensitive_data_is_not_extracted(browser_page, mock_cms_url, login_url):
    obs = _inspect(browser_page, mock_cms_url)
    payload = json.dumps(obs.for_llm())
    assert "SECRET-VALUE" not in payload and "TOPSECRET" not in payload
    assert "csrf" not in payload and "abc123" not in payload  # hidden input + link query

    login = _inspect(browser_page, login_url)
    assert login.has_password_field
    assert "hunter2" not in json.dumps(login.for_llm())
    assert all(e.type != "password" for e in login.elements)


def test_screenshot(browser_page, mock_cms_url, tmp_path):
    browser_page.navigate(mock_cms_url)
    path = PageInspector(browser_page.page, tmp_path).take_screenshot("t.png")
    assert path.exists() and path.stat().st_size > 0
