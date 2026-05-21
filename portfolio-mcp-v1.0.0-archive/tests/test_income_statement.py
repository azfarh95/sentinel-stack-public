"""Unit tests for income_statement's accrual-tag filter + classification."""
from app import income_statement as is_mod


class TestPriorYearTagFilter:
    def test_no_tags_not_excluded(self):
        tx = {"tags": None, "amount": "100", "category_name": "Salary"}
        assert not is_mod._has_prior_year_tag(tx)

    def test_empty_tags_not_excluded(self):
        tx = {"tags": [], "amount": "100"}
        assert not is_mod._has_prior_year_tag(tx)

    def test_unrelated_tags_not_excluded(self):
        tx = {"tags": ["ifast", "dividend-reinvest"], "amount": "100"}
        assert not is_mod._has_prior_year_tag(tx)

    def test_prior_year_tag_excluded(self):
        tx = {"tags": ["cpf", "prior-year:2025"], "amount": "100"}
        assert is_mod._has_prior_year_tag(tx)

    def test_prior_year_any_year(self):
        for year in (2024, 2025, 2026, 2030):
            tx = {"tags": [f"prior-year:{year}"]}
            assert is_mod._has_prior_year_tag(tx)

    def test_tag_must_start_with_prefix(self):
        # "year-prior:2025" or "old-prior-year:..." shouldn't match
        tx = {"tags": ["not-prior-year:2025", "this-prior-year:2025"]}
        assert not is_mod._has_prior_year_tag(tx)
