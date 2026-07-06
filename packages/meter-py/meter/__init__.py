"""Meter (Python) — fire-and-forget cost/latency tracking around the Anthropic SDK.

Byte-compatible with the TS package: same record shape, units and enums
(spec/record.schema.json, spec/record.example.json). See docs/PLAN.md.
"""

from .meter import metered_client, record
from .pricing import compute_cost, load_prices
from .transport import (
    FallbackTransport,
    JsonlFallback,
    SupabaseTransport,
    default_transport,
)
from .types import MeterRecord, MeterStatus, Transport

__all__ = [
    "metered_client",
    "record",
    "compute_cost",
    "load_prices",
    "SupabaseTransport",
    "JsonlFallback",
    "FallbackTransport",
    "default_transport",
    "MeterRecord",
    "MeterStatus",
    "Transport",
]
