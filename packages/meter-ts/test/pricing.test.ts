import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { computeCost, loadPrices } from "../src/pricing.js";
import type { MeterRecord } from "../src/types.js";

const prices = loadPrices();
const example = JSON.parse(
  readFileSync(new URL("../../../spec/record.example.json", import.meta.url), "utf8"),
) as MeterRecord;

describe("loadPrices", () => {
  it("finds spec/prices.json from the package directory", () => {
    expect(prices.as_of).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    expect(prices.models["claude-sonnet-4-6"]).toEqual({ in_per_mtok: 3.0, out_per_mtok: 15.0 });
  });
});

describe("computeCost", () => {
  it("matches the canonical example record from spec/", () => {
    expect(computeCost(example.model, example.tokens_in, example.tokens_out, prices)).toBe(
      example.cost_usd,
    );
  });

  it("computes and rounds to 5 decimals", () => {
    // 1_000_000 in @ $3 + 1_000_000 out @ $15 = $18 exactly
    expect(computeCost("claude-sonnet-4-6", 1_000_000, 1_000_000, prices)).toBe(18);
    // 1 in + 1 out = 0.000003 + 0.000015 = 0.000018 → rounds to 0.00002
    expect(computeCost("claude-sonnet-4-6", 1, 1, prices)).toBe(0.00002);
  });

  it("prices a dated snapshot id via its alias", () => {
    // The API records the resolved snapshot (e.g. claude-haiku-4-5-20251001);
    // it must cost the same as the alias claude-haiku-4-5.
    expect(computeCost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000, prices)).toBe(
      computeCost("claude-haiku-4-5", 1_000_000, 1_000_000, prices),
    );
    expect(computeCost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000, prices)).toBe(6);
  });

  it("returns null for a model missing from prices.json (Hard Rule 5)", () => {
    expect(computeCost("gpt-oops", 1000, 1000, prices)).toBeNull();
    // An unknown alias with a date suffix must not be estimated either.
    expect(computeCost("gpt-oops-20251001", 1000, 1000, prices)).toBeNull();
  });

  it("returns null when usage is unavailable", () => {
    expect(computeCost("claude-sonnet-4-6", null, 100, prices)).toBeNull();
    expect(computeCost("claude-sonnet-4-6", 100, null, prices)).toBeNull();
  });
});
