"""Unit suite for metered_client() / record() — mirror of meter-ts's meter.test.ts.

The SDK is mocked; recording is dispatched on a background thread, so capturing
transports expose wait_for()/wait_for_calls() to keep assertions deterministic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import wait_until_warned
from helpers import (
    CaptureTransport,
    DownTransport,
    FailingSupabaseClient,
    FakeStream,
    RaisingStream,
    assert_schema_shape,
    fake_client,
    load_example,
    message_response,
    stream_events,
)
from meter import metered_client, record
from meter.transport import FallbackTransport, JsonlFallback, SupabaseTransport

EXAMPLE = load_example()
CFG = {"project": "docflow", "component": "extract_invoice"}


# --- non-streaming happy path -------------------------------------------------


def test_non_streaming_records_shape_and_returns_response_unchanged():
    response = message_response(
        EXAMPLE["model"], EXAMPLE["tokens_in"], EXAMPLE["tokens_out"], "req_test_abc"
    )
    transport = CaptureTransport()
    client = metered_client(fake_client(lambda *a, **k: response), transport=transport, **CFG)

    result = client.messages.create(
        model=EXAMPLE["model"],
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result is response  # identity: response passes through untouched

    transport.wait_for(1)
    rec = transport.records[0]
    assert_schema_shape(rec)
    assert rec["project"] == "docflow"
    assert rec["component"] == "extract_invoice"
    assert rec["model"] == EXAMPLE["model"]
    assert rec["tokens_in"] == EXAMPLE["tokens_in"]
    assert rec["tokens_out"] == EXAMPLE["tokens_out"]
    # same tokens/model as spec/record.example.json -> same cost
    assert rec["cost_usd"] == EXAMPLE["cost_usd"]
    assert isinstance(rec["latency_ms"], int) and rec["latency_ms"] >= 0
    assert rec["status"] == "ok"
    assert rec["error_type"] is None
    assert rec["request_id"] == "req_test_abc"
    assert rec["trace_id"] is None


def test_resolves_trace_id_from_callable():
    response = message_response("claude-haiku-4-5", 1, 1, "req_1")
    transport = CaptureTransport()
    client = metered_client(
        fake_client(lambda *a, **k: response), transport=transport, trace_id=lambda: "trace-42", **CFG
    )
    client.messages.create(model="claude-haiku-4-5", max_tokens=1, messages=[])
    transport.wait_for(1)
    assert transport.records[0]["trace_id"] == "trace-42"


def test_non_intercepted_members_pass_through():
    client = metered_client(fake_client(lambda *a, **k: {}), transport=CaptureTransport(), **CFG)
    assert client.api_key == "test-key"
    assert client.messages.count_tokens() == {"input_tokens": 0}


# --- streaming happy path -----------------------------------------------------


def test_streaming_passes_events_through_and_records_on_end():
    events = stream_events("claude-sonnet-4-6", 1000, 500)
    transport = CaptureTransport()

    def create(*args, **kwargs):
        assert kwargs.get("stream") is True
        return FakeStream(events, request_id="req_stream_1")

    client = metered_client(fake_client(create), transport=transport, **CFG)
    stream = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=100, messages=[], stream=True
    )

    # Nothing recorded until the stream is consumed to the end.
    assert transport.records == []

    seen = [event for event in stream]
    assert seen == events  # events pass through unchanged

    transport.wait_for(1)
    rec = transport.records[0]
    assert_schema_shape(rec)
    assert rec["model"] == "claude-sonnet-4-6"
    assert rec["tokens_in"] == 1000  # from message_start
    assert rec["tokens_out"] == 500  # from final message_delta (cumulative)
    # 1000/1M * $3 + 500/1M * $15 = 0.003 + 0.0075
    assert rec["cost_usd"] == 0.0105
    assert rec["status"] == "ok"
    assert rec["request_id"] == "req_stream_1"


def test_streaming_records_accumulated_usage_on_early_break():
    transport = CaptureTransport()
    client = metered_client(
        fake_client(lambda *a, **k: FakeStream(stream_events("claude-sonnet-4-6", 800, 999))),
        transport=transport,
        **CFG,
    )
    stream = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=100, messages=[], stream=True
    )

    for event in stream:
        if event.type == "content_block_delta":
            break  # abort mid-stream

    transport.wait_for(1)
    rec = transport.records[0]
    assert rec["tokens_in"] == 800  # message_start was seen
    assert rec["tokens_out"] == 1  # final message_delta never arrived
    assert rec["status"] == "ok"


# --- Hard Rule 1: transport failure never affects the wrapped call ------------


def test_transport_down_returns_response_unchanged_and_logs_once(capsys):
    response = message_response("claude-sonnet-4-6", 5, 5, "req_x")
    transport = DownTransport()
    client = metered_client(fake_client(lambda *a, **k: response), transport=transport, **CFG)

    result = client.messages.create(model="claude-sonnet-4-6", max_tokens=10, messages=[])

    assert result is response
    transport.wait_for_calls(1)  # recording was attempted...
    wait_until_warned()  # ...and logged
    assert capsys.readouterr().err.count("meter: failed to record llm call") == 1


def test_logs_to_stderr_only_once_across_repeated_failures(capsys):
    transport = DownTransport()
    client = metered_client(
        fake_client(lambda *a, **k: message_response("m", 1, 1, "r")), transport=transport, **CFG
    )
    client.messages.create(model="m", max_tokens=1, messages=[])
    client.messages.create(model="m", max_tokens=1, messages=[])
    transport.wait_for_calls(2)
    wait_until_warned()
    assert capsys.readouterr().err.count("meter: failed to record llm call") == 1


def test_survives_transport_that_raises_synchronously():
    class ThrowingTransport:
        def send(self, record):
            raise RuntimeError("synchronous transport explosion")

    response = message_response("m", 1, 1, "r")
    client = metered_client(
        fake_client(lambda *a, **k: response), transport=ThrowingTransport(), **CFG
    )
    # Dispatch runs on a thread; the raise is swallowed there and never reaches here.
    assert client.messages.create(model="m", max_tokens=1, messages=[]) is response


def test_streaming_transport_failure_does_not_disturb_events():
    events = stream_events("claude-sonnet-4-6", 10, 20)
    client = metered_client(
        fake_client(lambda *a, **k: FakeStream(events)), transport=DownTransport(), **CFG
    )
    stream = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1, messages=[], stream=True
    )
    seen = [event for event in stream]
    assert seen == events


def test_falls_back_to_jsonl_when_supabase_fails(tmp_path):
    log_path = tmp_path / "meter.log.jsonl"
    failing_supabase = SupabaseTransport(client=FailingSupabaseClient("connection refused"))
    transport = FallbackTransport(failing_supabase, JsonlFallback(str(log_path)))
    response = message_response("claude-sonnet-4-6", 100, 200, "req_fb")
    client = metered_client(fake_client(lambda *a, **k: response), transport=transport, **CFG)

    result = client.messages.create(model="claude-sonnet-4-6", max_tokens=10, messages=[])
    assert result is response

    _wait_for_file(log_path)
    lines = Path(log_path).read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert_schema_shape(rec)
    assert rec["tokens_in"] == 100
    assert rec["status"] == "ok"


# --- Hard Rule 5: unknown model ----------------------------------------------


def test_unknown_model_writes_record_with_cost_none():
    response = message_response("claude-experimental-99", 123, 456, "req_u")
    transport = CaptureTransport()
    client = metered_client(fake_client(lambda *a, **k: response), transport=transport, **CFG)
    client.messages.create(model="claude-experimental-99", max_tokens=1, messages=[])

    transport.wait_for(1)
    rec = transport.records[0]
    assert_schema_shape(rec)
    assert rec["model"] == "claude-experimental-99"
    assert rec["tokens_in"] == 123
    assert rec["tokens_out"] == 456
    assert rec["cost_usd"] is None  # never estimated
    assert rec["status"] == "ok"


# --- SDK errors: observed, classified, rethrown unchanged ---------------------


def test_error_is_rethrown_and_recorded():
    api_error = RuntimeError("rate limited")
    api_error.status_code = 429
    api_error.request_id = "req_err_1"

    def boom(*args, **kwargs):
        raise api_error

    transport = CaptureTransport()
    client = metered_client(fake_client(boom), transport=transport, **CFG)

    with pytest.raises(RuntimeError) as excinfo:
        client.messages.create(model="claude-sonnet-4-6", max_tokens=1, messages=[])
    assert excinfo.value is api_error

    transport.wait_for(1)
    rec = transport.records[0]
    assert_schema_shape(rec)
    assert rec["status"] == "error"
    assert rec["error_type"] == "rate_limit"
    assert rec["tokens_in"] is None
    assert rec["tokens_out"] is None
    assert rec["cost_usd"] is None
    assert rec["model"] == "claude-sonnet-4-6"  # from request params
    assert rec["request_id"] == "req_err_1"


def test_mid_stream_failure_recorded_as_error_with_partial_usage():
    boom = RuntimeError("overloaded")
    boom.status_code = 529
    start_event = stream_events("claude-sonnet-4-6", 50, 999)[0]
    transport = CaptureTransport()
    client = metered_client(
        fake_client(lambda *a, **k: RaisingStream(start_event, boom, request_id="req_stream_err")),
        transport=transport,
        **CFG,
    )
    stream = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1, messages=[], stream=True
    )

    with pytest.raises(RuntimeError) as excinfo:
        for _ in stream:
            pass
    assert excinfo.value is boom

    transport.wait_for(1)
    rec = transport.records[0]
    assert rec["status"] == "error"
    assert rec["error_type"] == "api_error"
    assert rec["tokens_in"] == 50


# --- record(): manual hook for the host app ----------------------------------


def test_record_stamps_ts_and_records_retried_verbatim():
    from datetime import datetime

    transport = CaptureTransport()
    record(
        transport=transport,
        project="docflow",
        component="extract_invoice",
        model="claude-sonnet-4-6",
        tokens_in=10,
        tokens_out=20,
        cost_usd=0.00033,
        latency_ms=1500,
        status="retried",
        error_type="rate_limit",
        request_id="req_retry_1",
        trace_id="trace-7",
    )

    transport.wait_for(1)
    rec = transport.records[0]
    assert_schema_shape(rec)
    assert rec["status"] == "retried"
    assert rec["error_type"] == "rate_limit"
    datetime.fromisoformat(rec["ts"].replace("Z", "+00:00"))  # parses => valid ISO


def test_record_never_raises_even_when_transport_down():
    # Fire-and-forget on a background thread: this must return without raising.
    record(
        transport=DownTransport(),
        project="p",
        component="c",
        model="m",
        tokens_in=None,
        tokens_out=None,
        cost_usd=None,
        latency_ms=0,
        status="retried",
        error_type=None,
        request_id=None,
        trace_id=None,
    )


# --- helpers -----------------------------------------------------------------


def _wait_for_file(path, timeout: float = 3.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists() and Path(path).read_text(encoding="utf-8").strip():
            return
        time.sleep(0.005)
    raise AssertionError(f"file {path} never got content")
