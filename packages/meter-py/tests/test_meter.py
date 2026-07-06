"""metered_client() + record() tests — mirror of packages/meter-ts/test/meter.test.ts.

Dispatch is synchronous (Python SDK + supabase-py are sync), so a CaptureTransport
holds the record as soon as create() returns — no waiting needed.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from helpers import (
    CaptureTransport,
    DownTransport,
    ThrowingTransport,
    assert_schema_shape,
    fake_client,
    fake_response,
    fake_stream,
    stream_events,
)
from meter.meter import metered_client, record
from meter.transport import FallbackTransport, JsonlFallback, SupabaseTransport

CFG = {"project": "docflow", "component": "extract_invoice"}


# ---------------------------------------------------------------------------
# Non-streaming happy path
# ---------------------------------------------------------------------------


def test_non_streaming_records_shape_and_returns_response_unchanged(example):
    response = fake_response(
        example["model"], example["tokens_in"], example["tokens_out"], "req_test_abc"
    )
    transport = CaptureTransport()
    client = metered_client(fake_client(lambda *a, **k: response), transport=transport, **CFG)

    result = client.messages.create(
        model=example["model"],
        max_tokens=100,
        messages=[{"role": "user", "content": "hi"}],
    )

    assert result is response  # identity: response passes through untouched

    assert len(transport.records) == 1
    rec = transport.records[0]
    assert_schema_shape(rec)
    assert rec["project"] == "docflow"
    assert rec["component"] == "extract_invoice"
    assert rec["model"] == example["model"]
    assert rec["tokens_in"] == example["tokens_in"]
    assert rec["tokens_out"] == example["tokens_out"]
    assert rec["cost_usd"] == example["cost_usd"]  # same tokens/model -> same cost
    assert isinstance(rec["latency_ms"], int) and rec["latency_ms"] >= 0
    assert rec["status"] == "ok"
    assert rec["error_type"] is None
    assert rec["request_id"] == "req_test_abc"
    assert rec["trace_id"] is None
    assert rec["ts"].endswith("Z")


def test_resolves_trace_id_from_callable():
    transport = CaptureTransport()
    client = metered_client(
        fake_client(lambda *a, **k: fake_response("claude-haiku-4-5", 1, 1, None)),
        transport=transport,
        trace_id=lambda: "trace-42",
        **CFG,
    )
    client.messages.create(model="claude-haiku-4-5", max_tokens=1, messages=[])
    assert transport.records[0]["trace_id"] == "trace-42"


def test_leaves_non_intercepted_members_untouched():
    base = fake_client(lambda *a, **k: fake_response("m", 1, 1, None))
    client = metered_client(base, transport=CaptureTransport(), **CFG)
    assert client.api_key == "test-key"
    assert client.messages.count_tokens() == {"input_tokens": 0}


# ---------------------------------------------------------------------------
# Streaming happy path
# ---------------------------------------------------------------------------


def test_streaming_passes_events_through_and_records_after_end():
    events = stream_events("claude-sonnet-4-6", 1000, 500)
    transport = CaptureTransport()

    def create_impl(*a, **k):
        assert k.get("stream") is True
        return fake_stream(events, "req_stream_1")

    client = metered_client(fake_client(create_impl), transport=transport, **CFG)

    stream = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=100, messages=[], stream=True
    )

    # Nothing recorded until the stream is consumed to the end.
    assert len(transport.records) == 0

    seen = [event for event in stream]
    assert seen == events  # events pass through unchanged

    assert len(transport.records) == 1
    rec = transport.records[0]
    assert_schema_shape(rec)
    assert rec["model"] == "claude-sonnet-4-6"
    assert rec["tokens_in"] == 1000  # from message_start
    assert rec["tokens_out"] == 500  # from final message_delta (cumulative)
    assert rec["cost_usd"] == 0.0105  # 1000/1M*$3 + 500/1M*$15
    assert rec["status"] == "ok"
    assert rec["request_id"] == "req_stream_1"


def test_streaming_records_accumulated_usage_on_early_break():
    transport = CaptureTransport()
    client = metered_client(
        fake_client(lambda *a, **k: fake_stream(stream_events("claude-sonnet-4-6", 800, 999))),
        transport=transport,
        **CFG,
    )
    stream = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=100, messages=[], stream=True
    )

    for event in stream:
        if getattr(event, "type", None) == "content_block_delta":
            break  # abort mid-stream
    del stream  # drop the generator so GeneratorExit fires deterministically

    assert len(transport.records) == 1
    rec = transport.records[0]
    assert rec["tokens_in"] == 800  # message_start was seen
    assert rec["tokens_out"] == 1  # final message_delta never arrived
    assert rec["status"] == "ok"


# ---------------------------------------------------------------------------
# Hard Rule 1 — transport failure never affects the wrapped call
# ---------------------------------------------------------------------------


def test_transport_down_returns_response_unchanged(capsys):
    response = fake_response("claude-sonnet-4-6", 5, 5, None)
    transport = DownTransport()
    client = metered_client(fake_client(lambda *a, **k: response), transport=transport, **CFG)

    result = client.messages.create(model="claude-sonnet-4-6", max_tokens=10, messages=[])

    assert result is response
    assert transport.calls == 1  # recording was attempted...
    assert "meter: failed to record" in capsys.readouterr().err  # ...and logged


def test_logs_to_stderr_only_once_across_failures(capsys):
    client = metered_client(
        fake_client(lambda *a, **k: fake_response("m", 1, 1, None)),
        transport=DownTransport(),
        **CFG,
    )
    client.messages.create(model="m", max_tokens=1, messages=[])
    client.messages.create(model="m", max_tokens=1, messages=[])
    assert capsys.readouterr().err.count("meter: failed to record") == 1


def test_survives_transport_that_throws_synchronously():
    response = fake_response("m", 1, 1, None)
    client = metered_client(
        fake_client(lambda *a, **k: response), transport=ThrowingTransport(), **CFG
    )
    assert client.messages.create(model="m", max_tokens=1, messages=[]) is response


def test_streaming_transport_failure_does_not_disturb_events():
    events = stream_events("claude-sonnet-4-6", 10, 20)
    client = metered_client(
        fake_client(lambda *a, **k: fake_stream(events)), transport=DownTransport(), **CFG
    )
    stream = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1, messages=[], stream=True
    )
    assert [event for event in stream] == events


def test_falls_back_to_jsonl_when_supabase_fails():
    class _FailingClient:
        def table(self, _name):
            return self

        def insert(self, _row):
            return self

        def execute(self):
            raise RuntimeError("connection refused")

    with tempfile.TemporaryDirectory() as d:
        log_path = os.path.join(d, "meter.log.jsonl")
        failing = SupabaseTransport(client=_FailingClient())
        transport = FallbackTransport(failing, JsonlFallback(log_path))
        response = fake_response("claude-sonnet-4-6", 100, 200, None)
        client = metered_client(fake_client(lambda *a, **k: response), transport=transport, **CFG)

        result = client.messages.create(model="claude-sonnet-4-6", max_tokens=10, messages=[])
        assert result is response

        lines = open(log_path, encoding="utf-8").read().strip().split("\n")
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert_schema_shape(rec)
        assert rec["tokens_in"] == 100
        assert rec["status"] == "ok"


# ---------------------------------------------------------------------------
# Hard Rule 5 — unknown model
# ---------------------------------------------------------------------------


def test_unknown_model_still_writes_record_with_null_cost():
    transport = CaptureTransport()
    client = metered_client(
        fake_client(lambda *a, **k: fake_response("claude-experimental-99", 123, 456, None)),
        transport=transport,
        **CFG,
    )
    client.messages.create(model="claude-experimental-99", max_tokens=1, messages=[])

    assert len(transport.records) == 1
    rec = transport.records[0]
    assert_schema_shape(rec)
    assert rec["model"] == "claude-experimental-99"
    assert rec["tokens_in"] == 123
    assert rec["tokens_out"] == 456
    assert rec["cost_usd"] is None  # never estimated
    assert rec["status"] == "ok"


# ---------------------------------------------------------------------------
# SDK errors — observed, classified, re-raised unchanged
# ---------------------------------------------------------------------------


def test_reraises_exact_error_and_records_status_error():
    api_error = RuntimeError("rate limited")
    api_error.status_code = 429
    api_error.request_id = "req_err_1"
    transport = CaptureTransport()

    def boom(*a, **k):
        raise api_error

    client = metered_client(fake_client(boom), transport=transport, **CFG)

    with pytest.raises(RuntimeError) as excinfo:
        client.messages.create(model="claude-sonnet-4-6", max_tokens=1, messages=[])
    assert excinfo.value is api_error

    assert len(transport.records) == 1
    rec = transport.records[0]
    assert_schema_shape(rec)
    assert rec["status"] == "error"
    assert rec["error_type"] == "rate_limit"
    assert rec["tokens_in"] is None
    assert rec["tokens_out"] is None
    assert rec["cost_usd"] is None
    assert rec["model"] == "claude-sonnet-4-6"  # from request params
    assert rec["request_id"] == "req_err_1"


def test_records_mid_stream_failure_as_error_with_partial_usage():
    boom = RuntimeError("overloaded")
    boom.status_code = 529

    class _BrokenStream:
        response = None

        def __iter__(self):
            yield stream_events("claude-sonnet-4-6", 50, 999)[0]  # message_start only
            raise boom

    transport = CaptureTransport()
    client = metered_client(fake_client(lambda *a, **k: _BrokenStream()), transport=transport, **CFG)
    stream = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=1, messages=[], stream=True
    )

    with pytest.raises(RuntimeError) as excinfo:
        for _ in stream:
            pass
    assert excinfo.value is boom

    rec = transport.records[0]
    assert rec["status"] == "error"
    assert rec["error_type"] == "api_error"
    assert rec["tokens_in"] == 50


# ---------------------------------------------------------------------------
# record() — manual hook for the host app (status "retried")
# ---------------------------------------------------------------------------


def test_record_stamps_ts_and_records_retried_verbatim():
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

    assert len(transport.records) == 1
    rec = transport.records[0]
    assert_schema_shape(rec)
    assert rec["status"] == "retried"
    assert rec["error_type"] == "rate_limit"
    assert rec["ts"].endswith("Z")


def test_record_never_raises_even_when_transport_down(capsys):
    # Must not raise.
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
    assert "meter: failed to record" in capsys.readouterr().err
