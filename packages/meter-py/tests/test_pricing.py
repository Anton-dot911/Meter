"""Pricing tests — fixed fixtures from spec/prices.json, incl. unknown-model case."""

from __future__ import annotations

from helpers import load_example

from meter import compute_cost, load_prices


def test_known_model_matches_example():
    example = load_example()
    assert (
        compute_cost(example["model"], example["tokens_in"], example["tokens_out"])
        == example["cost_usd"]
    )


def test_cost_rounded_to_five_decimals():
    # claude-haiku-4-5 = $1/$5 per MTok: 1000/1M*1 + 1000/1M*5 = 0.001 + 0.005
    assert compute_cost("claude-haiku-4-5", 1000, 1000) == 0.006


def test_unknown_model_returns_none():
    assert compute_cost("claude-experimental-99", 100, 200) is None


def test_dated_snapshot_suffix_falls_back_to_alias():
    # The API resolves the alias to a dated snapshot; pricing strips -YYYYMMDD.
    assert compute_cost("claude-haiku-4-5-20251001", 1000, 1000) == compute_cost(
        "claude-haiku-4-5", 1000, 1000
    )
    assert compute_cost("claude-haiku-4-5-20251001", 1000, 1000) == 0.006


def test_null_usage_returns_none():
    assert compute_cost("claude-haiku-4-5", None, 5) is None
    assert compute_cost("claude-haiku-4-5", 5, None) is None


def test_prices_json_has_as_of():
    prices = load_prices()
    assert "as_of" in prices and prices["models"]
