"""Unit tests for balance_sheet's classification + aggregation logic.

These exercise the pure-Python paths (no Firefly/Moralis). Anything that
fetches network data is replaced by an in-memory _Context.
"""
import pytest
from app import balance_sheet as bs


# ── Test fixtures ─────────────────────────────────────────────────────────────

def make_ctx(positions=None, manual=None, by_acct_id=None, fx=1.27):
    return bs._Context(
        by_acct_id=by_acct_id or {},
        positions=positions or [],
        manual=manual or [],
        usd_to_sgd=fx,
    )


SAMPLE_POSITIONS = [
    {"symbol": "ETH", "chain": "eth", "usd_value": 50.0, "token_address": None, "decimals": 18, "raw_balance": "1"},
    {"symbol": "BNB", "chain": "bsc", "usd_value": 200.0, "token_address": None, "decimals": 18, "raw_balance": "1"},
    {"symbol": "USDC", "chain": "base", "usd_value": 1000.0, "token_address": "0x123", "decimals": 6, "raw_balance": "1"},
    {"symbol": "PACK", "chain": "cronos", "usd_value": 75.0, "token_address": "0xabc", "decimals": 18, "raw_balance": "1"},
    {"symbol": "DUST", "chain": "polygon", "usd_value": 2.50, "token_address": "0xdef", "decimals": 18, "raw_balance": "1"},
]


# ── Leaf resolution ───────────────────────────────────────────────────────────

class TestResolveLeaf:
    def test_firefly_account_aggregation(self):
        ctx = make_ctx(by_acct_id={
            1: {"id": "1", "attributes": {"name": "POSB", "current_balance": "1000",
                                          "currency_code": "SGD"}},
            4: {"id": "4", "attributes": {"name": "Cash", "current_balance": "50",
                                          "currency_code": "SGD"}},
        })
        node = {"id": "x", "label": "Cash", "firefly_account_ids": [1, 4]}
        usd, sgd, items = bs._resolve_leaf(node, ctx)
        assert sgd == 1050.0
        assert usd == pytest.approx(1050 / 1.27, rel=1e-4)
        assert len(items) == 2

    def test_firefly_usd_account_converted(self):
        ctx = make_ctx(by_acct_id={
            97: {"id": "97", "attributes": {"name": "Coinbase", "current_balance": "100",
                                            "currency_code": "USD"}},
        })
        node = {"id": "x", "label": "CEX", "firefly_account_ids": [97]}
        usd, sgd, _ = bs._resolve_leaf(node, ctx)
        # USD 100 × 1.27 = SGD 127
        assert sgd == pytest.approx(127.0)

    def test_chain_filter_named(self):
        ctx = make_ctx(positions=SAMPLE_POSITIONS)
        node = {"id": "bnb", "label": "BNB Chain",
                "source": "portfolio_mcp_liquid_chain", "chain": "bsc"}
        usd, sgd, items = bs._resolve_leaf(node, ctx)
        assert usd == 200.0
        assert len(items) == 1
        assert items[0]["label"] == "BNB"

    def test_dust_filter(self):
        ctx = make_ctx(positions=SAMPLE_POSITIONS)
        node = {"id": "dust", "label": "Dust Chains",
                "source": "portfolio_mcp_liquid_dust",
                "named_chains": ["bsc", "base", "cronos"],
                "threshold_usd": 50}
        usd, sgd, items = bs._resolve_leaf(node, ctx)
        # ETH (50) on its own chain → NOT dust because 50 == threshold but check is < not ≤
        # Actually our code uses < threshold. ETH chain total = 50 NOT < 50 → not dust.
        # POL on polygon = 2.50 → IS dust
        assert any(i["label"].startswith("DUST") for i in items)
        assert usd == 2.50

    def test_dust_filter_excludes_named(self):
        ctx = make_ctx(positions=SAMPLE_POSITIONS)
        node = {"id": "dust", "label": "Dust",
                "source": "portfolio_mcp_liquid_dust",
                "named_chains": ["bsc", "base", "cronos"],
                "threshold_usd": 100}    # both eth (50) and polygon (2.5) qualify
        usd, sgd, items = bs._resolve_leaf(node, ctx)
        chains_in = {i["label"].split("(")[-1].rstrip(")") for i in items}
        assert "bsc" not in str(items)  # named excluded
        assert "base" not in str(items)
        assert "cronos" not in str(items)

    def test_other_above_threshold(self):
        # Add a fake "arbitrum" position over threshold
        positions = SAMPLE_POSITIONS + [
            {"symbol": "ARB", "chain": "arbitrum", "usd_value": 75.0,
             "token_address": "0x111", "decimals": 18, "raw_balance": "1"}
        ]
        ctx = make_ctx(positions=positions)
        node = {"id": "other", "label": "Other",
                "source": "portfolio_mcp_liquid_other",
                "named_chains": ["bsc", "base", "cronos"],
                "threshold_usd": 50}
        usd, sgd, items = bs._resolve_leaf(node, ctx)
        # Only eth (50) and arbitrum (75) qualify, both >= 50 and not in named.
        # Note: dust is < 50, "other" is >= threshold (it's the complement).
        # Implementation uses >= threshold for "other".
        assert usd == pytest.approx(125.0)

    def test_manual_filter_by_protocol(self):
        ctx = make_ctx(manual=[
            {"label": "WolfSwap PACK stake", "chain": "cronos",
             "protocol": "WolfSwap", "usd_value": 7500.0},
            {"label": "Uniswap LP",
             "chain": "eth", "protocol": "Uniswap", "usd_value": 1000.0},
        ])
        node = {"id": "staking", "label": "Staking Vaults",
                "source": "portfolio_mcp_manual",
                "include_protocols": ["WolfSwap"]}
        usd, sgd, items = bs._resolve_leaf(node, ctx)
        assert usd == 7500.0
        assert len(items) == 1
        assert items[0]["protocol"] == "WolfSwap"

    def test_manual_empty_protocols_matches_nothing(self):
        ctx = make_ctx(manual=[
            {"label": "Some LP", "chain": "eth", "protocol": "X", "usd_value": 100.0},
        ])
        node = {"id": "lp", "label": "LP", "source": "portfolio_mcp_manual",
                "include_protocols": []}
        usd, sgd, items = bs._resolve_leaf(node, ctx)
        assert usd == 0.0
        assert items == []

    def test_todo_returns_zero(self):
        ctx = make_ctx()
        node = {"id": "todo", "label": "TBA", "source": "todo"}
        usd, sgd, items = bs._resolve_leaf(node, ctx)
        assert usd == 0.0
        assert sgd == 0.0
        assert items == []


# ── Liability bucket aging math ───────────────────────────────────────────────

class TestLiabilityBucket:
    def make_registry(self):
        return {
            "accounts": [
                {
                    "name": "DBS Cashline",
                    "plans": [{"monthly": 73.15, "remaining_months": 10}],
                },
                {
                    "name": "Maybank Term Loan",
                    "plans": [{"monthly": 105.00, "remaining_months": 60}],
                },
            ]
        }

    def test_due_30_days(self):
        # months 1-1 = next month only
        usd, sgd, br = bs._liability_bucket(self.make_registry(), 1, 1, fx=1.27)
        assert sgd == pytest.approx(73.15 + 105.00)
        assert len(br) == 2
        assert br[0]["name"] in ("DBS Cashline", "Maybank Term Loan")  # alpha sort

    def test_due_31_to_365(self):
        # months 2-12 = next 11 months
        usd, sgd, br = bs._liability_bucket(self.make_registry(), 2, 12, fx=1.27)
        # DBS Cashline: 9 more months × 73.15 = 658.35 (it only has 10 remaining)
        # Maybank: 11 × 105 = 1155
        assert sgd == pytest.approx(73.15 * 9 + 105.00 * 11)

    def test_due_12_plus_months(self):
        # months 13-9999
        usd, sgd, br = bs._liability_bucket(self.make_registry(), 13, 9999, fx=1.27)
        # DBS Cashline: 0 months > 12 (remaining=10)
        # Maybank: 48 months > 12 × 105
        assert sgd == pytest.approx(105.00 * 48)
        # Only Maybank has non-zero
        assert len(br) == 1
        assert br[0]["name"] == "Maybank Term Loan"

    def test_breakdown_alphabetical(self):
        """Breakdown should be sorted alphabetically by account name."""
        usd, sgd, br = bs._liability_bucket(self.make_registry(), 1, 1, fx=1.27)
        names = [b["name"] for b in br]
        assert names == sorted(names, key=lambda x: x.lower())


# ── Chain totals helper ───────────────────────────────────────────────────────

def test_chain_totals_groups_correctly():
    positions = [
        {"chain": "eth", "usd_value": 100, "symbol": "A"},
        {"chain": "eth", "usd_value": 50, "symbol": "B"},
        {"chain": "bsc", "usd_value": 200, "symbol": "C"},
    ]
    out = bs._chain_totals(positions)
    assert out == {"eth": 150.0, "bsc": 200.0}
