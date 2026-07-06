# PLAN.md — Meter Implementation Plan

One task = one session. T1–T4 make Meter usable by DocFlow (Etap 0 goal); T5–T6 finish the dashboard.

---

## Contracts

### Canonical record (`spec/record.schema.json`)

```jsonc
{
  "ts": "2026-07-02T10:15:00Z",       // ISO 8601 UTC
  "project": "docflow",                // lowercase slug
  "component": "extract_invoice",      // lowercase slug
  "model": "claude-sonnet-4-6",
  "tokens_in": 1234,
  "tokens_out": 456,
  "cost_usd": 0.01234,                 // 5 decimals; null if model unknown
  "latency_ms": 2380,
  "status": "ok",                      // "ok" | "error" | "retried"
  "error_type": null,                  // e.g. "rate_limit" | "timeout" | "validation" | "api_error"
  "request_id": "req_...",             // from SDK response headers when available
  "trace_id": null                     // optional external correlation id
}
```

### DDL (`supabase/migrations/001_llm_calls.sql`)

```sql
create table llm_calls (
  id bigint generated always as identity primary key,
  ts timestamptz not null,
  project text not null,
  component text not null,
  model text not null,
  tokens_in int, tokens_out int,
  cost_usd numeric(10,5),
  latency_ms int not null,
  status text not null check (status in ('ok','error','retried')),
  error_type text,
  request_id text,
  trace_id text,
  inserted_at timestamptz not null default now()
);
create index on llm_calls (project, ts desc);
create index on llm_calls (ts desc);

create table budgets (
  project text primary key,
  monthly_limit_usd numeric(10,2) not null
);
-- RLS: enable; policies restrict all access to owner account.
```

### TS API (`packages/meter-ts`)

```ts
import Anthropic from "@anthropic-ai/sdk";

export interface MeterConfig {
  project: string;
  component: string;
  transport?: Transport;        // default: SupabaseTransport(env) with JsonlFallback("./meter.log.jsonl")
  traceId?: string | (() => string | undefined);
}

export function meteredClient(client: Anthropic, cfg: MeterConfig): Anthropic;
// Proxy: intercepts messages.create (incl. streaming — record on stream end),
// measures latency, extracts usage + request id, computes cost, records fire-and-forget.

export function record(partial: Omit<Record, "ts">): void;  // manual hook (used by host llm client for "retried")
```

### Python API (`packages/meter-py`)

```python
def metered_client(client: anthropic.Anthropic, *, project: str, component: str,
                   transport: Transport | None = None,
                   trace_id: str | Callable[[], str | None] | None = None) -> anthropic.Anthropic: ...
def record(**fields) -> None: ...
```

Both packages fulfill the `meter.record(...)` hook stubbed in scaffolder's llm module — same signature, drop-in.

### Dashboard queries (single page)
Filters: project (multi), period (7/30/90d, custom). Widgets:
1. Stat cards: total cost, calls, error rate %, p50/p95 latency
2. Line chart: cost per day (stacked by project when multi)
3. Table: 50 recent calls (ts, project, component, model, tokens, cost, latency, status)
4. Top-10 components by cost for the period
5. Budget banner: for each project with a `budgets` row, current month spend vs limit; ≥100% → red banner
6. Prices staleness warning (rule 4)

---

## Tasks

**T1. Spec + migration.** ✅ done (PR #1)
`record.schema.json`, `record.example.json`, `prices.json` (current models, verified against docs.claude.com, with `as_of`), migration applied, monorepo scaffold (via antlab-create ts-cli/py templates as base).
DoD: migration applies cleanly; `pnpm test:spec` validates example against schema.

**T2. meter-ts.** ✅ done (PR #2, merged to main)
Proxy wrapper (non-streaming + streaming), pricing, SupabaseTransport + JsonlFallback, `record()` export. Full unit suite incl. transport-down test and unknown-model test.
DoD: tests green; manual `@llm` smoke writes a real row. ✅ **@llm smoke writes a real row — done.**
Status: `pnpm -F meter-ts test` → 23 passed (both `@llm` tests run when creds are present), `build` clean, `pnpm test:spec` green. Ran `pnpm -F meter-ts test:llm` live with `METER_ANTHROPIC_API_KEY` + `SUPABASE_SERVICE_ROLE_KEY`: both smoke tests (non-streaming + streaming) hit the real Anthropic API and wrote real `ts_smoke` rows, read back from `llm_calls`. Two defects the smoke surfaced and fixed here: (1) **cost was always null** — the API resolves the alias (`claude-haiku-4-5`) to a dated snapshot (`claude-haiku-4-5-20251001`) in the response, which the meter records, but `prices.json` is keyed by alias; `computeCost` now strips a trailing `-YYYYMMDD` snapshot suffix on a miss before giving up (unknown models still → null, Hard Rule 5). (2) **streamed rows had a null `request_id`** — a raw SDK `Stream` carries no request id (it lives only on the `request-id` response header); `meteredCreate` now reads it via `APIPromise.withResponse()`, so streamed rows are traceable too. Setup recap: Claude Code cloud sessions strip the reserved `ANTHROPIC_API_KEY`, so the key is forwarded as `METER_ANTHROPIC_API_KEY` from the environment Setup script; the owner-only RLS on `llm_calls` rejects anon inserts, so the smoke uses the service-role key. NOTE for T3/T4: the library's *default* transport also uses the anon key, so real host-app writes need either an RLS INSERT policy for the anon role or a service-role key server-side — decide during Scaffolder integration. NOTE for T3: mirror the dated-snapshot price normalization in `pricing.py`.

**T3. meter-py.** ✅ done
Mirror implementation; cross-validate output against `record.example.json`.
DoD: pytest green incl. transport-down; a row from py and a row from ts are field-identical in shape. ✅
Status: `uv run pytest` → 25 passed (23 unit + 2 `@llm` smoke, which run when creds are present). Faithful mirror of meter-ts: `metered_client()` is a proxy (`_ClientProxy`/`_MessagesProxy`) that intercepts `messages.create` for non-streaming and `stream=True`, plus `record()`, `pricing.py` (same `spec/prices.json`, same dated-snapshot alias normalization), and `SupabaseTransport` + `JsonlFallback` + `FallbackTransport`. Streaming usage is extracted by wrapping the SDK Stream in `_MeteredStream`, which yields every event unchanged and records once on end (exhausted / early break via `GeneratorExit` / error), reading `tokens_in` + model from `message_start` and cumulative `tokens_out` from the final `message_delta`; the stream request id comes from the `request-id` response header (`stream.response.headers`). TS↔Py shape compat is a test that builds a record through the real meter-py path and asserts it is key-, JSON-type- and enum-identical to the SAME `spec/record.example.json` meter-ts validates against (`tests/test_compat.py`). One deliberate language difference from meter-ts: dispatch is synchronous (Python SDK + supabase-py are sync) rather than promise-based, but the observable Hard Rule 1 contract is identical — a transport failure never throws and never alters the wrapped response (test `test_transport_down_returns_response_unchanged`). Deps kept minimal: `supabase` only; `anthropic` is host-owned (dev group), duck-typed, never imported at runtime. `@llm` smoke wrote real `py_smoke` rows to `llm_calls` (non-streaming + streaming) with correct tokens/cost/latency/request_id and read them back.

**T4. Scaffolder integration.**
Replace the no-op `meter.record` stubs in scaffolder templates (ts-fullstack, py-service) with real dependency (git-based install); bump templates; run scaffolder e2e.
DoD: freshly generated py-service project writes to `llm_calls` on its llm smoke test. (This closes Etap 0.)

**T5. Dashboard.**
Single page per contract (Recharts), Supabase reads with RLS, filters in URL params.
DoD: all 6 widgets render against seeded fixture data; component tests for cost aggregation.

**T6. Budgets + polish.**
`budgets` table wiring, banner logic, staleness warning, README with screenshots, Netlify deploy.
DoD: over-budget fixture shows red banner; TZ №6 DoD checklist green.

---

## Session prompt template
> Read CLAUDE.md and docs/PLAN.md. Implement task T<N> only. Contracts are verbatim — ask before deviating. Finish with tests green and a short summary.
