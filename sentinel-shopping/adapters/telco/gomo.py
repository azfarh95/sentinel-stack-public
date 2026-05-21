"""GOMO (gomo.sg) — Singtel sub-brand.

Markup uses styled-components — every class name is a hash like `sc-aXZVg nTDx`.
No semantic anchors for plan cards. Plan blocks need to be located by their
TEXT content (prices ending in "/mth") then walked up to find the card.

Marked as stub until properly extracted. The right shape is probably:
  1. Find all `*[innerText matches /\$\d+\.?\d*\/mth/]`
  2. Walk up ~3 ancestors to find the card container
  3. Within that container, find data-GB text and plan name

Alternatively: render with nodriver and use generic tile-extraction, since
nodriver already handles SPAs.

See [[project-sentinel-shopping-mcp]].
"""
from __future__ import annotations

from schema import TelcoPlan
from .base import HttpxCarrier


class GomoMobile(HttpxCarrier):
    name      = "gomo"
    network   = "singtel"
    category  = "mobile"
    home_url  = "https://www.gomo.sg/"
    plans_url = "https://www.gomo.sg/"

    async def fetch_plans(self, *, pool=None) -> list[TelcoPlan]:
        raise NotImplementedError(
            "gomo: styled-components markup, no semantic class names. "
            "Plan locator strategy needs to walk up from price-text nodes — "
            "deferred. See [[project-sentinel-shopping-mcp]].")
