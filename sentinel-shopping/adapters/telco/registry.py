"""Telco carrier registry.

Server-rendered (HttpxCarrier — fast, no browser):
  eight, vivifi, gomo

JS-rendered (NodriverCarrier — stubs, raise NotImplementedError for now):
  simba, zero1, zym, myrepublic, circles, redone, cmlink, hicard, heya, eight (broadband?),
  singtel, starhub, m1 (their consumer-facing plan grids), giga, viewqwest, whizcomms

Notes from SG telco landscape (2026-05-20):
  - Mandatory $0.30/mo platform fee since April 2025 — typically NOT in headline prices.
  - Most carriers also publish CIS PDFs with steady-state pricing T&Cs.
  - Some MVNOs (Eight, Vivifi, Gomo) are no-contract SIM-only and price-transparent.
"""
from __future__ import annotations

from .base import Carrier, NodriverCarrier
from .eight import EightMobile
from .vivifi import VivifiMobile
from .gomo import GomoMobile
from .giga import GigaMobile
from .circles import CirclesMobile

# Stubs — full extraction TBD; surfaces the carrier so users know it exists
# and can see "not yet supported" rather than the carrier missing entirely.


class _NodriverStub(NodriverCarrier):
    home_url = ""
    plans_url = ""

    async def fetch_plans(self):
        raise NotImplementedError(f"{self.name}: nodriver extractor not yet implemented")


def _stub(name, network, category, home, plans):
    cls = type(
        f"{name.title()}{category.title()}",
        (_NodriverStub,),
        {
            "name":      name, "network": network, "category": category,
            "home_url":  home, "plans_url": plans,
        },
    )
    return cls()


CARRIERS: list[Carrier] = [
    # Server-rendered, working
    EightMobile(),
    GigaMobile(),       # bs4 against /
    VivifiMobile(),     # stub — ArpPricingTable CSS counters
    GomoMobile(),       # stub — styled-components hash classes

    # JS-rendered, working (need pool)
    CirclesMobile(),    # nodriver-rendered + [data-plan] selector

    # JS-rendered, stubs
    _stub("singtel",    "singtel",  "mobile",
          "https://www.singtel.com/",
          "https://www.singtel.com/personal/products-services/mobile/postpaid-plans"),
    _stub("starhub",    "starhub",  "mobile",
          "https://www.starhub.com/",
          "https://www.starhub.com/personal/mobile/mobile-phones-plans.html"),
    _stub("m1",         "m1",       "mobile",
          "https://www.m1.com.sg/",
          "https://www.m1.com.sg/mobile/postpaid"),
    _stub("simba",      "simba",    "mobile",
          "https://simba.sg/",
          "https://simba.sg/plans/sim-only-plans"),
    _stub("zero1",      "singtel",  "mobile",
          "https://zero1.sg/",
          "https://zero1.sg/plans"),
    _stub("zym",        "singtel",  "mobile",
          "https://zym.sg/",
          "https://zym.sg/"),
    _stub("heya",       "singtel",  "mobile",
          "https://www.heya.sg/",
          "https://www.heya.sg/"),
    _stub("hicard",     "singtel",  "mobile",
          "https://www.hicard.sg/",
          "https://www.hicard.sg/"),
    _stub("myrepublic", "starhub",  "mobile",
          "https://www.myrepublic.com.sg/",
          "https://www.myrepublic.com.sg/mobile/"),
    _stub("redone",     "m1",       "mobile",
          "https://www.redone.sg/",
          "https://www.redone.sg/"),
    _stub("cmlink",     "m1",       "mobile",
          "https://www.cmlink.com.sg/",
          "https://www.cmlink.com.sg/"),

    # Broadband side — all stubs for now
    _stub("singtel",    "singtel",  "broadband",
          "https://www.singtel.com/",
          "https://www.singtel.com/personal/products-services/broadband"),
    _stub("starhub",    "starhub",  "broadband",
          "https://www.starhub.com/",
          "https://www.starhub.com/personal/broadband.html"),
    _stub("m1",         "m1",       "broadband",
          "https://www.m1.com.sg/",
          "https://www.m1.com.sg/home-services/home-broadband"),
    _stub("myrepublic", "starhub",  "broadband",
          "https://myrepublic.com.sg/",
          "https://myrepublic.com.sg/broadband/"),
    _stub("whizcomms",  "m1",       "broadband",
          "https://whizcomms.com.sg/",
          "https://whizcomms.com.sg/fibre-broadband-plans/"),
    _stub("viewqwest",  "viewqwest","broadband",
          "https://viewqwest.com/",
          "https://viewqwest.com/personal/fibre-broadband"),
]


def by_filter(*, category: str | None = None,
                carrier: str | None = None,
                network: str | None = None) -> list[Carrier]:
    out = list(CARRIERS)
    if category:
        out = [c for c in out if c.category == category]
    if carrier:
        out = [c for c in out if c.name == carrier]
    if network:
        out = [c for c in out if c.network == network]
    return out
