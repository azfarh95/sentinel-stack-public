"""Normalised listing schema. Every adapter returns Listing objects, so the
caller (MCP tool, dashboard, bot) never has to care which marketplace produced
the data.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Listing:
    marketplace:  str               # "challenger" | "shopee" | "lazada" | ...
    title:        str
    url:          str
    price_sgd:    float | None = None
    discount_pct: float | None = None      # 0..100
    rating:       float | None = None      # 0..5
    image_url:    str | None = None
    in_stock:     bool | None = None
    vendor:       str | None = None        # brand/seller if known
    captured_at:  str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TelcoPlan:
    """A normalised telco plan tier. Captures both promo and steady-state
    pricing per the SG market reality (promos run 3/6/12 months, then revert).

    Notes from the SG telco probe (2026-05-20):
      - The $0.30/mo platform fee (mandatory since April 2025) is OFTEN
        excluded from headline prices. `platform_fee_included` records
        whether the captured `monthly_sgd` already bakes it in or not.
      - Roaming structures vary too wildly (monthly vs annual vs pooled) to
        normalise — captured as freeform `roaming_note`.
      - For broadband, `speed_mbps` is the headline figure; `data_gb` stays
        None. For mobile, vice-versa.
    """
    carrier:                str             # "eight", "vivifi", "circles", ...
    network:                str             # "singtel" | "starhub" | "m1" (parent)
    category:               str             # "mobile" | "broadband"
    plan_name:              str
    monthly_sgd:            float | None    # current effective price (promo if active)
    monthly_sgd_steady:     float | None = None    # post-promo steady-state, if different
    promo_months:           int | None = None      # how long the promo lasts
    contract_months:        int | None = None      # 0 = no-contract / SIM-only
    data_gb:                float | None = None    # None for broadband. inf for unlimited.
    speed_mbps:             int | None = None      # None for mobile
    free_addons:            list[str] = field(default_factory=list)
    roaming_note:           str | None = None      # freeform — not normalised
    url:                    str = ""
    cis_pdf_url:            str | None = None
    platform_fee_included:  bool = False           # see class docstring
    captured_at:            str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())

    def per_gb_sgd(self) -> float | None:
        """Mobile-side comparison metric. None for broadband or unlimited."""
        if self.category != "mobile" or self.data_gb is None or self.data_gb <= 0:
            return None
        if self.monthly_sgd is None:
            return None
        return round(self.monthly_sgd / self.data_gb, 3)

    def per_mbps_sgd(self) -> float | None:
        """Broadband-side comparison metric."""
        if self.category != "broadband" or not self.speed_mbps:
            return None
        if self.monthly_sgd is None:
            return None
        return round(self.monthly_sgd / self.speed_mbps, 4)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["per_gb_sgd"]   = self.per_gb_sgd()
        d["per_mbps_sgd"] = self.per_mbps_sgd()
        return d


@dataclass
class ShopifyStore:
    """A registered Shopify storefront the adapter can search."""
    domain:        str           # canonical lowercase, no scheme, no trailing slash
    display_name:  str           # human label e.g. "Challenger"
    currency:      str = "SGD"   # most SG-side Shopify stores; verified at detect-time
    enabled:       bool = True
    added_at:      str = field(default_factory=lambda: datetime.now(tz=timezone.utc).isoformat())
    last_seen_at:  str | None = None
    notes:         str = ""

    def base_url(self) -> str:
        return f"https://{self.domain}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
