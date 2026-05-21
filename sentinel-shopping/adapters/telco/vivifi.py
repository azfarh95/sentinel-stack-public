"""VIVIFI (vivifi.me) — Singtel MVNO. WordPress + ArpPricingTable plugin.

Markup uses ArpPricingTable's obfuscated structure:
  <div class='ArpPricingTableColumnWrapper'>
    <div class='arpplan column_1 ...'>
      <div class='planContainer'>
        ...

Data quotas are rendered via custom CSS counters (literally `<b>GB</b>` with
the number painted via a sibling span), so straightforward bs4-text extraction
loses the numbers. Needs a proper text-walker that tracks the row index, or
nodriver-rendered DOM.

Marked as stub until properly extracted. See [[project-sentinel-shopping-mcp]].
"""
from __future__ import annotations

from schema import TelcoPlan
from .base import HttpxCarrier


class VivifiMobile(HttpxCarrier):
    name      = "vivifi"
    network   = "singtel"
    category  = "mobile"
    home_url  = "https://vivifi.me/"
    plans_url = "https://vivifi.me/"

    async def fetch_plans(self, *, pool=None) -> list[TelcoPlan]:
        raise NotImplementedError(
            "vivifi: ArpPricingTable plugin renders data quotas via CSS counters — "
            "needs nodriver DOM walk to extract proper data_gb values. "
            "Plan names + prices are extractable from raw HTML but incomplete plans "
            "are worse than no plans (see [[project-sentinel-shopping-mcp]]).")
