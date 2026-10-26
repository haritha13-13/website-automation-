"""In-memory credentials and log/report redaction. Nothing here is ever written to disk."""
from __future__ import annotations

import logging
import threading
from urllib.parse import quote, quote_plus
from dataclasses import dataclass, field

_MIN_SECRET_LEN = 3


class Redactor:
    """Replaces every registered secret in any text we are about to print, log, or store."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add(self, *values: str) -> None:
        with self._lock:
            for v in values:
                if v and len(v) >= _MIN_SECRET_LEN:
                    # URLs carry secrets percent-encoded (an email becomes name%40example.com).
                    self._secrets |= {v, quote(v, safe=""), quote_plus(v), quote(v, safe="@")}

    def clear(self) -> None:
        with self._lock:
            self._secrets.clear()

    def redact(self, text: str) -> str:
        if not text:
            return text
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for s in secrets:
            text = text.replace(s, "***")
        return text

    def contains(self, text: str) -> bool:
        with self._lock:
            return any(s in text for s in self._secrets)


REDACTOR = Redactor()


class RedactingLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = REDACTOR.redact(str(record.getMessage()))
        record.args = ()
        return True


@dataclass(frozen=True)
class Credentials:
    email: str
    password: str = field(repr=False)

    def __post_init__(self) -> None:
        REDACTOR.add(self.email, self.password)

    def __repr__(self) -> str:  # never show either value
        return "Credentials(<redacted>)"

    __str__ = __repr__


class CredentialStore:
    """Shared login plus optional per-domain logins. Memory only for the life of the process."""

    def __init__(self) -> None:
        self.shared: Credentials | None = None
        self.by_host: dict[str, Credentials] = {}

    def set_shared(self, email: str, password: str) -> None:
        self.shared = Credentials(email.strip(), password)

    def set_for_host(self, host: str, email: str, password: str) -> None:
        self.by_host[host.lower()] = Credentials(email.strip(), password)

    def for_host(self, host: str) -> tuple[Credentials | None, str]:
        """(credentials, 'site' | 'shared' | 'none')"""
        host = host.lower()
        if host in self.by_host:
            return self.by_host[host], "site"
        if self.shared:
            return self.shared, "shared"
        return None, "none"

    def clear(self) -> None:
        self.shared, self.by_host = None, {}
        REDACTOR.clear()

    @property
    def configured(self) -> bool:
        return bool(self.shared or self.by_host)
