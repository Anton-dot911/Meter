"""Transports — mirror of packages/meter-ts/src/transport.ts.

SupabaseTransport writes to the shared ``llm_calls`` table; JsonlFallback appends
to a local file; FallbackTransport tries the primary and falls back on any error.
The default is SupabaseTransport(env) with JsonlFallback("./meter.log.jsonl").
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .types import MeterRecord, Transport


class SupabaseTransport:
    """Writes records to the shared Supabase ``llm_calls`` table. Raises on failure."""

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        key: Optional[str] = None,
        table: Optional[str] = None,
        client: Optional[Any] = None,
    ) -> None:
        self._client = client
        self._url = url
        self._key = key
        self._table = table or "llm_calls"

    def send(self, record: MeterRecord) -> None:
        # supabase-py raises (APIError) on a failed insert; let it propagate so the
        # fire-and-forget dispatcher / FallbackTransport can react.
        self._get_client().table(self._table).insert(record).execute()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        url = self._url or os.environ.get("METER_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
        key = (
            self._key
            or os.environ.get("METER_SUPABASE_ANON_KEY")
            or os.environ.get("SUPABASE_ANON_KEY")
        )
        if not url or not key:
            raise RuntimeError(
                "meter: supabase transport not configured (SUPABASE_URL / SUPABASE_ANON_KEY)"
            )
        from supabase import create_client  # imported lazily to keep import cost off the hot path

        self._client = create_client(url, key)
        return self._client


class JsonlFallback:
    """Appends records as JSON lines to a local file."""

    def __init__(self, path: str = "./meter.log.jsonl") -> None:
        self._path = path

    def send(self, record: MeterRecord) -> None:
        # Compact separators + ensure_ascii=False to match JS JSON.stringify output.
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class FallbackTransport:
    """Tries the primary transport; on any failure writes to the fallback instead."""

    def __init__(self, primary: Transport, fallback: Transport) -> None:
        self._primary = primary
        self._fallback = fallback

    def send(self, record: MeterRecord) -> None:
        try:
            self._primary.send(record)
        except Exception:
            self._fallback.send(record)


def default_transport() -> Transport:
    """Default per contract: SupabaseTransport(env) with JsonlFallback("./meter.log.jsonl")."""
    return FallbackTransport(SupabaseTransport(), JsonlFallback("./meter.log.jsonl"))
