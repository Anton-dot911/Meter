"""Cost math — one place per package (Hard Rule 4), driven by spec/prices.json.

Faithful mirror of packages/meter-ts/src/pricing.ts, including the dated-snapshot
price normalization noted for T3 in docs/PLAN.md.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Optional, TypedDict


class ModelPrice(TypedDict):
    in_per_mtok: float
    out_per_mtok: float


class Prices(TypedDict):
    as_of: str
    models: dict[str, ModelPrice]


_cache: Optional[Prices] = None


def load_prices(path: Optional[str] = None) -> Prices:
    """Load the shared price table (spec/prices.json).

    Resolution order: explicit path -> METER_PRICES_PATH env var -> walk up from
    this module looking for spec/prices.json (covers monorepo dev and
    git-dependency installs of the whole repo).
    """
    global _cache
    if path is None and _cache is not None:
        return _cache
    file = path or os.environ.get("METER_PRICES_PATH") or _find_spec_prices()
    with open(file, "r", encoding="utf-8") as fh:
        prices: Prices = json.load(fh)
    if path is None:
        _cache = prices
    return prices


def _find_spec_prices() -> str:
    directory = Path(__file__).resolve().parent
    for _ in range(8):
        candidate = directory / "spec" / "prices.json"
        if candidate.exists():
            return str(candidate)
        if directory.parent == directory:
            break
        directory = directory.parent
    raise FileNotFoundError("meter: spec/prices.json not found; set METER_PRICES_PATH")


_SNAPSHOT_RE = re.compile(r"-\d{8}$")


def _lookup_price(model: str, prices: Prices) -> Optional[ModelPrice]:
    """Look up a model in the price table.

    The Anthropic API resolves an alias (``claude-haiku-4-5``) to a dated snapshot
    (``claude-haiku-4-5-20251001``) in the response, and that resolved id is what
    the meter records. prices.json is keyed by alias, so fall back to stripping a
    trailing ``-YYYYMMDD`` snapshot suffix. Genuinely unknown models still miss
    (Hard Rule 5).
    """
    models = prices["models"]
    exact = models.get(model)
    if exact:
        return exact
    alias = _SNAPSHOT_RE.sub("", model)
    if alias == model:
        return None
    return models.get(alias)


def compute_cost(
    model: str,
    tokens_in: Optional[int],
    tokens_out: Optional[int],
    prices: Optional[Prices] = None,
) -> Optional[float]:
    """Cost in USD rounded to 5 decimals.

    Returns None when the model is unknown in prices.json (Hard Rule 5: the record
    is still written, never estimated) or when usage is unavailable.
    """
    if tokens_in is None or tokens_out is None:
        return None
    if prices is None:
        prices = load_prices()
    price = _lookup_price(model, prices)
    if not price:
        return None
    usd = (tokens_in * price["in_per_mtok"] + tokens_out * price["out_per_mtok"]) / 1_000_000
    # Match JS Math.round (round half up); cost is always >= 0.
    return math.floor(usd * 1e5 + 0.5) / 1e5
