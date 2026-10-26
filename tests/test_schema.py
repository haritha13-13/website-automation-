import pytest
from pydantic import ValidationError

from llm.schemas import AnalysisProposal

GOOD = {
    "website_type": "custom_cms", "page_type": "article_editor",
    "fields": {
        "title": {"selector": "#headline", "confidence": 0.98},
        "content": {"selector": "#article-body", "confidence": 0.95},
        "category": {"selector": "#category", "confidence": 0.9},
        "tags": {"selector": None, "confidence": 0, "reason": "no tag control observed"},
        "image": {"selector": "#cover", "confidence": 0.96},
    },
    "actions": {"save_draft": {"selector": "#save-draft"}, "publish": {"selector": "#publish"}},
    "missing_fields": ["tags"], "requires_human_review": False,
}


def test_valid_response_parses():
    p = AnalysisProposal.model_validate(GOOD)
    assert p.fields.tags.selector is None and p.fields.title.confidence == 0.98


def test_null_selector_requires_reason():
    bad = {**GOOD, "fields": {**GOOD["fields"], "tags": {"selector": None, "confidence": 0}}}
    with pytest.raises(ValidationError):
        AnalysisProposal.model_validate(bad)


def test_confidence_range_and_page_type_enforced():
    with pytest.raises(ValidationError):
        AnalysisProposal.model_validate({**GOOD, "fields": {**GOOD["fields"], "title": {"selector": "#a", "confidence": 1.5}}})
    with pytest.raises(ValidationError):
        AnalysisProposal.model_validate({**GOOD, "page_type": "banana"})


def test_missing_sections_rejected():
    with pytest.raises(ValidationError):
        AnalysisProposal.model_validate({"website_type": "x", "page_type": "other"})


def test_json_roundtrip():
    p = AnalysisProposal.model_validate_json(AnalysisProposal.model_validate(GOOD).model_dump_json())
    assert p.actions.publish.selector == "#publish"
