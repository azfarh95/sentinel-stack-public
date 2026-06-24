"""P4.3 — domain guard for the browser assistant.

Classifies a URL's host:
  • 'blocked'   → auto-deny navigation (a hard no-go list; default empty).
  • 'sensitive' → require approval even to NAVIGATE (mail / bank / identity / the
                  owner's finance surfaces). State-changes there are already gated.
  • 'normal'    → navigate freely; state-changes still gated by P4.1.

Conservative substring matching (over-match → more prompts → safe). Overridable
via browser-assistant/domain_policy.json: {"sensitive": [...], "blocked": [...]}.
"""
import json
from pathlib import Path
from urllib.parse import urlparse

_DEFAULT_SENSITIVE = [
    # identity / login
    "accounts.google.com", "login.", "signin.", "/login", "id.",
    # mail
    "mail.google.com", "gmail.com", "outlook.", "mail.",
    # banking / payments / crypto
    "bank", "chase.com", "wellsfargo.com", "paypal.com", "stripe.com",
    "coinbase.com", "binance.", "crypto.com", "kraken.com", "revolut.",
    # the owner's own finance / defi surfaces
    "sentinelfinance", "defi.sentinel",
]
_DEFAULT_BLOCKED: list[str] = []


def _host(url) -> str:
    try:
        return (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return ""


def domain_of(url) -> str:
    return _host(url) or "?"


class DomainPolicy:
    def __init__(self, sensitive=None, blocked=None):
        self.sensitive = list(sensitive if sensitive is not None else _DEFAULT_SENSITIVE)
        self.blocked = list(blocked if blocked is not None else _DEFAULT_BLOCKED)

    @classmethod
    def load(cls, path=None):
        p = Path(path) if path else Path(__file__).resolve().parent / "domain_policy.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return cls(d.get("sensitive"), d.get("blocked"))
        except Exception:
            return cls()

    @staticmethod
    def _match(host: str, patterns) -> bool:
        return any(pat in host for pat in patterns)

    def classify(self, url) -> str:
        host = _host(url)
        if not host:
            return "normal"
        if self._match(host, self.blocked):
            return "blocked"
        if self._match(host, self.sensitive):
            return "sensitive"
        return "normal"
