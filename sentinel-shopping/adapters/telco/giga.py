"""giga! (giga.com.sg) — StarHub sub-brand. Server-rendered.

HTML structure (verified 2026-05-21):
  <div class="PlanDisplayCardsWrapper">
    <div>{regions} {discount}% Savings {data}GB FOR ${price} MONTHLY
         {features incl. 4G/5G, calls, SMS, roaming, perks}</div>
    ...

The regex parses the canonical "{data}GB FOR ${price} MONTHLY" anchor —
robust to ordering of region tags / promo badges around it. Plans without
that anchor (banners, FAQ items) get filtered.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from schema import TelcoPlan
from .base import HttpxCarrier

logger = logging.getLogger(__name__)


# "100GB FOR $10.90 MONTHLY" or "1TB FOR $28.90 MONTHLY"
_PLAN_ANCHOR = re.compile(
    r"(\d+(?:\.\d+)?)\s*(GB|TB)\s+FOR\s+\$(\d+(?:\.\d+)?)\s+MONTHLY",
    re.IGNORECASE,
)
_NETWORK = re.compile(r"\b(5G|4G)\s+Speeds?", re.IGNORECASE)
_ROAMING = re.compile(r"(\d+(?:\.\d+)?)\s*(GB|TB)\s+Asia\s+Roaming", re.IGNORECASE)
_CALLS_SMS = re.compile(
    r"(\d+(?:,\d{3})*)\s+SMS\s*,\s*(\d+(?:,\d{3})*)\s+mins?",
    re.IGNORECASE,
)


class GigaMobile(HttpxCarrier):
    name      = "giga"
    network   = "starhub"
    category  = "mobile"
    home_url  = "https://giga.com.sg/"
    plans_url = "https://giga.com.sg/"

    async def fetch_plans(self, *, pool=None) -> list[TelcoPlan]:
        html = await self._get(self.plans_url)
        soup = BeautifulSoup(html, "lxml")

        plans: list[TelcoPlan] = []
        seen: set[tuple[float, float, str]] = set()

        for wrapper in soup.select("div.PlanDisplayCardsWrapper"):
            for card in wrapper.find_all("div", recursive=False):
                text = " ".join(card.get_text(" ", strip=True).split())
                m = _PLAN_ANCHOR.search(text)
                if not m:
                    continue
                data_val   = float(m.group(1))
                data_unit  = m.group(2).upper()
                price      = float(m.group(3))
                data_gb    = data_val * (1024.0 if data_unit == "TB" else 1.0)

                # Network: look for "5G Speeds" or "4G Speeds" in the card text
                net_match = _NETWORK.search(text)
                network_tag = net_match.group(1).upper() if net_match else ""

                # Build a deduped key — same (data, price, network) is the same plan
                key = (data_gb, price, network_tag)
                if key in seen:
                    continue
                seen.add(key)

                # Roaming
                rm = _ROAMING.search(text)
                roaming_note: str | None = None
                if rm:
                    roaming_note = f"{rm.group(1)}{rm.group(2)} Asia Roaming"

                # Free addons (calls/SMS)
                addons: list[str] = []
                cs = _CALLS_SMS.search(text)
                if cs:
                    addons.append(f"{cs.group(1)} SMS · {cs.group(2)} mins")
                if "Free Rollover" in text or "rollover" in text.lower():
                    addons.append("Free Rollover Data (up to 2x cap)")

                # Plan name: synthesised since giga doesn't use named tiers
                plan_label = f"{data_val:g}{data_unit}" + (f" {network_tag}" if network_tag else "")

                plans.append(TelcoPlan(
                    carrier=self.name,
                    network=self.network,
                    category=self.category,
                    plan_name=plan_label,
                    monthly_sgd=price,
                    data_gb=data_gb,
                    contract_months=0,    # no-contract SIM-only
                    free_addons=addons,
                    roaming_note=roaming_note,
                    url=self.plans_url,
                    platform_fee_included=False,
                ))

        logger.info("giga: extracted %d plans", len(plans))
        return plans
