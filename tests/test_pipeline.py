import json

import pandas as pd
import pytest

from agents.website_analysis_agent import WebsiteAnalysisAgent
from llm.ollama_client import OllamaConfig
from llm.schemas import AnalysisProposal
from services.adapters import ROOT, AdapterRegistry
from services.analysis_service import ServiceIO
from services.content_service import PreparationError
from services.pipeline import AutomationPipeline, PipelineOptions

SITE_B = {
    "website_type": "forum", "page_type": "article_editor",
    "fields": {"title": {"selector": "#subject", "confidence": 0.9}, "content": {"selector": "#msg", "confidence": 0.9},
               "category": {"selector": "select[name='topic']", "confidence": 0.8},
               "tags": {"selector": "#kw", "confidence": 0.7},
               "image": {"selector": "#att", "confidence": 0.8}},
    "actions": {"save_draft": {"selector": "button:has-text('Store draft')"},
                "publish": {"selector": "button:has-text('Go live')"}},
}


class Terminal:
    def __init__(self, answers=None):
        self.answers, self.prompts, self.log = answers or {}, [], []

    def ask(self, prompt):
        self.prompts.append(prompt)
        for key, val in self.answers.items():
            if key in prompt:
                return val
        return "y"

    def io(self):
        return ServiceIO(ask=self.ask, info=self.log.append, warn=self.log.append)


def _workbook(tmp_path, png, publish_url, site_b_url):
    rows = [
        {"website_url": publish_url, "title": "Draft one", "content": "Body of the draft", "category": "technology",
         "tags": "AI, Automation", "images": str(png), "publish_mode": "draft"},
        {"website_url": publish_url, "title": "Publish two", "content": "Body of the published article",
         "category": "News", "tags": "", "images": "", "publish_mode": "publish"},
        {"website_url": publish_url, "title": "Broken", "content": "", "category": "", "tags": "", "images": "",
         "publish_mode": "draft"},
        {"website_url": site_b_url, "title": "Other site", "content": "Body for site B", "category": "News",
         "tags": "x", "images": "", "publish_mode": "draft"},
    ]
    p = tmp_path / "content.xlsx"
    pd.DataFrame(rows).to_excel(p, index=False)
    return p


def _pipeline(tmp_path, term, ollama=True, learned=None):
    cfg = OllamaConfig(api_key="k")
    agent = WebsiteAnalysisAgent(cfg, analyzer=lambda obs: AnalysisProposal.model_validate(SITE_B))
    registry = AdapterRegistry([ROOT / "adapters", learned or tmp_path / "learned"])
    opts = PipelineOptions(allow_local=True, headless=True, db_path=tmp_path / "t.db", output_dir=tmp_path / "out", workflow_dir=tmp_path / "workflows")
    return AutomationPipeline(cfg, term.io(), opts, analysis_agent=agent, registry=registry, ollama_available=ollama)


def test_full_four_agent_run_and_duplicate_protection(tmp_path, png, publish_url, site_b_url):
    book = _workbook(tmp_path, png, publish_url, site_b_url)
    term = Terminal()
    pipe = _pipeline(tmp_path, term)
    report = pipe.run(str(book))
    st = {i["row_number"]: i for i in report["items"]}
    assert st[2]["status"] == "draft_saved" and st[3]["status"] == "published" and st[5]["status"] == "draft_saved"
    assert st[3]["article_url"].startswith(publish_url + "#article=")
    assert all(c["status"] != "FAIL" for i in st.values() for c in i["verification"]["checks"])
    assert report["summary"]["invalid_rows"] == 1 and report["invalid_rows"][0]["row_number"] == 4
    assert report["summary"]["published"] == 1 and report["summary"]["drafts_saved"] == 2
    sites = {s["source"] for s in report["websites"]}
    assert sites == {"adapter", "ollama"}
    # approval prompts: 2 drafts + publish + site-b draft, mapping confirmation, save-adapter question
    assert sum("PUBLISH" in p for p in term.prompts) == 1
    assert any("AI-proposed mapping" in p for p in term.prompts)
    saved = json.loads((tmp_path / "out" / "execution_report_run1.json").read_text())
    assert saved["summary"]["published"] == 1
    pipe.close()

    # Same file again: nothing is re-sent; the learned adapter is reused instead of asking the model.
    term2 = Terminal()
    pipe2 = _pipeline(tmp_path, term2)
    report2 = pipe2.run(str(book))
    assert {i["row_number"]: i["status"] for i in report2["items"]} == {2: "duplicate_skipped", 3: "duplicate_skipped", 5: "duplicate_skipped"}
    assert report2["summary"]["duplicates_skipped"] == 3 and report2["summary"]["published"] == 0
    assert not any("PUBLISH" in p for p in term2.prompts)
    pipe2.close()


def test_user_can_decline_publish_and_ai_mapping(tmp_path, png, publish_url, site_b_url):
    book = _workbook(tmp_path, png, publish_url, site_b_url)
    term = Terminal({"PUBLISH": "n", "AI-proposed": "n"})
    pipe = _pipeline(tmp_path, term)
    report = pipe.run(str(book))
    st = {i["row_number"]: i for i in report["items"]}
    assert st[3]["status"] == "rejected_by_user" and st[2]["status"] == "draft_saved"
    assert st[5]["status"] == "analysis_failed"
    pipe.close()


def test_ollama_unavailable_still_runs_known_adapters(tmp_path, png, publish_url, site_b_url):
    book = _workbook(tmp_path, png, publish_url, site_b_url)
    pipe = _pipeline(tmp_path, Terminal(), ollama=False)
    report = pipe.run(str(book))
    st = {i["row_number"]: i["status"] for i in report["items"]}
    assert st == {2: "draft_saved", 3: "published", 5: "analysis_failed"}
    pipe.close()


def test_unusable_file_raises_clear_error(tmp_path):
    term = Terminal()
    pipe = _pipeline(tmp_path, term)
    with pytest.raises(PreparationError):
        pipe.run(str(tmp_path / "missing.xlsx"))
    assert sum("[Agent 1]" in m for m in term.log) == 1  # failed once, not retried
    assert not any("[Agent 2]" in m for m in term.log)
    pipe.close()
