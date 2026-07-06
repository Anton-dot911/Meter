import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export interface ModelPrice {
  in_per_mtok: number;
  out_per_mtok: number;
}

export interface Prices {
  as_of: string;
  models: Record<string, ModelPrice>;
}

let cached: Prices | null = null;

/**
 * Load the shared price table (spec/prices.json).
 * Resolution order: explicit path → METER_PRICES_PATH env var → walk up from
 * this module looking for spec/prices.json (covers monorepo dev, dist builds,
 * and git-dependency installs of the whole repo).
 */
export function loadPrices(path?: string): Prices {
  if (path === undefined && cached) return cached;
  const file = path ?? process.env.METER_PRICES_PATH ?? findSpecPrices();
  const prices = JSON.parse(readFileSync(file, "utf8")) as Prices;
  if (path === undefined) cached = prices;
  return prices;
}

function findSpecPrices(): string {
  let dir = dirname(fileURLToPath(import.meta.url));
  for (let i = 0; i < 8; i++) {
    const candidate = join(dir, "spec", "prices.json");
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error("meter: spec/prices.json not found; set METER_PRICES_PATH");
}

/**
 * Look up a model in the price table. The Anthropic API resolves an alias
 * (`claude-haiku-4-5`) to a dated snapshot (`claude-haiku-4-5-20251001`) in
 * the response, and that resolved id is what the meter records. prices.json is
 * keyed by alias, so fall back to stripping a trailing `-YYYYMMDD` snapshot
 * suffix. Genuinely unknown models still miss (Hard Rule 5).
 */
function lookupPrice(model: string, prices: Prices): ModelPrice | undefined {
  const exact = prices.models[model];
  if (exact) return exact;
  const alias = model.replace(/-\d{8}$/, "");
  return alias === model ? undefined : prices.models[alias];
}

/**
 * Cost in USD rounded to 5 decimals.
 * Returns null when the model is unknown in prices.json (Hard Rule 5: the
 * record is still written, never estimated) or when usage is unavailable.
 */
export function computeCost(
  model: string,
  tokensIn: number | null,
  tokensOut: number | null,
  prices: Prices = loadPrices(),
): number | null {
  if (tokensIn == null || tokensOut == null) return null;
  const p = lookupPrice(model, prices);
  if (!p) return null;
  const usd = (tokensIn * p.in_per_mtok + tokensOut * p.out_per_mtok) / 1_000_000;
  return Math.round(usd * 1e5) / 1e5;
}
