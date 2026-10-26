"""Drive the real terminal menu (main.main) with scripted keyboard input."""
import pandas as pd
import pytest

import main
from services.adapters import ROOT, AdapterRegistry, SiteAdapter
from services.secrets import REDACTOR
from tests.fixtures.login_server import EMAIL, PASSWORD


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("HEADLESS", "true")
    monkeypatch.setattr(main, "check_connectivity", lambda cfg: None)
    yield
    REDACTOR.clear()


def _script(monkeypatch, replies: dict, menu: list[str]):
    """input() answers by matching text in the prompt; `Select Option` consumes the menu list in order."""
    menu = list(menu)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if "Select Option" in prompt:
            return menu.pop(0)
        for key, val in replies.items():
            if key in prompt:
                return val
        return "y"
    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def test_menu_shows_options_and_exits(monkeypatch, capsys, tmp_path):
    _script(monkeypatch, {}, ["9", "5"])
    assert main.main(["--db", str(tmp_path / "x.db"), "--workflows", str(tmp_path / "wf")]) == 0
    out = capsys.readouterr().out
    for line in ("MULTI-WEBSITE AUTOMATION SYSTEM", "1. Analyze Website", "2. Upload Content to Real Website",
                 "3. Configure Login Credentials", "4. View Job History", "5. Exit", "Please choose 1-5."):
        assert line in out


def test_configure_credentials_is_memory_only_and_hidden(monkeypatch, capsys, tmp_path):
    asked = []
    monkeypatch.setattr(main.getpass, "getpass", lambda prompt="": (asked.append(prompt) or "pw-from-getpass-1"))
    # menu "3" -> submenu "1" (shared login) -> email -> [hidden password] -> submenu "4" (back) -> menu "5"
    keys = iter(["3", "1", "me@example.com", "4", "5"])
    monkeypatch.setattr("builtins.input", lambda p="": next(keys))
    assert main.main(["--db", str(tmp_path / "x.db"), "--workflows", str(tmp_path / "wf")]) == 0
    out = capsys.readouterr().out
    assert asked and "pw-from-getpass-1" not in out  # asked through getpass, never echoed
    assert "Shared login saved" in out
    assert not list(tmp_path.glob("*"))  # nothing written to disk
    assert not REDACTOR.contains("pw-from-getpass-1")  # store cleared on exit


def test_job_history_on_empty_database(monkeypatch, capsys, tmp_path):
    _script(monkeypatch, {}, ["4", "5"])
    main.main(["--db", str(tmp_path / "none.db"), "--workflows", str(tmp_path / "wf")])
    assert "No jobs yet." in capsys.readouterr().out


def test_upload_through_menu_with_real_login(monkeypatch, capsys, tmp_path, login_server):
    base = SiteAdapter.model_validate_json((ROOT / "adapters" / "mock_cms.json").read_text())
    adir = tmp_path / "adapters"
    adir.mkdir()
    (adir / "l.json").write_text(base.model_copy(update={"name": "local", "url_contains": [], "hosts": ["127.0.0.1"]}).model_dump_json())
    monkeypatch.setattr("services.pipeline.AdapterRegistry", lambda *a, **k: AdapterRegistry([adir, tmp_path / "learned"]))
    book = tmp_path / "jobs.xlsx"
    pd.DataFrame([{"website_url": login_server.base + "/admin/new", "title": "Menu Published Article",
                   "content": "Published through the menu.", "category": "News", "tags": "menu", "images": "",
                   "publish_mode": "publish"}]).to_excel(book, index=False)
    monkeypatch.setattr(main.getpass, "getpass", lambda prompt="": PASSWORD)
    prompts = _script(monkeypatch, {"Excel/CSV path": str(book), "login email": EMAIL, "PUBLISHING": "yes"}, ["2", "5"])
    assert main.main(["--db", str(tmp_path / "jobs.db"), "--workflows", str(tmp_path / "wf")]) == 0
    out = capsys.readouterr().out
    for line in ("[Agent 1] Validating content...", "[Agent 3] Authentication successful.",
                 "[Agent 2] Analyzing page layout and looking for the article-creation page...", "[Agent 4] Verifying results...", "Status: COMPLETED", "Article URL: "):
        assert line in out, line
    assert any("Confirm PUBLISHING" in p for p in prompts)
    assert PASSWORD not in out and EMAIL not in out
    assert login_server.state.login_attempts == 1
    db_bytes = (tmp_path / "jobs.db").read_bytes()
    assert PASSWORD.encode() not in db_bytes and EMAIL.encode() not in db_bytes
    # Job history lists it afterwards.
    _script(monkeypatch, {"run number": "1"}, ["4", "5"])
    main.main(["--db", str(tmp_path / "jobs.db"), "--workflows", str(tmp_path / "wf")])
    assert "Menu Published Article" in capsys.readouterr().out
