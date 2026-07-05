import type Anthropic from "@anthropic-ai/sdk";
import type { MeterRecord, Transport } from "../src/types.js";

/** Transport that captures records in memory (optionally failing). */
export class CaptureTransport implements Transport {
  records: MeterRecord[] = [];
  failWith: Error | null = null;

  async send(record: MeterRecord): Promise<void> {
    if (this.failWith) throw this.failWith;
    this.records.push(record);
  }
}

/** Transport whose send() always rejects — simulates Supabase being down. */
export class DownTransport implements Transport {
  calls = 0;

  async send(_record: MeterRecord): Promise<void> {
    this.calls += 1;
    throw new Error("supabase is down");
  }
}

/** Transport whose send() throws synchronously (worst-case misbehavior). */
export class ThrowingTransport implements Transport {
  send(_record: MeterRecord): Promise<void> {
    throw new Error("synchronous transport explosion");
  }
}

/** Minimal mocked Anthropic client: only messages.create is real. */
export function fakeClient(createImpl: (params: unknown, options?: unknown) => unknown): Anthropic {
  return {
    apiKey: "test-key",
    messages: {
      create: createImpl,
      countTokens: async () => ({ input_tokens: 0 }),
    },
  } as unknown as Anthropic;
}

/** A fake SSE stream shaped like the SDK's Stream<RawMessageStreamEvent>. */
export function fakeStream(events: unknown[], requestId?: string) {
  return {
    _request_id: requestId ?? null,
    controller: new AbortController(),
    async *[Symbol.asyncIterator]() {
      for (const event of events) yield event;
    },
  };
}

export function streamEvents(model: string, inputTokens: number, outputTokens: number): unknown[] {
  return [
    {
      type: "message_start",
      message: {
        id: "msg_stream_1",
        type: "message",
        role: "assistant",
        model,
        content: [],
        stop_reason: null,
        usage: { input_tokens: inputTokens, output_tokens: 1 },
      },
    },
    { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } },
    { type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "Hello" } },
    { type: "content_block_stop", index: 0 },
    { type: "message_delta", delta: { stop_reason: "end_turn" }, usage: { output_tokens: outputTokens } },
    { type: "message_stop" },
  ];
}

/** The exact key set required by spec/record.schema.json (additionalProperties: false). */
export const SCHEMA_KEYS = [
  "ts",
  "project",
  "component",
  "model",
  "tokens_in",
  "tokens_out",
  "cost_usd",
  "latency_ms",
  "status",
  "error_type",
  "request_id",
  "trace_id",
].sort();

export function assertSchemaShape(record: MeterRecord): void {
  const keys = Object.keys(record).sort();
  if (JSON.stringify(keys) !== JSON.stringify(SCHEMA_KEYS)) {
    throw new Error(`record keys ${keys.join(",")} != schema keys ${SCHEMA_KEYS.join(",")}`);
  }
}
