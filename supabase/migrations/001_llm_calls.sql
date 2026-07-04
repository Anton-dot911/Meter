-- Meter: llm_calls + budgets. DDL per docs/PLAN.md (verbatim), followed by RLS.

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
-- Single-user tool: signups are disabled in this Supabase project, so the only
-- authenticated principal is the owner. Anonymous (anon-key, unauthenticated)
-- access is denied by default once RLS is enabled — no policy grants it.
alter table llm_calls enable row level security;
alter table budgets enable row level security;

create policy "owner can select llm_calls" on llm_calls
  for select to authenticated using (true);
create policy "owner can insert llm_calls" on llm_calls
  for insert to authenticated with check (true);

create policy "owner full access to budgets" on budgets
  for all to authenticated using (true) with check (true);
