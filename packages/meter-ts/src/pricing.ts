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
 * Resolution order: explicit path → METER_PRICES_PATH env var → a copy bundled
 * next to the built module (dist/prices.json, shipped by the build) → walk up
 * from this module looking for spec/prices.json (monorepo dev). The bundled
 * copy is what makes a git-dependency install — which ships only dist/ — able
 * to price calls at all.
 */
export function loadPrices(path?: string): Prices {
  if (path === undefined && cached) return cached;
  const file = path ?? process.env.METER_PRICES_PATH ?? findSpecPrices();
  const prices = JSON.parse(readFileSync(file, "utf8")) as Prices;
  if (path === undefined) cached = prices;
  return prices;
}

/** @internal test hook */
export function __resetPricesCache(): void {
  cached = null;
}

function findSpecPrices(): string {
  const moduleDir = dirname(fileURLToPath(import.meta.url));
  // Copy bundled beside the compiled module by the build (scripts/copy-prices).
  const bundled = join(moduleDir, "prices.json");
  if (existsSync(bundled)) return bundled;
  // Monorepo dev / whole-repo checkout: walk up to the shared spec/ directory.
  let dir = moduleDir;
  for (let i = 0; i < 8; i++) {
    const candidate = join(dir, "spec", "prices.json");
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error("meter: spec/prices.json not found; set METER_PRICES_PATH");
}

// A price-table load failure must never drop the record (meter Hard Rule 5:
// degrade to cost_usd = null, keep the record). Warn once, then stay silent.
let pricesWarned = false;

function warnPricesOnce(err: unknown): void {
  if (pricesWarned) return;
  pricesWarned = true;
  const msg = err instanceof Error ? err.message : String(err);
  console.error(
    `meter: pricing unavailable, recording cost_usd=null (further pricing errors will be silent): ${msg}`,
  );
}

/** @internal test hook */
export function __resetPricesWarnOnce(): void {
  pricesWarned = false;
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
 * Returns null when usage is unavailable, when the model is unknown in
 * prices.json, or when the price table itself cannot be loaded (Hard Rule 5:
 * the record is still written, never estimated, never dropped).
 */
export function computeCost(
  model: string,
  tokensIn: number | null,
  tokensOut: number | null,
  prices?: Prices,
): number | null {
  if (tokensIn == null || tokensOut == null) return null;
  let table = prices;
  if (table === undefined) {
    try {
      table = loadPrices();
    } catch (err) {
      warnPricesOnce(err);
      return null;
    }
  }
  const p = lookupPrice(model, table);
  if (!p) return null;
  const usd = (tokensIn * p.in_per_mtok + tokensOut * p.out_per_mtok) / 1_000_000;
  return Math.round(usd * 1e5) / 1e5;
}
