"""Sentinel Finance — runtime settings stored in /finance/settings.yaml.

Read with `get()` / `get_all()`. Write via `save()` (round-trips YAML).

Helpers:
  format_date(iso_str) -> str   Apply date_format to a YYYY-MM-DD string.
  youragency_rate()              Default pay per shift (float).
  youragency_pending_factor()    Confidence factor for "pending" events.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

SETTINGS_PATH = Path("/finance/settings.yaml")

_DEFAULTS = {
    "date_format": "dd-MM",
    "timezone": "Asia/Singapore",
    "youragency": {
        "default_pay_per_shift": 120.00,
        "pending_factor": 0.5,
    },
    # Chain Dust Threshold (USD). Moralis tokens worth less than this are
    # hidden from the balance sheet. Default $0.01 catches spam/airdrop dust.
    "dust_usd": 0.01,
    # Home glance card visibility + order. Edit via /config/glance.
    "glance_cards": [
        {"key": "bank",      "enabled": True, "order": 1},
        {"key": "crypto",    "enabled": True, "order": 2},
        {"key": "ilp",       "enabled": True, "order": 3},
        {"key": "cpf",       "enabled": True, "order": 4},
        {"key": "loans",     "enabled": True, "order": 5},
        {"key": "cc",        "enabled": True, "order": 6},
        {"key": "recurring", "enabled": True, "order": 7},
        {"key": "pending",   "enabled": True, "order": 8},
        {"key": "networth",  "enabled": True, "order": 9},
    ],
}

# Static card catalog — what each key renders (label, drill_key).
# Stored here (not in settings.yaml) so the user only edits visibility/order.
GLANCE_CATALOG = {
    "bank":      {"label": "Bank Balance",      "drill": "bank"},
    "crypto":    {"label": "Crypto Holdings",   "drill": "crypto"},
    "ilp":       {"label": "ILP Investments",   "drill": "ilp"},
    "cpf":       {"label": "CPF (incl. IS)",    "drill": "cpf"},
    "loans":     {"label": "Total Loans",       "drill": "loans"},
    "cc":        {"label": "Total CC",          "drill": "cc"},
    "recurring": {"label": "Monthly Recurring", "drill": "recurring"},
    "pending":   {"label": "Pending Reconciliation", "drill": "pending"},
    "networth":  {"label": "Net Worth",         "drill": None},
}


def glance_cards_ordered() -> list[dict]:
    """Return enabled glance cards in user-defined order.
    Each item: {key, label, drill, enabled, order}.
    """
    cfg = get_all().get("glance_cards") or _DEFAULTS["glance_cards"]
    out = []
    for c in cfg:
        if not c.get("enabled", True):
            continue
        key = c.get("key")
        meta = GLANCE_CATALOG.get(key, {})
        out.append({
            "key": key,
            "label": meta.get("label", key),
            "drill": meta.get("drill"),
            "order": c.get("order", 99),
        })
    out.sort(key=lambda x: x.get("order", 99))
    return out

DATE_FORMATS = ("dd-MM", "MM-dd", "dd MMM", "yyyy-MM-dd")

TIMEZONES = (
    "Asia/Singapore",
    "Asia/Kuala_Lumpur",
    "Asia/Hong_Kong",
    "Asia/Tokyo",
    "Asia/Dubai",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Los_Angeles",
    "UTC",
)


def get_all() -> dict:
    try:
        data = yaml.safe_load(SETTINGS_PATH.read_text()) or {}
    except FileNotFoundError:
        data = {}
    # Shallow merge with defaults (one level deep for youragency sub-dict)
    out = dict(_DEFAULTS)
    out.update({k: v for k, v in data.items() if k != "youragency"})
    if "youragency" in data and isinstance(data["youragency"], dict):
        merged = dict(_DEFAULTS["youragency"])
        merged.update(data["youragency"])
        out["youragency"] = merged
    return out


def get(key: str, default: Any = None) -> Any:
    return get_all().get(key, default)


def save(updates: dict) -> dict:
    """Merge `updates` into settings.yaml. Returns merged result."""
    current = get_all()
    for k, v in updates.items():
        if k == "youragency" and isinstance(v, dict):
            cur = current.get("youragency") or {}
            cur.update(v)
            current["youragency"] = cur
        else:
            current[k] = v
    SETTINGS_PATH.write_text(yaml.safe_dump(current, sort_keys=False, default_flow_style=False))
    return current


def dust_usd() -> float:
    """Chain dust threshold in USD — tokens worth less are hidden."""
    return float(get_all().get("dust_usd", 0.01))


def youragency_rate() -> float:
    return float(get_all().get("youragency", {}).get("default_pay_per_shift", 120.00))


def youragency_pending_factor() -> float:
    return float(get_all().get("youragency", {}).get("pending_factor", 0.5))


def format_date(iso_str: str) -> str:
    """Format a 'YYYY-MM-DD' string using configured date_format.

    Falls back to dd-MM on unrecognized format.
    """
    fmt = get_all().get("date_format", "dd-MM")
    try:
        d = datetime.strptime(iso_str[:10], "%Y-%m-%d").date()
    except Exception:
        return iso_str
    return _apply_format(d, fmt)


def _apply_format(d: date, fmt: str) -> str:
    if fmt == "MM-dd":
        return d.strftime("%m-%d")
    if fmt == "dd MMM":
        return d.strftime("%d %b")
    if fmt == "yyyy-MM-dd":
        return d.strftime("%Y-%m-%d")
    # default
    return d.strftime("%d-%m")
