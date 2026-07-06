"""Shared test doubles — mirror of packages/meter-ts/test/helpers.ts.

SDK objects (client, responses, stream events) are faked with SimpleNamespace so
attribute access matches the real Anthropic Python SDK (pydantic models).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable, List, Optional

from meter.types import MeterRecord

# The exact key set required by spec/record.schema.json (additionalProperties: false).
SCHEMA_KEYS = sorted(
    [
        "ts",
        "project",
        "component",
        "model",
        "tokens_in",
        "tokens_out",
        "cost_usd",
        "latency_ms",
        "status",
        "error_type",
        "request_id",
        "trace_id",
    ]
)


class CaptureTransport:
    """Transport that captures records in memory (optionally failing)."""

    def __init__(self) -> None:
        self.records: List[MeterRecord] = []
        self.fail_with: Optional[Exception] = None

    def send(self, record: MeterRecord) -> None:
        if self.fail_with:
            raise self.fail_with
        self.records.append(record)


class DownTransport:
    """Transport whose send() always raises — simulates Supabase being down."""

    def __init__(self) -> None:
        self.calls = 0

    def send(self, record: MeterRecord) -> None:
        self.calls += 1
        raise RuntimeError("supabase is down")


class ThrowingTransport:
    """Transport whose send() raises immediately (worst-case misbehavior)."""

    def send(self, record: MeterRecord) -> None:
        raise RuntimeError("synchronous transport explosion")


def fake_client(create_impl: Callable[..., Any]) -> Any:
    """Minimal mocked Anthropic client: only messages.create is real."""

    messages = SimpleNamespace(
        create=create_impl,
        count_tokens=lambda *a, **k: {"input_tokens": 0},
    )
    return SimpleNamespace(api_key="test-key", messages=messages)


def fake_response(model: str, input_tokens: int, output_tokens: int, request_id: Optional[str]) -> Any:
    """A fake non-streaming Message shaped like the SDK response."""

    resp = SimpleNamespace(
        id="msg_123",
        type="message",
        role="assistant",
        model=model,
        content=[SimpleNamespace(type="text", text="done")],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )
    # The SDK attaches the request id under _request_id on the parsed response.
    object.__setattr__(resp, "_request_id", request_id)
    return resp


class FakeStream:
    """A fake SDK Stream: iterable of events, with a .response carrying the
    ``request-id`` header (how the real Stream exposes its request id)."""

    def __init__(self, events: List[Any], request_id: Optional[str] = None) -> None:
        self._events = events
        self.response = SimpleNamespace(headers={"request-id": request_id} if request_id else {})

    def __iter__(self):
        for event in self._events:
            yield event

    def close(self) -> None:
        pass


def fake_stream(events: List[Any], request_id: Optional[str] = None) -> FakeStream:
    return FakeStream(events, request_id)


def stream_events(model: str, input_tokens: int, output_tokens: int) -> List[Any]:
    """Six-event SSE sequence matching the TS helper (message_start carries
    input_tokens + output_tokens=1; the final message_delta carries the total)."""

    return [
        SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg_stream_1",
                type="message",
                role="assistant",
                model=model,
                content=[],
                stop_reason=None,
                usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=1),
            ),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="text", text=""),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="Hello"),
        ),
        SimpleNamespace(type="content_block_stop", index=0),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=output_tokens),
        ),
        SimpleNamespace(type="message_stop"),
    ]


def assert_schema_shape(record: MeterRecord) -> None:
    keys = sorted(record.keys())
    if keys != SCHEMA_KEYS:
        raise AssertionError(f"record keys {keys} != schema keys {SCHEMA_KEYS}")
