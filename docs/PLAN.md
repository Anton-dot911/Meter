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

**T1. Spec + migration.**
`record.schema.json`, `record.example.json`, `prices.json` (current models, verified against docs.claude.com, with `as_of`), migration applied, monorepo scaffold (via antlab-create ts-cli/py templates as base).
DoD: migration applies cleanly; `pnpm test:spec` validates example against schema.

**T2. meter-ts.**
Proxy wrapper (non-streaming + streaming), pricing, SupabaseTransport + JsonlFallback, `record()` export. Full unit suite incl. transport-down test and unknown-model test.
DoD: tests green; manual `@llm` smoke writes a real row.

**T3. meter-py.**
Mirror implementation; cross-validate output against `record.example.json`.
DoD: pytest green incl. transport-down; a row from py and a row from ts are field-identical in shape.

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
