"""Circles.Life (M1 MVNO) — Next.js SPA. Needs nodriver to render.

Verified 2026-05-21: rendered DOM has nodes with `data-plan="<number>"`
attributes. Each unique data-plan value is one plan tier; the node's
innerText carries everything we need:

  "5g MY, ASIA, GLOBAL 600 GB $14.88"
  "5g ASIA 300 GB $12.88"
  "5g 200 GB $12"
  "4g ASIA 350 GB $10.80"
  "5g MY, ASIA, GLOBAL 2 TB $32.00"

Regex: optional 4g/5g → optional region words → data → unit → $price.
"""
from __future__ import annotations

import logging
import re

from schema import TelcoPlan
from .base import NodriverCarrier

logger = logging.getLogger(__name__)


_TIER_RE = re.compile(
    r"""
    (?P<net>4g|5g)?       \s*
    (?P<regions>(?:[A-Z]+(?:,\s*[A-Z]+)*\b)?)  \s*
    (?P<data>\d+(?:\.\d+)?) \s*
    (?P<unit>GB|TB)        \s*
    \$\s*(?P<price>\d+(?:\.\d+)?)
    """,
    re.IGNORECASE | re.VERBOSE,
)


class CirclesMobile(NodriverCarrier):
    name      = "circles"
    network   = "m1"
    category  = "mobile"
    home_url  = "https://www.circles.life/sg/"
    plans_url = "https://www.circles.life/sg/sim-only-plans/"
    wait_seconds = 10.0   # SPA hydration

    def extract(self, soup) -> list[TelcoPlan]:
        # Dedupe by (data-plan attribute value, price) so 200GB/300GB-twice-in-DOM collapse
        seen: set[tuple[str, float]] = set()
        plans: list[TelcoPlan] = []

        for node in soup.select("[data-plan]"):
            dp = (node.get("data-plan") or "").strip()
            text = " ".join(node.get_text(" ", strip=True).split())
            m = _TIER_RE.search(text)
            if not m:
                continue
            try:
                data_val = float(m.group("data"))
                price    = float(m.group("price"))
            except ValueError:
                continue
            unit = m.group("unit").upper()
            data_gb = data_val * (1024.0 if unit == "TB" else 1.0)

            key = (dp, price)
            if key in seen:
                continue
            seen.add(key)

            net_tag = (m.group("net") or "").upper()
            regions = (m.group("regions") or "").strip(", ").strip()

            # Sanity gate: a circles plan is usually 100-2000 GB and $5-$50
            if not (1 <= data_gb <= 5000):
                continue
            if not (4 <= price <= 100):
                continue

            label_data = f"{data_val:g}{unit}"
            label = f"{net_tag} {label_data}" if net_tag else label_data
            if regions:
                label += f" ({regions})"

            roaming_note = regions or None

            plans.append(TelcoPlan(
                carrier=self.name,
                network=self.network,
                category=self.category,
                plan_name=label,
                monthly_sgd=price,
                data_gb=data_gb,
                contract_months=0,
                roaming_note=roaming_note,
                url=self.plans_url,
                platform_fee_included=False,
            ))

        plans.sort(key=lambda p: (p.monthly_sgd or 0))
        logger.info("circles: extracted %d plans", len(plans))
        return plans
