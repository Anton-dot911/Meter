"""Shared test doubles — mirror packages/meter-ts/test/helpers.ts.

Recording is dispatched on a background daemon thread (fire-and-forget), so the
capturing transports expose ``wait_for`` to let tests block until N records land.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from meter.types import MeterRecord

SPEC_DIR = Path(__file__).resolve().parents[3] / "spec"


def load_example() -> dict:
    with open(SPEC_DIR / "record.example.json", "r", encoding="utf-8") as fh:
        return json.load(fh)


class CaptureTransport:
    """Captures records in memory (optionally failing)."""

    def __init__(self) -> None:
        self.records: list[MeterRecord] = []
        self.fail_with: Optional[Exception] = None
        self._lock = threading.Lock()

    def send(self, record: MeterRecord) -> None:
        if self.fail_with:
            raise self.fail_with
        with self._lock:
            self.records.append(record)

    def wait_for(self, n: int = 1, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.records) >= n:
                    return
            time.sleep(0.005)
        raise AssertionError(f"expected >= {n} record(s), got {len(self.records)}")


class DownTransport:
    """send() always raises — simulates Supabase being down."""

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def send(self, record: MeterRecord) -> None:
        with self._lock:
            self.calls += 1
        raise RuntimeError("supabase is down")

    def wait_for_calls(self, n: int = 1, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self.calls >= n:
                    return
            time.sleep(0.005)
        raise AssertionError(f"expected >= {n} call(s), got {self.calls}")


class FailingSupabaseClient:
    """Structural stub of the supabase client whose insert().execute() raises."""

    def __init__(self, message: str = "connection refused") -> None:
        self._message = message

    def table(self, _name: str) -> "FailingSupabaseClient":
        return self

    def insert(self, _row: Any) -> "FailingSupabaseClient":
        return self

    def execute(self) -> None:
        raise RuntimeError(self._message)


# --- Minimal object doubles shaped like the real anthropic SDK types ---------


class _Namespace:
    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


def fake_client(create_impl: Callable[..., Any]) -> Any:
    """Minimal mocked Anthropic client: only messages.create is real."""

    class _Messages:
        def create(self, *args: Any, **kwargs: Any) -> Any:
            return create_impl(*args, **kwargs)

        def count_tokens(self, *args: Any, **kwargs: Any) -> Any:
            return {"input_tokens": 0}

    class _Client:
        api_key = "test-key"
        messages = _Messages()

    return _Client()


def message_response(model: str, input_tokens: int, output_tokens: int, request_id: str) -> Any:
    """A non-streaming Message-shaped object with usage + _request_id."""
    return _Namespace(
        id="msg_123",
        type="message",
        role="assistant",
        model=model,
        usage=_Namespace(input_tokens=input_tokens, output_tokens=output_tokens),
        _request_id=request_id,
    )


def stream_events(model: str, input_tokens: int, output_tokens: int) -> list[Any]:
    """Event sequence shaped like the SDK's RawMessageStreamEvent objects."""
    return [
        _Namespace(
            type="message_start",
            message=_Namespace(
                id="msg_stream_1",
                type="message",
                role="assistant",
                model=model,
                usage=_Namespace(input_tokens=input_tokens, output_tokens=1),
            ),
        ),
        _Namespace(type="content_block_start", index=0),
        _Namespace(type="content_block_delta", index=0),
        _Namespace(type="content_block_stop", index=0),
        _Namespace(type="message_delta", usage=_Namespace(output_tokens=output_tokens)),
        _Namespace(type="message_stop"),
    ]


class FakeStream:
    """Iterable shaped like the SDK's Stream[RawMessageStreamEvent]."""

    def __init__(self, events: list[Any], request_id: Optional[str] = None) -> None:
        self._events = events
        self._request_id = request_id
        self.closed = False

    def __iter__(self):
        for event in self._events:
            yield event

    def close(self) -> None:
        self.closed = True


class RaisingStream:
    """Yields one event, then raises — simulates a mid-stream failure."""

    def __init__(self, event: Any, error: Exception, request_id: Optional[str] = None) -> None:
        self._event = event
        self._error = error
        self._request_id = request_id

    def __iter__(self):
        yield self._event
        raise self._error


# --- Cross-compat schema helpers ---------------------------------------------

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


def assert_schema_shape(record: MeterRecord) -> None:
    keys = sorted(record.keys())
    if keys != SCHEMA_KEYS:
        raise AssertionError(f"record keys {keys} != schema keys {SCHEMA_KEYS}")
