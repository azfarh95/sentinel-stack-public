"""Unit tests for cash forecast recurrence math."""
from datetime import date, timedelta
from app import cash_forecast as cf


class TestEventsInRange:
    def test_simple_monthly_income(self):
        items = [{"name": "Salary", "amount": 3000, "day": 15, "enabled": True}]
        start = date(2026, 5, 1)
        end = date(2026, 8, 31)
        events = cf._events_in_range(start, end, items, sign=+1)
        # May 15, Jun 15, Jul 15, Aug 15 = 4 events
        assert len(events) == 4
        assert all(e["amount_signed"] == 3000 for e in events)
        assert events[0]["date"] == "2026-05-15"
        assert events[-1]["date"] == "2026-08-15"

    def test_monthly_expense_negative_sign(self):
        items = [{"name": "Rent", "amount": 1500, "day": 1, "enabled": True}]
        events = cf._events_in_range(date(2026, 5, 13), date(2026, 7, 5), items, sign=-1)
        # June 1 + July 1 (May 1 is before start)
        assert len(events) == 2
        assert all(e["amount_signed"] == -1500 for e in events)

    def test_disabled_items_skipped(self):
        items = [
            {"name": "On", "amount": 100, "day": 5, "enabled": True},
            {"name": "Off", "amount": 999, "day": 5, "enabled": False},
        ]
        events = cf._events_in_range(date(2026, 5, 1), date(2026, 5, 31), items, sign=-1)
        assert len(events) == 1
        assert events[0]["name"] == "On"

    def test_day_clamped_to_28(self):
        """Day 31 should still produce one event per month, clamped to safe day."""
        items = [{"name": "X", "amount": 1, "day": 31, "enabled": True}]
        events = cf._events_in_range(date(2026, 1, 1), date(2026, 12, 31), items, sign=-1)
        # 12 events, all on day <= 28
        assert len(events) == 12
        for e in events:
            assert int(e["date"][-2:]) <= 28

    def test_empty_items(self):
        events = cf._events_in_range(date(2026, 5, 1), date(2026, 6, 30), [], sign=+1)
        assert events == []


class TestLoadSave:
    def test_yaml_roundtrip(self, tmp_path, monkeypatch):
        # Use a temporary file
        fake = tmp_path / "recurring.yaml"
        fake.write_text("income:\n  - name: X\n    amount: 100\n    day: 5\n    enabled: true\nexpense: []\n")
        monkeypatch.setattr(cf, "RECURRING_PATH", fake)
        data = cf.load_recurring()
        assert data["income"][0]["name"] == "X"
        data["income"].append({"name": "Y", "amount": 200, "day": 10, "enabled": True})
        cf.save_recurring(data)
        reloaded = cf.load_recurring()
        assert len(reloaded["income"]) == 2
        assert reloaded["income"][1]["name"] == "Y"

    def test_add_recurring_appends(self, tmp_path, monkeypatch):
        fake = tmp_path / "recurring.yaml"
        fake.write_text("income: []\nexpense: []\n")
        monkeypatch.setattr(cf, "RECURRING_PATH", fake)
        entry = cf.add_recurring("expense", "Netflix", 19.99, 5, category="Subscription")
        assert entry["name"] == "Netflix"
        assert entry["amount"] == 19.99
        assert entry["enabled"] is True
        data = cf.load_recurring()
        assert len(data["expense"]) == 1
        assert data["expense"][0]["name"] == "Netflix"

    def test_add_recurring_rejects_bad_kind(self, tmp_path, monkeypatch):
        fake = tmp_path / "recurring.yaml"
        fake.write_text("income: []\nexpense: []\n")
        monkeypatch.setattr(cf, "RECURRING_PATH", fake)
        import pytest
        with pytest.raises(ValueError):
            cf.add_recurring("salary", "x", 1, 1)
