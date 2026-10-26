"""Ollama Cloud access: config, connectivity check, and a CrewAI-compatible LLM.

Talks to {OLLAMA_BASE_URL}/api/chat directly with a Bearer key from the environment.
The key is never logged (see RedactingFilter).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx
from crewai import BaseLLM
from dotenv import load_dotenv
from pydantic import BaseModel

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ollama.com"
DEFAULT_MODEL = "gpt-oss:20b-cloud"
REQUEST_TIMEOUT = 180.0


class OllamaError(RuntimeError):
    """Base class; message is safe to show to the user."""


class OllamaAuthError(OllamaError):
    pass


class OllamaUsageLimitError(OllamaError):
    pass


class OllamaModelUnavailableError(OllamaError):
    pass


class OllamaUnreachableError(OllamaError):
    pass


class OllamaConfig(BaseModel):
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str = ""

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        load_dotenv()
        key = os.getenv("OLLAMA_API_KEY", "").strip()
        if key in {"your_api_key", "your-api-key"}:
            key = ""
        return cls(
            base_url=os.getenv("OLLAMA_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            model=os.getenv("OLLAMA_MODEL", DEFAULT_MODEL),
            api_key=key,
        )


class RedactingFilter(logging.Filter):
    """Strip the API key from any log record."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if self.secret:
            record.msg = str(record.getMessage()).replace(self.secret, "***")
            record.args = ()
        return True


def _headers(cfg: OllamaConfig) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if cfg.api_key:
        h["Authorization"] = f"Bearer {cfg.api_key}"
    return h


def _raise_for_status(resp: httpx.Response, cfg: OllamaConfig) -> None:
    code = resp.status_code
    if code < 400:
        return
    detail = ""
    try:
        detail = str(resp.json().get("error", ""))[:200]
    except Exception:  # noqa: BLE001
        detail = resp.text[:200]
    if code in (401, 403):
        raise OllamaAuthError(
            "Ollama Cloud rejected the credentials. Check OLLAMA_API_KEY in your .env "
            "(create one at https://ollama.com/settings/keys)."
        )
    if code in (402, 429):
        raise OllamaUsageLimitError(
            f"Ollama Cloud usage limit reached ({detail or code}). Wait for the limit to reset or upgrade your plan."
        )
    if code == 404:
        raise OllamaModelUnavailableError(
            f"Model '{cfg.model}' is not available on {cfg.base_url}. Check OLLAMA_MODEL."
        )
    raise OllamaError(f"Ollama returned HTTP {code}: {detail}")


def chat(cfg: OllamaConfig, messages: list[dict[str, str]], *, schema: dict | None = None,
         temperature: float = 0.0, max_tokens: int | None = None) -> str:
    payload: dict[str, Any] = {
        "model": cfg.model, "messages": messages, "stream": False,
        "options": {"temperature": temperature},
    }
    if max_tokens:
        payload["options"]["num_predict"] = max_tokens
    if schema:
        payload["format"] = schema
    try:
        resp = httpx.post(f"{cfg.base_url}/api/chat", json=payload, headers=_headers(cfg),
                          timeout=REQUEST_TIMEOUT)
    except httpx.TimeoutException as exc:
        raise OllamaUnreachableError(f"Timed out waiting for Ollama at {cfg.base_url}.") from exc
    except httpx.HTTPError as exc:
        raise OllamaUnreachableError(
            f"Cannot reach Ollama at {cfg.base_url} ({type(exc).__name__}). Check network and OLLAMA_BASE_URL."
        ) from exc
    _raise_for_status(resp, cfg)
    try:
        return resp.json()["message"]["content"]
    except (KeyError, ValueError) as exc:
        raise OllamaError("Unexpected response shape from Ollama.") from exc


def check_connectivity(cfg: OllamaConfig) -> None:
    """Tiny authenticated request. Raises an OllamaError subclass with a helpful message."""
    if not cfg.api_key:
        raise OllamaAuthError(
            "OLLAMA_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    chat(cfg, [{"role": "user", "content": "Reply with the single word: ok"}], max_tokens=64)


class OllamaCloudLLM(BaseLLM):
    """Minimal CrewAI LLM backed by Ollama Cloud's /api/chat with schema-constrained JSON output."""

    llm_type: str = "ollama_cloud"
    cfg: OllamaConfig

    def __init__(self, cfg: OllamaConfig, **kwargs: Any) -> None:
        super().__init__(model=cfg.model, temperature=0.0, cfg=cfg, **kwargs)

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None):
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        schema = response_model.model_json_schema() if response_model else None
        text = chat(self.cfg, [dict(m) for m in messages], schema=schema)
        logger.debug("LLM returned %d chars", len(text))
        return text

    def supports_function_calling(self) -> bool:
        return False

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 128_000
