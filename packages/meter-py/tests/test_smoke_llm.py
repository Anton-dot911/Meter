"""@llm smoke test — hits the REAL Anthropic API and writes REAL rows to the
Supabase ``llm_calls`` table, then reads them back by request_id. Mirror of
packages/meter-ts/test/smoke.llm.test.ts. Skipped automatically unless
credentials are set, so it never runs without creds.

Run manually (per docs/PLAN.md T3 DoD):

    METER_ANTHROPIC_API_KEY=sk-ant-... \\   # or ANTHROPIC_API_KEY
    SUPABASE_URL=https://<proj>.supabase.co \\
    SUPABASE_SERVICE_ROLE_KEY=<service-role key> \\
    uv run pytest -m llm -s

Uses the cheapest model (claude-haiku-4-5) with a tiny max_tokens.
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional

import pytest

from meter.meter import metered_client
from meter.transport import SupabaseTransport
from meter.types import MeterRecord

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
# The API resolves the alias to a dated snapshot (e.g. claude-haiku-4-5-20251001)
# and the meter records that resolved id, so assert on the alias prefix.
MODEL_RE = re.compile(rf"^{MODEL}(-\d{{8}})?$")
CFG = {"project": "meter", "component": "py_smoke"}


class _Capturing:
    """Wraps a transport to capture the last record the meter sent."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.last: Optional[MeterRecord] = None

    def send(self, record: MeterRecord) -> None:
        self.last = record
        self._inner.send(record)


def _read_back(request_id: str) -> Optional[dict]:
    from supabase import create_client

    db = create_client(SUPA_URL, SUPA_KEY)
    for _ in range(5):
        resp = db.table(TABLE).select("*").eq("request_id", request_id).limit(1).execute()
        if resp.data:
            return resp.data[0]
        time.sleep(0.5)
    return None


pytestmark = pytest.mark.skipif(not HAS_CREDS, reason="@llm smoke needs Anthropic + Supabase creds")


@pytest.mark.llm
def test_llm_records_a_real_non_streaming_call():
    import anthropic

    transport = _Capturing(SupabaseTransport(url=SUPA_URL, key=SUPA_KEY, table=TABLE))
    client = metered_client(
        anthropic.Anthropic(api_key=ANTHROPIC_KEY), transport=transport, **CFG
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=16,
        messages=[{"role": "user", "content": "Reply with the single word: ok"}],
    )

    rec = transport.last
    assert rec is not None
    assert rec["status"] == "ok"
    assert MODEL_RE.match(rec["model"])
    assert rec["tokens_in"] > 0
    assert rec["tokens_out"] > 0
    assert isinstance(rec["cost_usd"], float)  # haiku is a known model
    assert rec["request_id"]
    assert rec["request_id"] == getattr(response, "_request_id", None)

    row = _read_back(rec["request_id"])
    assert row is not None, "row should be readable back from Supabase"
    assert row["project"] == "meter"
    assert row["component"] == "py_smoke"
    assert MODEL_RE.match(row["model"])
    print(f"\n[@llm] non-streaming row:\n{row}")


@pytest.mark.llm
def test_llm_records_a_real_streaming_call():
    import anthropic

    transport = _Capturing(SupabaseTransport(url=SUPA_URL, key=SUPA_KEY, table=TABLE))
    client = metered_client(
        anthropic.Anthropic(api_key=ANTHROPIC_KEY), transport=transport, **CFG
    )

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

    rec = transport.last
    assert rec is not None
    assert rec["status"] == "ok"
    assert MODEL_RE.match(rec["model"])
    assert rec["tokens_in"] > 0  # from message_start
    assert rec["tokens_out"] > 0  # from final message_delta
    assert isinstance(rec["cost_usd"], float)
    assert rec["request_id"]  # captured from the request-id header

    row = _read_back(rec["request_id"])
    assert row is not None, "streamed row should be readable back from Supabase"
    print(f"\n[@llm] streaming row:\n{row}")
