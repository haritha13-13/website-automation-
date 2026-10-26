import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture()
def browser_page():
    from browser.playwright_manager import PlaywrightManager

    with PlaywrightManager(headless=True) as mgr:
        yield mgr


@pytest.fixture()
def mock_cms_url():
    return (FIXTURES / "mock_cms.html").as_uri()


@pytest.fixture()
def login_url():
    return (FIXTURES / "login_page.html").as_uri()


# ---- helpers for the multi-agent pipeline tests ------------------------------------------------
import base64

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@pytest.fixture()
def png(tmp_path):
    p = tmp_path / "images"
    p.mkdir()
    f = p / "automation.png"
    f.write_bytes(PNG)
    return f


@pytest.fixture()
def publish_url():
    return (FIXTURES / "mock_cms_publish.html").as_uri()


@pytest.fixture()
def site_b_url():
    return (FIXTURES / "mock_site_b.html").as_uri()


@pytest.fixture()
def login_server():
    from tests.fixtures.login_server import LoginServer
    with LoginServer() as srv:
        yield srv
