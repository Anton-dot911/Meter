import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { __resetWarnOnce, meteredClient, record } from "../src/meter.js";
import { FallbackTransport, JsonlFallback, SupabaseTransport } from "../src/transport.js";
import type { MeterRecord } from "../src/types.js";
import {
  CaptureTransport,
  DownTransport,
  ThrowingTransport,
  assertSchemaShape,
  fakeClient,
  fakeStream,
  streamEvents,
} from "./helpers.js";

const example = JSON.parse(
  readFileSync(new URL("../../../spec/record.example.json", import.meta.url), "utf8"),
) as MeterRecord;

const CFG = { project: "docflow", component: "extract_invoice" };

beforeEach(() => {
  __resetWarnOnce();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("meteredClient — non-streaming happy path", () => {
  it("records a schema-shaped record and returns the SDK response unchanged", async () => {
    const response = {
      id: "msg_123",
      type: "message",
      role: "assistant",
      model: example.model,
      content: [{ type: "text", text: "done" }],
      usage: { input_tokens: example.tokens_in, output_tokens: example.tokens_out },
      _request_id: "req_test_abc",
    };
    const transport = new CaptureTransport();
    const client = meteredClient(
      fakeClient(async () => response),
      { ...CFG, transport },
    );

    const result = await client.messages.create({
      model: example.model,
      max_tokens: 100,
      messages: [{ role: "user", content: "hi" }],
    });

    expect(result).toBe(response); // identity: response passes through untouched

    expect(transport.records).toHaveLength(1);
    const rec = transport.records[0]!;
    assertSchemaShape(rec);
    expect(rec.project).toBe("docflow");
    expect(rec.component).toBe("extract_invoice");
    expect(rec.model).toBe(example.model);
    expect(rec.tokens_in).toBe(example.tokens_in);
    expect(rec.tokens_out).toBe(example.tokens_out);
    // same tokens/model as spec/record.example.json → same cost
    expect(rec.cost_usd).toBe(example.cost_usd);
    expect(Number.isInteger(rec.latency_ms)).toBe(true);
    expect(rec.latency_ms).toBeGreaterThanOrEqual(0);
    expect(rec.status).toBe("ok");
    expect(rec.error_type).toBeNull();
    expect(rec.request_id).toBe("req_test_abc");
    expect(rec.trace_id).toBeNull();
    expect(new Date(rec.ts).toString()).not.toBe("Invalid Date");
  });

  it("resolves traceId from a function", async () => {
    const transport = new CaptureTransport();
    const client = meteredClient(
      fakeClient(async () => ({ model: "claude-haiku-4-5", usage: { input_tokens: 1, output_tokens: 1 } })),
      { ...CFG, transport, traceId: () => "trace-42" },
    );
    await client.messages.create({ model: "claude-haiku-4-5", max_tokens: 1, messages: [] });
    expect(transport.records[0]!.trace_id).toBe("trace-42");
  });

  it("leaves non-intercepted client members untouched", async () => {
    const base = fakeClient(async () => ({}));
    const client = meteredClient(base, { ...CFG, transport: new CaptureTransport() });
    expect(client.apiKey).toBe("test-key");
    await expect(client.messages.countTokens({} as never)).resolves.toEqual({ input_tokens: 0 });
  });
});

describe("meteredClient — streaming happy path", () => {
  it("passes every event through unchanged and records after the stream ends", async () => {
    const events = streamEvents("claude-sonnet-4-6", 1000, 500);
    const transport = new CaptureTransport();
    const client = meteredClient(
      fakeClient((params) => {
        expect((params as { stream?: boolean }).stream).toBe(true);
        return fakeStream(events, "req_stream_1");
      }),
      { ...CFG, transport },
    );

    const stream = (await client.messages.create({
      model: "claude-sonnet-4-6",
      max_tokens: 100,
      messages: [],
      stream: true,
    })) as AsyncIterable<unknown>;

    // Nothing recorded until the stream is consumed to the end.
    expect(transport.records).toHaveLength(0);

    const seen: unknown[] = [];
    for await (const event of stream) seen.push(event);

    expect(seen).toEqual(events); // events pass through unchanged

    expect(transport.records).toHaveLength(1);
    const rec = transport.records[0]!;
    assertSchemaShape(rec);
    expect(rec.model).toBe("claude-sonnet-4-6");
    expect(rec.tokens_in).toBe(1000); // from message_start
    expect(rec.tokens_out).toBe(500); // from final message_delta (cumulative)
    // 1000/1M * $3 + 500/1M * $15 = 0.003 + 0.0075
    expect(rec.cost_usd).toBe(0.0105);
    expect(rec.status).toBe("ok");
    expect(rec.request_id).toBe("req_stream_1");
  });

  it("records accumulated usage when the consumer breaks early", async () => {
    const transport = new CaptureTransport();
    const client = meteredClient(
      fakeClient(() => fakeStream(streamEvents("claude-sonnet-4-6", 800, 999))),
      { ...CFG, transport },
    );
    const stream = (await client.messages.create({
      model: "claude-sonnet-4-6",
      max_tokens: 100,
      messages: [],
      stream: true,
    })) as AsyncIterable<{ type?: string }>;

    for await (const event of stream) {
      if (event.type === "content_block_delta") break; // abort mid-stream
    }

    expect(transport.records).toHaveLength(1);
    const rec = transport.records[0]!;
    expect(rec.tokens_in).toBe(800); // message_start was seen
    expect(rec.tokens_out).toBe(1); // final message_delta never arrived
    expect(rec.status).toBe("ok");
  });
});

describe("Hard Rule 1 — transport failure never affects the wrapped call", () => {
  it("returns the response unchanged when the transport rejects (Supabase down)", async () => {
    const stderr = vi.spyOn(console, "error").mockImplementation(() => {});
    const response = { model: "claude-sonnet-4-6", usage: { input_tokens: 5, output_tokens: 5 } };
    const transport = new DownTransport();
    const client = meteredClient(
      fakeClient(async () => response),
      { ...CFG, transport },
    );

    const result = await client.messages.create({
      model: "claude-sonnet-4-6",
      max_tokens: 10,
      messages: [],
    });

    expect(result).toBe(response);
    expect(transport.calls).toBe(1); // recording was attempted…
    await vi.waitFor(() => expect(stderr).toHaveBeenCalledTimes(1)); // …and logged once
  });

  it("logs to stderr only once across repeated transport failures", async () => {
    const stderr = vi.spyOn(console, "error").mockImplementation(() => {});
    const client = meteredClient(
      fakeClient(async () => ({ model: "m", usage: { input_tokens: 1, output_tokens: 1 } })),
      { ...CFG, transport: new DownTransport() },
    );
    await client.messages.create({ model: "m", max_tokens: 1, messages: [] });
    await client.messages.create({ model: "m", max_tokens: 1, messages: [] });
    await vi.waitFor(() => expect(stderr).toHaveBeenCalledTimes(1));
  });

  it("survives a transport that throws synchronously", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const response = { model: "m", usage: { input_tokens: 1, output_tokens: 1 } };
    const client = meteredClient(
      fakeClient(async () => response),
      { ...CFG, transport: new ThrowingTransport() },
    );
    await expect(
      client.messages.create({ model: "m", max_tokens: 1, messages: [] }),
    ).resolves.toBe(response);
  });

  it("streaming: transport failure does not disturb the event stream", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    const events = streamEvents("claude-sonnet-4-6", 10, 20);
    const client = meteredClient(
      fakeClient(() => fakeStream(events)),
      { ...CFG, transport: new DownTransport() },
    );
    const stream = (await client.messages.create({
      model: "claude-sonnet-4-6",
      max_tokens: 1,
      messages: [],
      stream: true,
    })) as AsyncIterable<unknown>;
    const seen: unknown[] = [];
    for await (const event of stream) seen.push(event);
    expect(seen).toEqual(events);
  });

  it("falls back to JSONL when Supabase fails, without touching the response", async () => {
    const dir = mkdtempSync(join(tmpdir(), "meter-test-"));
    const logPath = join(dir, "meter.log.jsonl");
    const failingSupabase = new SupabaseTransport({
      client: {
        from: () => ({
          insert: async () => ({ error: { message: "connection refused" } }),
        }),
      },
    });
    const transport = new FallbackTransport(failingSupabase, new JsonlFallback(logPath));
    const response = {
      model: "claude-sonnet-4-6",
      usage: { input_tokens: 100, output_tokens: 200 },
    };
    const client = meteredClient(
      fakeClient(async () => response),
      { ...CFG, transport },
    );

    const result = await client.messages.create({
      model: "claude-sonnet-4-6",
      max_tokens: 10,
      messages: [],
    });
    expect(result).toBe(response);

    await vi.waitFor(() => {
      const lines = readFileSync(logPath, "utf8").trim().split("\n");
      expect(lines).toHaveLength(1);
      const rec = JSON.parse(lines[0]!) as MeterRecord;
      assertSchemaShape(rec);
      expect(rec.tokens_in).toBe(100);
      expect(rec.status).toBe("ok");
    });
  });
});

describe("Hard Rule 5 — unknown model", () => {
  it("still writes the record with cost_usd = null", async () => {
    const transport = new CaptureTransport();
    const client = meteredClient(
      fakeClient(async () => ({
        model: "claude-experimental-99",
        usage: { input_tokens: 123, output_tokens: 456 },
      })),
      { ...CFG, transport },
    );
    await client.messages.create({ model: "claude-experimental-99", max_tokens: 1, messages: [] });

    expect(transport.records).toHaveLength(1);
    const rec = transport.records[0]!;
    assertSchemaShape(rec);
    expect(rec.model).toBe("claude-experimental-99");
    expect(rec.tokens_in).toBe(123);
    expect(rec.tokens_out).toBe(456);
    expect(rec.cost_usd).toBeNull(); // never estimated
    expect(rec.status).toBe("ok");
  });
});

describe("SDK errors — observed, classified, rethrown unchanged", () => {
  it("rethrows the exact error and records status error", async () => {
    const apiError = Object.assign(new Error("rate limited"), {
      status: 429,
      request_id: "req_err_1",
    });
    const transport = new CaptureTransport();
    const client = meteredClient(
      fakeClient(async () => {
        throw apiError;
      }),
      { ...CFG, transport },
    );

    await expect(
      client.messages.create({ model: "claude-sonnet-4-6", max_tokens: 1, messages: [] }),
    ).rejects.toBe(apiError);

    expect(transport.records).toHaveLength(1);
    const rec = transport.records[0]!;
    assertSchemaShape(rec);
    expect(rec.status).toBe("error");
    expect(rec.error_type).toBe("rate_limit");
    expect(rec.tokens_in).toBeNull();
    expect(rec.tokens_out).toBeNull();
    expect(rec.cost_usd).toBeNull();
    expect(rec.model).toBe("claude-sonnet-4-6"); // from request params
    expect(rec.request_id).toBe("req_err_1");
  });

  it("records a mid-stream failure as error with partial usage, rethrowing to the consumer", async () => {
    const boom = Object.assign(new Error("overloaded"), { status: 529 });
    const brokenStream = {
      _request_id: "req_stream_err",
      async *[Symbol.asyncIterator]() {
        yield streamEvents("claude-sonnet-4-6", 50, 999)[0]; // message_start only
        throw boom;
      },
    };
    const transport = new CaptureTransport();
    const client = meteredClient(
      fakeClient(() => brokenStream),
      { ...CFG, transport },
    );
    const stream = (await client.messages.create({
      model: "claude-sonnet-4-6",
      max_tokens: 1,
      messages: [],
      stream: true,
    })) as AsyncIterable<unknown>;

    await expect(async () => {
      for await (const _ of stream) {
        /* consume */
      }
    }).rejects.toBe(boom);

    const rec = transport.records[0]!;
    expect(rec.status).toBe("error");
    expect(rec.error_type).toBe("api_error");
    expect(rec.tokens_in).toBe(50);
  });
});

describe("record() — manual hook for the host app", () => {
  it("stamps ts and records status retried verbatim", async () => {
    const transport = new CaptureTransport();
    record(
      {
        project: "docflow",
        component: "extract_invoice",
        model: "claude-sonnet-4-6",
        tokens_in: 10,
        tokens_out: 20,
        cost_usd: 0.00033,
        latency_ms: 1500,
        status: "retried",
        error_type: "rate_limit",
        request_id: "req_retry_1",
        trace_id: "trace-7",
      },
      transport,
    );

    await vi.waitFor(() => expect(transport.records).toHaveLength(1));
    const rec = transport.records[0]!;
    assertSchemaShape(rec);
    expect(rec.status).toBe("retried");
    expect(rec.error_type).toBe("rate_limit");
    expect(new Date(rec.ts).toString()).not.toBe("Invalid Date");
  });

  it("never throws even when the transport is down", () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    expect(() =>
      record(
        {
          project: "p",
          component: "c",
          model: "m",
          tokens_in: null,
          tokens_out: null,
          cost_usd: null,
          latency_ms: 0,
          status: "retried",
          error_type: null,
          request_id: null,
          trace_id: null,
        },
        new DownTransport(),
      ),
    ).not.toThrow();
  });
});
