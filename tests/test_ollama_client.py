import httpx
import pytest

from llm import ollama_client as oc

CFG = oc.OllamaConfig(base_url="https://ollama.test", model="gpt-oss:20b-cloud", api_key="sk-secret")


@pytest.mark.parametrize("status,exc", [
    (401, oc.OllamaAuthError), (403, oc.OllamaAuthError),
    (429, oc.OllamaUsageLimitError), (404, oc.OllamaModelUnavailableError),
])
def test_http_errors_are_mapped(monkeypatch, status, exc):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(status, json={"error": "x"}))
    with pytest.raises(exc) as e:
        oc.check_connectivity(CFG)
    assert "sk-secret" not in str(e.value)


def test_missing_key():
    with pytest.raises(oc.OllamaAuthError):
        oc.check_connectivity(oc.OllamaConfig(api_key=""))


def test_network_error(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("nope")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(oc.OllamaUnreachableError):
        oc.check_connectivity(CFG)


def test_success_sends_bearer_and_schema(monkeypatch):
    seen = {}

    def fake(url, json=None, headers=None, timeout=None):
        seen.update(url=url, json=json, headers=headers)
        return httpx.Response(200, json={"message": {"content": "{}"}})
    monkeypatch.setattr(httpx, "post", fake)
    assert oc.chat(CFG, [{"role": "user", "content": "hi"}], schema={"type": "object"}) == "{}"
    assert seen["headers"]["Authorization"] == "Bearer sk-secret"
    assert seen["json"]["format"] == {"type": "object"} and seen["url"].endswith("/api/chat")
