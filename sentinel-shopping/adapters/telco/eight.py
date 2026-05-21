"""eight.com.sg — StarHub MVNO. Server-rendered with semantic CSS classes.

HTML structure (verified 2026-05-20):
  <article class="card-plan">
    <div class="card-plan__inner">
      <div class="card-plan__box">
        <div class="card-plan__header">
          <div class="card-plan__network">5G</div>
          <div class="card-plan__quota">528GB</div>
          <div class="card-plan__name">Value Eight Plan</div>
          <div class="card-plan__price card-plan__price--monthly">
            <span class="card-plan__price-text">$10.80</span>
            <span class="card-plan__price-duration">/mth</span>
          </div>
        </div>

Plans are duplicated in monthly and yearly tabs; the yearly tab uses
`card-plan__price--yearly` — we skip those and only return monthly plans.

Categories are encoded on the parent `<li class="swiper-slide ... cat-NN">`:
  cat-23 = Mobile Plans (standard)
  cat-24 = Data-Only Plans
  cat-25 = Senior Plans
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

from bs4 import BeautifulSoup

from schema import TelcoPlan
from .base import HttpxCarrier, parse_price, parse_data_gb

logger = logging.getLogger(__name__)


CATEGORY_LABELS = {
    "cat-23": "Mobile",
    "cat-24": "Data-Only",
    "cat-25": "Senior",
}


class EightMobile(HttpxCarrier):
    name      = "eight"
    network   = "starhub"
    category  = "mobile"
    home_url  = "https://www.eight.com.sg/"
    plans_url = "https://www.eight.com.sg/mobile/"

    async def fetch_plans(self, *, pool=None) -> list[TelcoPlan]:
        html = await self._get(self.plans_url)
        soup = BeautifulSoup(html, "lxml")

        plans: list[TelcoPlan] = []
        seen: set[tuple[str, float, float]] = set()  # (name, price, data_gb)

        for article in soup.select("article.card-plan"):
            price_block = article.select_one("div.card-plan__price")
            if price_block is None:
                continue
            # Skip yearly cards — same plans appear in both tabs
            if "card-plan__price--yearly" in (price_block.get("class") or []):
                continue

            price_text_el = price_block.select_one("span.card-plan__price-text")
            if price_text_el is None:
                continue
            price = parse_price(price_text_el.get_text(" ", strip=True))
            if price is None:
                continue

            quota_el = article.select_one("div.card-plan__quota")
            name_el  = article.select_one("div.card-plan__name")
            data_gb  = parse_data_gb(quota_el.get_text(" ", strip=True)) if quota_el else None
            plan_name = name_el.get_text(" ", strip=True) if name_el else ""

            network_el = article.select_one("div.card-plan__network")
            network_tag = network_el.get_text(" ", strip=True) if network_el else ""

            # Category from parent <li>
            parent_li = article.find_parent("li")
            sub_category = "Mobile"
            if parent_li is not None:
                for cls in parent_li.get("class") or []:
                    if cls in CATEGORY_LABELS:
                        sub_category = CATEGORY_LABELS[cls]
                        break

            # Buy-now URL
            buy_a = article.select_one("a.button--filled, a.button--primary, a[href*='choose-number']")
            url = buy_a.get("href") if buy_a is not None else self.plans_url

            # Roaming note from details
            roaming_parts: list[str] = []
            for li in article.select("li.card-plan__detail"):
                t = li.get_text(" ", strip=True)
                if "roaming" in t.lower():
                    roaming_parts.append(t)
            roaming_note = " | ".join(roaming_parts) if roaming_parts else None

            # Free addons — IDD mins, talk-time, SMS
            free_addons = []
            for li in article.select("li.card-plan__detail"):
                t = li.get_text(" ", strip=True)
                low = t.lower()
                if any(kw in low for kw in ("talktime", "idd", "sms", "esim", "physical sim")):
                    free_addons.append(t)

            display_name = f"{plan_name} ({sub_category}, {network_tag})".strip(" ,()")
            key = (display_name, price, float(data_gb) if data_gb is not None and data_gb != float("inf") else -1)
            if key in seen:
                continue
            seen.add(key)

            plans.append(TelcoPlan(
                carrier=self.name,
                network=self.network,
                category=self.category,
                plan_name=display_name,
                monthly_sgd=price,
                data_gb=data_gb,
                contract_months=0,  # eight is SIM-only / no-contract
                free_addons=free_addons,
                roaming_note=roaming_note,
                url=url if url and url.startswith("http") else f"https://www.eight.com.sg{url or ''}",
                cis_pdf_url="https://www.eight.com.sg/critical-information-summary/",
                platform_fee_included=False,  # headline excludes $0.30 fee
            ))

        logger.info("eight: extracted %d plans (monthly only)", len(plans))
        return plans
