import type Anthropic from "@anthropic-ai/sdk";
import { computeCost } from "./pricing.js";
import { defaultTransport } from "./transport.js";
import type { MeterConfig, MeterRecord, Transport } from "./types.js";

// ---------------------------------------------------------------------------
// Fire-and-forget dispatch (Hard Rule 1: meter must NEVER break the host app).
// ---------------------------------------------------------------------------

let warned = false;

function warnOnce(err: unknown): void {
  if (warned) return;
  warned = true;
  const msg = err instanceof Error ? err.message : String(err);
  console.error(`meter: failed to record llm call (further transport errors will be silent): ${msg}`);
}

/** @internal test hook */
export function __resetWarnOnce(): void {
  warned = false;
}

function dispatch(transport: Transport, record: MeterRecord): void {
  try {
    void transport.send(record).then(undefined, warnOnce);
  } catch (err) {
    warnOnce(err);
  }
}

let lazyDefault: Transport | null = null;

function getDefaultTransport(): Transport {
  return (lazyDefault ??= defaultTransport());
}

// ---------------------------------------------------------------------------
// Manual record() hook (used by the host llm client for status: "retried").
// ---------------------------------------------------------------------------

/**
 * Write one record manually; `ts` is stamped here. Fire-and-forget.
 * `transport` is optional and defaults to SupabaseTransport(env) + JsonlFallback.
 */
export function record(partial: Omit<MeterRecord, "ts">, transport?: Transport): void {
  dispatch(transport ?? getDefaultTransport(), { ts: new Date().toISOString(), ...partial });
}

// ---------------------------------------------------------------------------
// meteredClient — proxy around the Anthropic SDK client.
// ---------------------------------------------------------------------------

/**
 * Wrap an Anthropic client so every messages.create call (non-streaming and
 * stream: true) is recorded. The wrapper only observes: the SDK response,
 * stream events, and thrown errors pass through unchanged.
 */
export function meteredClient(client: Anthropic, cfg: MeterConfig): Anthropic {
  const transport = cfg.transport ?? getDefaultTransport();
  return new Proxy(client, {
    get(target, prop, _receiver) {
      if (prop === "messages") {
        const messages = target.messages;
        return new Proxy(messages, {
          get(mTarget, mProp) {
            if (mProp === "create") {
              return (params: unknown, options?: unknown) =>
                meteredCreate(mTarget, params as CreateParams, options, cfg, transport);
            }
            const value = Reflect.get(mTarget, mProp, mTarget);
            return typeof value === "function" ? value.bind(mTarget) : value;
          },
        });
      }
      const value = Reflect.get(target, prop, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

interface CreateParams {
  model?: string;
  stream?: boolean;
}

/** The subset of the SDK's APIPromise the meter uses to read the request-id header. */
interface WithResponse {
  withResponse(): Promise<{ data: unknown; request_id?: string | null }>;
}

function hasWithResponse(value: unknown): value is WithResponse {
  return typeof (value as { withResponse?: unknown })?.withResponse === "function";
}

async function meteredCreate(
  messages: Anthropic["messages"],
  params: CreateParams,
  options: unknown,
  cfg: MeterConfig,
  transport: Transport,
): Promise<unknown> {
  const ts = new Date().toISOString();
  const start = Date.now();

  const emit = (fields: {
    model: string | null | undefined;
    tokens_in: number | null;
    tokens_out: number | null;
    status: "ok" | "error";
    error_type: string | null;
    request_id: string | null;
  }): void => {
    try {
      const model = fields.model || params?.model || "unknown";
      dispatch(transport, {
        ts,
        project: cfg.project,
        component: cfg.component,
        model,
        tokens_in: fields.tokens_in,
        tokens_out: fields.tokens_out,
        cost_usd: computeCost(model, fields.tokens_in, fields.tokens_out),
        latency_ms: Math.max(0, Math.round(Date.now() - start)),
        status: fields.status,
        error_type: fields.error_type,
        request_id: fields.request_id,
        trace_id: resolveTraceId(cfg),
      });
    } catch (err) {
      warnOnce(err);
    }
  };

  let result: unknown;
  // The SDK attaches _request_id to a non-streaming response, but a raw
  // Stream carries no request id — it lives only on the `request-id` response
  // header, reachable via APIPromise.withResponse(). Capturing it here means
  // streamed rows are traceable too. withResponse() returns the same parsed
  // value as awaiting the promise, so the host still sees an unchanged object.
  let headerRequestId: string | null = null;
  try {
    const pending: unknown = messages.create(params as never, options as never);
    if (hasWithResponse(pending)) {
      const wr = await pending.withResponse();
      result = wr.data;
      headerRequestId = wr.request_id ?? null;
    } else {
      result = await (pending as Promise<unknown>);
    }
  } catch (err) {
    emit({
      model: params?.model,
      tokens_in: null,
      tokens_out: null,
      status: "error",
      error_type: classifyError(err),
      request_id: requestIdOf(err),
    });
    throw err;
  }

  if (params?.stream) {
    return wrapStream(result as AsyncIterable<StreamEvent>, headerRequestId, (usage, err) => {
      emit({
        model: usage.model,
        tokens_in: usage.inputTokens,
        tokens_out: usage.outputTokens,
        status: err === undefined ? "ok" : "error",
        error_type: err === undefined ? null : classifyError(err),
        request_id: usage.requestId,
      });
    });
  }

  const response = result as {
    model?: string;
    usage?: { input_tokens?: number; output_tokens?: number };
    _request_id?: string | null;
  };
  emit({
    model: response?.model,
    tokens_in: response?.usage?.input_tokens ?? null,
    tokens_out: response?.usage?.output_tokens ?? null,
    status: "ok",
    error_type: null,
    request_id: response?._request_id ?? headerRequestId,
  });
  return result;
}

function resolveTraceId(cfg: MeterConfig): string | null {
  try {
    const t = typeof cfg.traceId === "function" ? cfg.traceId() : cfg.traceId;
    return t ?? null;
  } catch {
    return null;
  }
}

function requestIdOf(source: unknown): string | null {
  const id = (source as { request_id?: unknown })?.request_id;
  return typeof id === "string" ? id : null;
}

/** Map SDK/network errors onto the spec's error_type vocabulary. */
function classifyError(err: unknown): string {
  const e = err as { status?: number; name?: string; code?: string } | null;
  const name = e?.name ?? "";
  if (name.includes("Timeout") || e?.code === "ETIMEDOUT" || e?.status === 408) return "timeout";
  if (e?.status === 429) return "rate_limit";
  if (e?.status === 400 || e?.status === 422) return "validation";
  return "api_error";
}

// ---------------------------------------------------------------------------
// Streaming: observe events as the consumer iterates; record on stream end.
// ---------------------------------------------------------------------------

interface StreamEvent {
  type?: string;
  message?: { model?: string; usage?: { input_tokens?: number; output_tokens?: number } };
  usage?: { output_tokens?: number };
}

interface StreamUsage {
  model: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  requestId: string | null;
}

function wrapStream<S extends AsyncIterable<StreamEvent>>(
  stream: S,
  seedRequestId: string | null,
  onEnd: (usage: StreamUsage, err?: unknown) => void,
): S {
  const usage: StreamUsage = {
    model: null,
    inputTokens: null,
    outputTokens: null,
    // Prefer the request-id header captured from the create call; fall back to
    // _request_id (set on the SDK's higher-level MessageStream and test fakes).
    requestId: seedRequestId ?? (stream as { _request_id?: string | null })?._request_id ?? null,
  };
  let ended = false;
  const finish = (err?: unknown): void => {
    if (ended) return;
    ended = true;
    onEnd(usage, err);
  };
  const observe = (event: StreamEvent): void => {
    try {
      if (event?.type === "message_start") {
        usage.model = event.message?.model ?? usage.model;
        usage.inputTokens = event.message?.usage?.input_tokens ?? usage.inputTokens;
        usage.outputTokens = event.message?.usage?.output_tokens ?? usage.outputTokens;
      } else if (event?.type === "message_delta") {
        // Cumulative output token count; the final message_delta carries the total.
        usage.outputTokens = event.usage?.output_tokens ?? usage.outputTokens;
      }
    } catch {
      // Observation must never affect the host's iteration.
    }
  };

  return new Proxy(stream, {
    get(target, prop) {
      if (prop === Symbol.asyncIterator) {
        return () => {
          const inner = target[Symbol.asyncIterator]();
          const iterator: AsyncIterator<StreamEvent> & AsyncIterable<StreamEvent> = {
            next: async (...args) => {
              try {
                const r = await inner.next(...args);
                if (r.done) finish();
                else observe(r.value);
                return r;
              } catch (err) {
                finish(err);
                throw err;
              }
            },
            return: async (value?: unknown) => {
              // Consumer stopped early (break/abort): record what we saw.
              finish();
              return inner.return
                ? inner.return(value)
                : { done: true as const, value: value as StreamEvent };
            },
            throw: async (err?: unknown) => {
              finish(err);
              if (inner.throw) return inner.throw(err);
              throw err;
            },
            [Symbol.asyncIterator]() {
              return this;
            },
          };
          return iterator;
        };
      }
      const value = Reflect.get(target, prop, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  }) as S;
}
