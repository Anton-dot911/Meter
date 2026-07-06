"""Cost math — the single place pricing lives for the Python package (Hard Rule 4).

Driven by ``spec/prices.json`` (same file the TS package reads). Prices are
verified manually against https://docs.claude.com — never guessed.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

_cached: Optional[Dict[str, Any]] = None
_SNAPSHOT_RE = re.compile(r"-\d{8}$")


def load_prices(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the shared price table (``spec/prices.json``).

    Resolution order: explicit ``path`` → ``METER_PRICES_PATH`` env var → walk up
    from this module looking for ``spec/prices.json`` (covers monorepo dev and
    git-dependency installs of the whole repo).
    """
    global _cached
    if path is None and _cached is not None:
        return _cached
    file = path or os.environ.get("METER_PRICES_PATH") or _find_spec_prices()
    with open(file, "r", encoding="utf-8") as fh:
        prices = json.load(fh)
    if path is None:
        _cached = prices
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
    raise RuntimeError("meter: spec/prices.json not found; set METER_PRICES_PATH")


def _lookup_price(model: str, prices: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Look up a model in the price table.

    The Anthropic API resolves an alias (``claude-haiku-4-5``) to a dated snapshot
    (``claude-haiku-4-5-20251001``) in the response, and that resolved id is what
    the meter records. ``prices.json`` is keyed by alias, so fall back to stripping
    a trailing ``-YYYYMMDD`` snapshot suffix. Genuinely unknown models still miss
    (Hard Rule 5). This mirrors ``lookupPrice`` in pricing.ts.
    """
    models = prices.get("models", {})
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
    prices: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """Cost in USD rounded to 5 decimals.

    Returns None when the model is unknown in prices.json (Hard Rule 5: the
    record is still written, never estimated) or when usage is unavailable.
    """
    if tokens_in is None or tokens_out is None:
        return None
    if prices is None:
        prices = load_prices()
    price = _lookup_price(model, prices)
    if not price:
        return None
    usd = (tokens_in * price["in_per_mtok"] + tokens_out * price["out_per_mtok"]) / 1_000_000
    # math.floor(x + 0.5) matches JS Math.round (round half up) for non-negative
    # values, keeping cost byte-compatible with pricing.ts.
    return math.floor(usd * 1e5 + 0.5) / 1e5
