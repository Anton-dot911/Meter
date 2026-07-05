/**
 * @llm smoke test — hits the REAL Anthropic API and writes REAL rows to the
 * Supabase `llm_calls` table. Skipped automatically unless credentials are set,
 * so it never runs in the normal `pnpm -F meter-ts test` suite.
 *
 * Run manually (per docs/PLAN.md T2 DoD: "manual `@llm` smoke writes a real row"):
 *
 *   ANTHROPIC_API_KEY=sk-ant-... \
 *   SUPABASE_URL=https://<proj>.supabase.co \
 *   SUPABASE_ANON_KEY=<key or service-role key> \
 *   pnpm -F meter-ts test:llm
 *
 * Uses the cheapest model (claude-haiku-4-5) with a tiny max_tokens.
 */
import Anthropic from "@anthropic-ai/sdk";
import { createClient } from "@supabase/supabase-js";
import { describe, expect, it } from "vitest";
import { meteredClient } from "../src/meter.js";
import { SupabaseTransport } from "../src/transport.js";
import type { MeterRecord, Transport } from "../src/types.js";

const SUPA_URL = process.env.METER_SUPABASE_URL ?? process.env.SUPABASE_URL;
const SUPA_KEY =
  process.env.METER_SUPABASE_ANON_KEY ??
  process.env.SUPABASE_ANON_KEY ??
  process.env.SUPABASE_SERVICE_ROLE_KEY;
const TABLE = process.env.METER_TABLE ?? "llm_calls";

const HAS_CREDS = Boolean(process.env.ANTHROPIC_API_KEY && SUPA_URL && SUPA_KEY);

const MODEL = "claude-haiku-4-5";
const CFG = { project: "meter", component: "ts_smoke" };

/** Wraps a transport so the test can await the fire-and-forget send. */
class Awaitable implements Transport {
  settled: Promise<void> = Promise.resolve();
  last: MeterRecord | null = null;
  constructor(private readonly inner: Transport) {}
  send(record: MeterRecord): Promise<void> {
    this.last = record;
    this.settled = this.inner.send(record);
    return this.settled;
  }
}

async function readBack(requestId: string): Promise<Record<string, unknown> | null> {
  const db = createClient(SUPA_URL!, SUPA_KEY!, { auth: { persistSession: false } });
  for (let attempt = 0; attempt < 5; attempt++) {
    const { data, error } = await db
      .from(TABLE)
      .select("*")
      .eq("request_id", requestId)
      .limit(1);
    if (error) throw new Error(`readback failed: ${error.message}`);
    if (data && data.length > 0) return data[0] as Record<string, unknown>;
    await new Promise((r) => setTimeout(r, 500));
  }
  return null;
}

describe.skipIf(!HAS_CREDS)("@llm meter-ts smoke (real API + real Supabase row)", () => {
  it(
    "@llm records a real non-streaming call",
    async () => {
      const transport = new Awaitable(new SupabaseTransport({ table: TABLE }));
      const client = meteredClient(new Anthropic(), { ...CFG, transport });

      const response = await client.messages.create({
        model: MODEL,
        max_tokens: 16,
        messages: [{ role: "user", content: "Reply with the single word: ok" }],
      });

      // Recording is fire-and-forget; wait for the real Supabase insert.
      await transport.settled;

      const rec = transport.last!;
      expect(rec.status).toBe("ok");
      expect(rec.model).toBe(MODEL);
      expect(rec.tokens_in).toBeGreaterThan(0);
      expect(rec.tokens_out).toBeGreaterThan(0);
      expect(typeof rec.cost_usd).toBe("number"); // haiku is a known model
      expect(rec.request_id).toBeTruthy();
      expect(rec.request_id).toBe((response as { _request_id?: string })._request_id);

      const row = await readBack(rec.request_id!);
      expect(row, "row should be readable back from Supabase").not.toBeNull();
      expect(row!.project).toBe("meter");
      expect(row!.component).toBe("ts_smoke");
      expect(row!.model).toBe(MODEL);
      // eslint-disable-next-line no-console
      console.log(`[@llm] wrote non-streaming row request_id=${rec.request_id} cost=$${rec.cost_usd}`);
    },
    30_000,
  );

  it(
    "@llm records a real streaming call after the stream ends",
    async () => {
      const transport = new Awaitable(new SupabaseTransport({ table: TABLE }));
      const client = meteredClient(new Anthropic(), { ...CFG, transport });

      const stream = (await client.messages.create({
        model: MODEL,
        max_tokens: 16,
        messages: [{ role: "user", content: "Count to three." }],
        stream: true,
      })) as AsyncIterable<unknown>;

      let events = 0;
      for await (const _ of stream) events += 1;
      expect(events).toBeGreaterThan(0);

      await transport.settled;

      const rec = transport.last!;
      expect(rec.status).toBe("ok");
      expect(rec.model).toBe(MODEL);
      expect(rec.tokens_in).toBeGreaterThan(0); // from message_start
      expect(rec.tokens_out).toBeGreaterThan(0); // from final message_delta
      expect(typeof rec.cost_usd).toBe("number");
      expect(rec.request_id).toBeTruthy();

      const row = await readBack(rec.request_id!);
      expect(row, "streamed row should be readable back from Supabase").not.toBeNull();
      // eslint-disable-next-line no-console
      console.log(`[@llm] wrote streaming row request_id=${rec.request_id} cost=$${rec.cost_usd}`);
    },
    30_000,
  );
});
