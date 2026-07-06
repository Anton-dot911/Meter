"""TS<->Py record-shape compatibility (Hard Rule 3, T3 DoD).

A record built by meter-py must be field-identical in SHAPE to the SAME canonical
fixture spec/record.example.json that meter-ts validates against: same keys, same
JSON types, same enums. Any drift between the TS and Py record shape fails here.
"""

from __future__ import annotations

from typing import Any

from helpers import CaptureTransport, fake_client, fake_response
from meter.meter import metered_client

# The enum vocabulary the schema pins for `status`.
STATUS_ENUM = {"ok", "error", "retried"}


def _json_type(value: Any) -> str:
    """Canonical JSON type tag for a Python value (bool before int on purpose)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    raise AssertionError(f"unexpected JSON value type: {type(value)!r}")


def _build_py_record(example: dict) -> dict:
    """Produce a record through the real meter-py path using the example's values,
    so any change to how meter-py emits records is reflected here."""
    transport = CaptureTransport()
    response = fake_response(
        example["model"], example["tokens_in"], example["tokens_out"], example["request_id"]
    )
    client = metered_client(
        fake_client(lambda *a, **k: response),
        transport=transport,
        project=example["project"],
        component=example["component"],
    )
    client.messages.create(model=example["model"], max_tokens=16, messages=[])
    assert len(transport.records) == 1
    return transport.records[0]


def test_py_record_is_shape_identical_to_canonical_example(example):
    rec = _build_py_record(example)

    # 1) same keys (additionalProperties:false in both directions)
    assert set(rec.keys()) == set(example.keys()), (
        f"key drift: py-only={set(rec) - set(example)} example-only={set(example) - set(rec)}"
    )

    # 2) same JSON type per field
    for key in example:
        assert _json_type(rec[key]) == _json_type(example[key]), (
            f"type drift on {key!r}: py={_json_type(rec[key])} example={_json_type(example[key])}"
        )

    # 3) same enum vocabulary for status
    assert rec["status"] in STATUS_ENUM
    assert example["status"] in STATUS_ENUM

    # 4) value cross-check on the computed field: same model+tokens -> same cost
    #    (proves the pricing math agrees with the fixture meter-ts also matches).
    assert rec["cost_usd"] == example["cost_usd"]


def test_null_capable_fields_emit_as_null_not_missing():
    """error_type / trace_id / request_id are nullable; a record with no trace
    id and no error must carry them as explicit None (matching the example's
    null trace_id), never drop the key."""
    transport = CaptureTransport()
    client = metered_client(
        fake_client(lambda *a, **k: fake_response("claude-haiku-4-5", 1, 1, None)),
        transport=transport,
        project="p",
        component="c",
    )
    client.messages.create(model="claude-haiku-4-5", max_tokens=1, messages=[])
    rec = transport.records[0]
    assert rec["error_type"] is None
    assert rec["trace_id"] is None
    assert rec["request_id"] is None
    assert "error_type" in rec and "trace_id" in rec and "request_id" in rec
