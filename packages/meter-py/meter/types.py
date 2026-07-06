"""Canonical record shape — must stay byte-compatible with spec/record.schema.json
and with the TypeScript package (packages/meter-ts/src/types.ts): same field names,
same units (usd, ms), same enums (Hard Rule 3).
"""

from __future__ import annotations

from typing import Literal, Optional, Protocol, TypedDict

MeterStatus = Literal["ok", "error", "retried"]


class MeterRecord(TypedDict):
    """One LLM call record written to the ``llm_calls`` table.

    A plain ``dict`` at runtime so it serializes byte-compatibly with the TS
    object and inserts straight into Supabase. Keys/types mirror
    ``spec/record.schema.json`` exactly.
    """

    ts: str
    """ISO 8601 UTC timestamp of the call."""
    project: str
    """Lowercase project slug."""
    component: str
    """Lowercase component slug."""
    model: str
    """Model ID as reported by the SDK response."""
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    cost_usd: Optional[float]
    """USD rounded to 5 decimals; None if the model is unknown in prices.json."""
    latency_ms: int
    status: MeterStatus
    error_type: Optional[str]
    request_id: Optional[str]
    trace_id: Optional[str]


class Transport(Protocol):
    """Persist one record. May raise — callers treat recording as fire-and-forget."""

    def send(self, record: MeterRecord) -> None: ...
