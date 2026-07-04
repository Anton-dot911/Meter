# CLAUDE.md — Meter (LLM Cost/Latency Tracker)

## What this project is
Thin observability layer for all my LLM calls across projects: TS + Python wrapper packages around the Anthropic SDK writing to one Supabase table, plus a single-page dashboard. Spec: `docs/TZ.md`. Plan: `docs/PLAN.md` — one task per session.

## Stack
- Monorepo (pnpm workspaces): `packages/meter-ts` (TS lib), `packages/meter-py` (Python lib, uv), `dashboard/` (React+Vite+TS+Tailwind, Netlify)
- Storage: Supabase table `llm_calls` (shared across all projects), RLS on my account
- Tests: vitest (TS), pytest (Python)

## Hard rules
1. Meter must NEVER break the host application. Recording is fire-and-forget: transport errors are swallowed (logged to stderr once), the wrapped SDK response is returned unchanged. There is a test proving Supabase being down does not affect the call.
2. The wrapper does not modify, retry, or reinterpret SDK behavior — it only observes. Retries/backoff belong to the host app's llm client (scaffolder contract), which reports `status: "retried"` explicitly.
3. TS and Python packages write byte-compatible records: same field names, same units (usd, ms), same enums. A shared JSON fixture in `spec/record.example.json` is validated by tests in BOTH packages.
4. Cost math lives in one place per package (`pricing.ts` / `pricing.py`) driven by `spec/prices.json`. `prices.json` carries `"as_of"` date; the dashboard shows a staleness warning after 60 days. Prices are verified manually against https://docs.claude.com — never guessed.
5. Unknown model in prices → record is still written with `cost_usd = null` (never dropped, never estimated).
6. No secrets in the dashboard build; it reads Supabase with the anon key + RLS.
7. Keep both libs dependency-light: supabase client + stdlib only.

## Structure
```
spec/
  record.schema.json      # canonical record shape (source of truth)
  record.example.json
  prices.json             # {as_of, models: {"claude-sonnet-4-6": {in_per_mtok, out_per_mtok}, ...}}
packages/meter-ts/src/    # meteredClient(), pricing, transport (supabase + jsonl fallback)
packages/meter-py/meter/  # metered_client(), pricing, transport
dashboard/src/            # one page: filters, stat cards, daily cost chart, recent calls, top components
supabase/migrations/
```

## Commands
- TS: `pnpm -F meter-ts test|build` · Python: `cd packages/meter-py && uv run pytest`
- Dashboard: `pnpm -F dashboard dev`
- Cross-check record compatibility: `pnpm test:spec`

## Testing conventions
- SDK is mocked in unit tests; a `@llm`-marked smoke test per package hits the real API manually.
- Transport failure tests are mandatory (rule 1).
- Pricing tests use fixed fixtures from `spec/prices.json`, incl. unknown-model case.
