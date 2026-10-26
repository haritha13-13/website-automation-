from browser.locator_validator import LocatorValidator
from services.adapters import AdapterRegistry, SiteAdapter
from services.models import ExecutionPlan
from llm.schemas import Status


def test_registry_finds_mock_adapter_and_ignores_bad_files(tmp_path):
    (tmp_path / "bad.json").write_text("{not json")
    (tmp_path / "ok.json").write_text(SiteAdapter(name="x", hosts=["cms.example.com"], fields={"title": "#t"}).model_dump_json())
    reg = AdapterRegistry([tmp_path])
    assert [a.name for a in reg.adapters] == ["x"]
    assert reg.find("https://cms.example.com/new").name == "x" and reg.find("https://other.com") is None
    assert AdapterRegistry().find("file:///a/mock_cms_publish.html").name == "mock_cms"


def test_adapter_proposal_validates_against_page(browser_page, publish_url):
    browser_page.navigate(publish_url)
    adapter = AdapterRegistry().find(publish_url)
    items = LocatorValidator(browser_page.page).validate(adapter.to_proposal())
    assert {i.name: i.status for i in items} == {n: Status.PASS for n in
            ("title", "content", "category", "tags", "image", "save_draft", "publish")}


def test_save_learned_roundtrip(tmp_path):
    reg = AdapterRegistry([tmp_path])
    plan = ExecutionPlan(website_type="custom_cms", source="ollama", validation_status="passed",
                         field_mapping={"title": "#a", "content": "#b"}, actions={"save_draft": "#c"})
    path = reg.save_learned(plan, "https://news.example.org/admin/new")
    assert path.exists() and reg.find("https://news.example.org/other").fields["title"] == "#a"
