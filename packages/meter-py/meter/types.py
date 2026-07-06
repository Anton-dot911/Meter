"""Canonical record shape — must stay byte-compatible with spec/record.schema.json.

Both the TS and Python packages produce records with the same field names,
units (usd, ms) and enums. The shared JSON fixture ``spec/record.example.json``
is validated by tests in BOTH packages (Hard Rule 3).
"""

from __future__ import annotations

from typing import Callable, Optional, Protocol, TypedDict, Union

# status enum: "ok" | "error" | "retried"
MeterStatus = str


class MeterRecord(TypedDict):
    """One LLM call record written to the ``llm_calls`` table."""

    ts: str  # ISO 8601 UTC timestamp of the call
    project: str  # lowercase project slug
    component: str  # lowercase component slug
    model: str  # model id as reported by the SDK response
    tokens_in: Optional[int]
    tokens_out: Optional[int]
    cost_usd: Optional[float]  # USD, 5 decimals; None if the model is unknown
    latency_ms: int
    status: MeterStatus
    error_type: Optional[str]
    request_id: Optional[str]
    trace_id: Optional[str]


class Transport(Protocol):
    """Persist one record. May raise — callers treat recording as fire-and-forget."""

    def send(self, record: MeterRecord) -> None:  # pragma: no cover - protocol
        ...


# project/component/transport/trace_id config accepted by metered_client.
TraceId = Union[str, Callable[[], Optional[str]], None]
