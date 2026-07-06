"""TS <-> Py cross-language record-shape compatibility (Hard Rule 3).

Builds a record via meter-py and asserts it is field-identical in SHAPE to the
canonical spec/record.example.json — the same example meter-ts validates against
in `pnpm test:spec` (scripts/validate-spec.mjs). It also validates the produced
record against spec/record.schema.json with jsonschema — the Python analogue of
the ajv check the TS side runs — so any drift between the TS and Py record shape
(missing/extra key, wrong type, bad enum) fails this test.

jsonschema is a test-only dev dependency (not shipped at runtime); it mirrors the
repo's existing ajv-based spec validation for the Python package.
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import CaptureTransport, fake_client, load_example, message_response
from jsonschema import Draft7Validator

from meter import metered_client

SPEC_DIR = Path(__file__).resolve().parents[3] / "spec"


def _load_schema() -> dict:
    with open(SPEC_DIR / "record.schema.json", "r", encoding="utf-8") as fh:
        return json.load(fh)


def _build_record_via_meter_py() -> dict:
    """Drive the real wrapper with the canonical example's inputs to get a record."""
    example = load_example()
    response = message_response(
        example["model"], example["tokens_in"], example["tokens_out"], example["request_id"]
    )
    transport = CaptureTransport()
    client = metered_client(
        fake_client(lambda *a, **k: response),
        project=example["project"],
        component=example["component"],
        transport=transport,
    )
    client.messages.create(model=example["model"], max_tokens=100, messages=[])
    transport.wait_for(1)
    return dict(transport.records[0])


def test_py_record_validates_against_canonical_schema():
    record = _build_record_via_meter_py()
    errors = sorted(Draft7Validator(_load_schema()).iter_errors(record), key=lambda e: e.path)
    assert not errors, "record does not match spec/record.schema.json: " + "; ".join(
        f"{list(e.path) or '(root)'}: {e.message}" for e in errors
    )


def test_py_record_is_field_identical_in_shape_to_example():
    record = _build_record_via_meter_py()
    example = load_example()

    # Same keys (exact set — the schema is additionalProperties:false).
    assert sorted(record.keys()) == sorted(example.keys())

    # Same JSON type per field as the canonical example.
    for key, example_value in example.items():
        assert _json_type(record[key]) == _json_type(example_value), (
            f"field {key!r}: py type {_json_type(record[key])} "
            f"!= example type {_json_type(example_value)}"
        )

    # Same enum value for status (ok/error/retried vocabulary).
    assert record["status"] == example["status"] == "ok"

    # The record must serialize (byte-compatible transport payload with TS).
    json.dumps(record)


def _json_type(value: object) -> str:
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
    if isinstance(value, list):
        return "array"
    return "object"
