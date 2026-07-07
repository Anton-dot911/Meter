"""Pricing tests — mirror of packages/meter-ts/test/pricing.test.ts."""

from __future__ import annotations

from helpers import CaptureTransport, fake_client, fake_response

from meter.meter import metered_client
from meter.pricing import compute_cost, load_prices

PRICES = load_prices()


def test_load_prices_finds_spec_from_package_dir():
    assert PRICES["as_of"].count("-") == 2  # YYYY-MM-DD
    assert PRICES["models"]["claude-sonnet-4-6"] == {"in_per_mtok": 3.0, "out_per_mtok": 15.0}


def test_matches_canonical_example(example):
    assert (
        compute_cost(example["model"], example["tokens_in"], example["tokens_out"], PRICES)
        == example["cost_usd"]
    )


def test_computes_and_rounds_to_5_decimals():
    # 1_000_000 in @ $3 + 1_000_000 out @ $15 = $18 exactly
    assert compute_cost("claude-sonnet-4-6", 1_000_000, 1_000_000, PRICES) == 18
    # 1 in + 1 out = 0.000003 + 0.000015 = 0.000018 -> rounds to 0.00002
    assert compute_cost("claude-sonnet-4-6", 1, 1, PRICES) == 0.00002


def test_prices_dated_snapshot_via_alias():
    # The API records the resolved snapshot (e.g. claude-haiku-4-5-20251001);
    # it must cost the same as the alias claude-haiku-4-5 (Hard Rule 5 note in PLAN).
    assert compute_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000, PRICES) == compute_cost(
        "claude-haiku-4-5", 1_000_000, 1_000_000, PRICES
    )
    assert compute_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000, PRICES) == 6


def test_unknown_model_returns_none():
    assert compute_cost("gpt-oops", 1000, 1000, PRICES) is None
    # An unknown alias with a date suffix must not be estimated either.
    assert compute_cost("gpt-oops-20251001", 1000, 1000, PRICES) is None


def test_none_usage_returns_none():
    assert compute_cost("claude-sonnet-4-6", None, 100, PRICES) is None
    assert compute_cost("claude-sonnet-4-6", 100, None, PRICES) is None


# Hard Rule 5: a missing/unreadable price table must NOT raise and must NOT drop
# the record — cost degrades to None while the record is still written.
def test_missing_prices_returns_none_not_raise(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("spec/prices.json not found; set METER_PRICES_PATH")

    monkeypatch.setattr("meter.pricing.load_prices", boom)
    assert compute_cost("claude-sonnet-4-6", 1000, 1000) is None


def test_prices_absent_call_still_recorded_with_cost_null(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("spec/prices.json not found; set METER_PRICES_PATH")

    monkeypatch.setattr("meter.pricing.load_prices", boom)

    transport = CaptureTransport()
    client = metered_client(
        fake_client(lambda **kwargs: fake_response("claude-haiku-4-5", 10, 5, "req_no_prices")),
        project="p",
        component="c",
        transport=transport,
    )
    resp = client.messages.create(model="claude-haiku-4-5", max_tokens=16, messages=[])

    assert resp is not None
    assert len(transport.records) == 1
    rec = transport.records[0]
    assert rec["cost_usd"] is None
    assert rec["status"] == "ok"
    assert rec["tokens_in"] == 10
    assert rec["tokens_out"] == 5
