"""Transports: Supabase primary + JSONL fallback (Hard Rule 1, 6, 7).

Mirror of transport.ts. Keeps deps light: the supabase client is imported lazily
so the library works with a custom transport even where supabase is absent.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from .types import MeterRecord, Transport


class SupabaseTransport(Transport):
    """Writes records to the shared Supabase ``llm_calls`` table. Raises on failure."""

    def __init__(
        self,
        *,
        url: Optional[str] = None,
        key: Optional[str] = None,
        table: str = "llm_calls",
        client: Any = None,
    ) -> None:
        self._url = url
        self._key = key
        self._table = table
        self._client = client

    def send(self, record: MeterRecord) -> None:
        self._get_client().table(self._table).insert(record).execute()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        url = self._url or os.environ.get("METER_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
        # Prefer a write-capable service-role key: the owner-only RLS on llm_calls
        # rejects anon inserts, so host apps writing real rows need it. Falls back
        # to the anon-key names for read-only / RLS-permitted setups.
        key = (
            self._key
            or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
            or os.environ.get("METER_SUPABASE_ANON_KEY")
            or os.environ.get("SUPABASE_ANON_KEY")
        )
        if not url or not key:
            raise RuntimeError(
                "meter: supabase transport not configured "
                "(SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY | SUPABASE_ANON_KEY)"
            )
        # Lazy import: only required when the default transport is actually used.
        from supabase import create_client

        self._client = create_client(url, key)
        return self._client


class JsonlFallback(Transport):
    """Appends records as JSON lines to a local file."""

    def __init__(self, path: str = "./meter.log.jsonl") -> None:
        self._path = path

    def send(self, record: MeterRecord) -> None:
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")


class FallbackTransport(Transport):
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
    """Default per contract: SupabaseTransport(env) with JsonlFallback('./meter.log.jsonl')."""
    return FallbackTransport(SupabaseTransport(), JsonlFallback("./meter.log.jsonl"))
