"""Cost math — the single place pricing lives for the Python package (Hard Rule 4).

Driven by ``spec/prices.json`` (same file the TS package reads). Prices are
verified manually against https://docs.claude.com — never guessed.
"""

from __future__ import annotations

import importlib.resources
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_cached: Optional[Dict[str, Any]] = None
_SNAPSHOT_RE = re.compile(r"-\d{8}$")


def load_prices(path: Optional[str] = None) -> Dict[str, Any]:
    """Load the shared price table (``spec/prices.json``).

    Resolution order: explicit ``path`` → ``METER_PRICES_PATH`` env var → a copy
    bundled as package data (``meter/prices.json``, shipped in the wheel) → walk
    up from this module looking for ``spec/prices.json`` (monorepo dev). The
    packaged copy is what makes a git-dependency install — whose wheel ships only
    the ``meter`` package — able to price calls at all.
    """
    global _cached
    if path is None and _cached is not None:
        return _cached
    file = path or os.environ.get("METER_PRICES_PATH") or _packaged_prices() or _find_spec_prices()
    with open(file, "r", encoding="utf-8") as fh:
        prices = json.load(fh)
    if path is None:
        _cached = prices
    return prices


def _reset_prices_cache() -> None:
    """@internal test hook."""
    global _cached
    _cached = None


def _packaged_prices() -> Optional[str]:
    """Path to the price table bundled as package data, if present.

    Populated only in a built/installed wheel (see pyproject force-include);
    absent when running from the monorepo source tree, where the walk-up below
    finds the shared ``spec/prices.json`` instead.
    """
    try:
        resource = importlib.resources.files("meter").joinpath("prices.json")
        if resource.is_file():
            return str(resource)
    except (ModuleNotFoundError, FileNotFoundError, TypeError):
        return None
    return None


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


# A price-table load failure must never drop the record (Hard Rule 5: degrade to
# cost_usd = null, keep the record). Warn once, then stay silent.
_prices_warned = False


def _warn_prices_once(err: object) -> None:
    global _prices_warned
    if _prices_warned:
        return
    _prices_warned = True
    print(
        "meter: pricing unavailable, recording cost_usd=null "
        f"(further pricing errors will be silent): {err}",
        file=sys.stderr,
    )


def _reset_prices_warn_once() -> None:
    """@internal test hook."""
    global _prices_warned
    _prices_warned = False


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

    Returns None when usage is unavailable, when the model is unknown in
    prices.json, or when the price table itself cannot be loaded (Hard Rule 5:
    the record is still written, never estimated, never dropped).
    """
    if tokens_in is None or tokens_out is None:
        return None
    if prices is None:
        try:
            prices = load_prices()
        except Exception as err:  # noqa: BLE001 - missing/unreadable table degrades
            _warn_prices_once(err)
            return None
    price = _lookup_price(model, prices)
    if not price:
        return None
    usd = (tokens_in * price["in_per_mtok"] + tokens_out * price["out_per_mtok"]) / 1_000_000
    # math.floor(x + 0.5) matches JS Math.round (round half up) for non-negative
    # values, keeping cost byte-compatible with pricing.ts.
    return math.floor(usd * 1e5 + 0.5) / 1e5
