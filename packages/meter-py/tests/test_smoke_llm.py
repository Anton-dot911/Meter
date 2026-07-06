"""@llm smoke test — hits the REAL Anthropic API and writes REAL rows to the
Supabase ``llm_calls`` table, then reads them back by request_id. Mirror of
packages/meter-ts/test/smoke.llm.test.ts.

Skipped automatically unless credentials are set, so it never runs in the normal
suite. Run manually (docs/PLAN.md T3 DoD):

    ANTHROPIC_API_KEY=sk-ant-...  \\   # or METER_ANTHROPIC_API_KEY (see below)
    SUPABASE_URL=https://<proj>.supabase.co  \\
    SUPABASE_SERVICE_ROLE_KEY=<service-role key>  \\
    uv run pytest -m llm -s

Hosts that reserve/strip ANTHROPIC_API_KEY from the runtime (e.g. Claude Code
cloud sessions) can forward it as METER_ANTHROPIC_API_KEY; this test picks it up.
Uses the cheapest model (claude-haiku-4-5) with a tiny max_tokens.
"""

from __future__ import annotations

import os
import re
import threading
import time

import pytest

from meter import metered_client
from meter.transport import SupabaseTransport
from meter.types import MeterRecord, Transport

SUPA_URL = os.environ.get("METER_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
# The smoke performs a real INSERT + read-back. Under the owner-only RLS on
# llm_calls the anon key is rejected, so prefer a write-capable service-role key;
# fall back to anon only if that is all that is configured.
SUPA_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("METER_SUPABASE_ANON_KEY")
    or os.environ.get("SUPABASE_ANON_KEY")
)
TABLE = os.environ.get("METER_TABLE", "llm_calls")
# Some hosts strip the reserved ANTHROPIC_API_KEY; METER_ANTHROPIC_API_KEY is an
# unreserved alias the setup script can forward so the smoke can still run.
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("METER_ANTHROPIC_API_KEY")

HAS_CREDS = bool(ANTHROPIC_KEY and SUPA_URL and SUPA_KEY)

MODEL = "claude-haiku-4-5"
# The API resolves the alias to a dated snapshot (claude-haiku-4-5-20251001); the
# meter records that resolved id, so assert on the alias prefix.
MODEL_RE = re.compile(rf"^{MODEL}(-\d{{8}})?$")
CFG = {"project": "meter", "component": "py_smoke"}

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(not HAS_CREDS, reason="requires ANTHROPIC + SUPABASE credentials"),
]


class Awaitable(Transport):
    """Wraps a transport so the test can block on the fire-and-forget send."""

    def __init__(self, inner: Transport) -> None:
        self._inner = inner
        self.last: MeterRecord | None = None
        self.error: BaseException | None = None
        self.done = threading.Event()

    def send(self, record: MeterRecord) -> None:
        self.last = record
        try:
            self._inner.send(record)
        except BaseException as err:  # noqa: BLE001 - surface it to the test
            self.error = err
            raise
        finally:
            self.done.set()

    def wait(self, timeout: float = 20.0) -> None:
        assert self.done.wait(timeout), "recording never completed"
        if self.error is not None:
            raise self.error


def _read_back(request_id: str) -> dict | None:
    from supabase import create_client

    db = create_client(SUPA_URL, SUPA_KEY)
    for _ in range(5):
        res = db.table(TABLE).select("*").eq("request_id", request_id).limit(1).execute()
        if res.data:
            return res.data[0]
        time.sleep(0.5)
    return None


def _anthropic_client():
    import anthropic

    return anthropic.Anthropic(api_key=ANTHROPIC_KEY)


def test_llm_records_a_real_non_streaming_call():
    transport = Awaitable(SupabaseTransport(table=TABLE, url=SUPA_URL, key=SUPA_KEY))
    client = metered_client(_anthropic_client(), transport=transport, **CFG)

    response = client.messages.create(
        model=MODEL,
        max_tokens=16,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )
    transport.wait()

    rec = transport.last
    assert rec["status"] == "ok"
    assert MODEL_RE.match(rec["model"])
    assert rec["tokens_in"] > 0
    assert rec["tokens_out"] > 0
    assert isinstance(rec["cost_usd"], float)  # haiku is a known model
    assert rec["request_id"]
    assert rec["request_id"] == response._request_id

    row = _read_back(rec["request_id"])
    assert row is not None, "row should be readable back from Supabase"
    assert row["project"] == "meter"
    assert row["component"] == "py_smoke"
    assert MODEL_RE.match(row["model"])
    print(
        f"\n[@llm] non-streaming row read back from {TABLE}:\n"
        + _dump(row)
    )


def test_llm_records_a_real_streaming_call():
    transport = Awaitable(SupabaseTransport(table=TABLE, url=SUPA_URL, key=SUPA_KEY))
    client = metered_client(_anthropic_client(), transport=transport, **CFG)

    stream = client.messages.create(
        model=MODEL,
        max_tokens=16,
        messages=[{"role": "user", "content": "Count to three."}],
        stream=True,
    )
    events = 0
    for _ in stream:
        events += 1
    assert events > 0

    transport.wait()

    rec = transport.last
    assert rec["status"] == "ok"
    assert MODEL_RE.match(rec["model"])
    assert rec["tokens_in"] > 0  # from message_start
    assert rec["tokens_out"] > 0  # from final message_delta
    assert isinstance(rec["cost_usd"], float)
    assert rec["request_id"]  # captured from the request-id response header

    row = _read_back(rec["request_id"])
    assert row is not None, "streamed row should be readable back from Supabase"
    print(
        f"\n[@llm] streaming row read back from {TABLE}:\n"
        + _dump(row)
    )


def _dump(row: dict) -> str:
    import json

    return json.dumps(row, indent=2, sort_keys=True, default=str)
