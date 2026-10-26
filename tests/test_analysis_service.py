import json

import pytest

from llm.schemas import AnalysisProposal
from services.analysis_service import (AnalysisService, ServiceIO, UnsafeTargetError, validate_url)
from tests.test_schema import GOOD

IO = ServiceIO(ask=lambda _: "", info=lambda m: None, warn=lambda m: None)


def _svc(tmp_path, proposal):
    return AnalysisService(lambda obs: AnalysisProposal.model_validate(proposal), IO, headless=True,
                           output_dir=tmp_path / "out", screenshot_dir=tmp_path / "shots", allow_local=True)


def test_end_to_end_writes_json(tmp_path, mock_cms_url):
    seen = {}
    svc = AnalysisService(lambda obs: seen.setdefault("obs", obs) and AnalysisProposal.model_validate(
        {**GOOD, "fields": {**GOOD["fields"], "tags": {"selector": "#tags", "confidence": 0.8}}}),
        IO, headless=True, output_dir=tmp_path / "out", screenshot_dir=tmp_path / "shots", allow_local=True)
    result = svc.run(mock_cms_url)
    assert result.status == "COMPLETED"
    data = json.loads((tmp_path / "out" / "website_analysis.json").read_text())
    assert data["ai_proposal"]["fields"]["title"]["selector"] == "#headline"
    assert {i["name"]: i["status"] for i in data["browser_validation"]}["title"] == "PASS"
    assert (tmp_path / "shots" / "website_analysis.png").exists()
    assert "SECRET-VALUE" not in json.dumps(seen["obs"])


def test_missing_and_bad_selectors_downgrade_status(tmp_path, mock_cms_url):
    bad = {**GOOD, "fields": {**GOOD["fields"], "image": {"selector": "#nope", "confidence": 0.9}}}
    r = _svc(tmp_path, bad).run(mock_cms_url)
    st = {i.name: i.status.value for i in r.browser_validation}
    assert st["image"] == "NOT_FOUND" and st["tags"] == "NOT_FOUND"
    assert r.status == "COMPLETED_WITH_WARNINGS"

    ambiguous = {**GOOD, "fields": {**GOOD["fields"], "title": {"selector": "input[type='text']", "confidence": 0.9}}}
    assert _svc(tmp_path, ambiguous).run(mock_cms_url).status == "NEEDS_REVIEW"


def test_non_editor_page_needs_review(tmp_path, login_url):
    p = {**GOOD, "page_type": "login", "next_step_suggestion": "Log in then open the new-article page"}
    assert _svc(tmp_path, p).run(login_url).status == "NEEDS_REVIEW"


@pytest.mark.parametrize("bad", ["", "ftp://x.com", "https://user:pw@x.com", "file:///etc/passwd"])
def test_unsafe_urls_rejected(bad):
    with pytest.raises(UnsafeTargetError):
        validate_url(bad)


def test_unauthorized_remote_target_aborts(tmp_path):
    svc = AnalysisService(lambda o: None, ServiceIO(ask=lambda _: "n", info=print, warn=print),
                          headless=True, output_dir=tmp_path)
    with pytest.raises(UnsafeTargetError):
        svc.run("https://example.com/admin")
