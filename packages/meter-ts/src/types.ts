/** Canonical record shape — must stay byte-compatible with spec/record.schema.json. */
export type MeterStatus = "ok" | "error" | "retried";

export interface MeterRecord {
  /** ISO 8601 UTC timestamp of the call. */
  ts: string;
  /** Lowercase project slug. */
  project: string;
  /** Lowercase component slug. */
  component: string;
  /** Model ID as reported by the SDK response. */
  model: string;
  tokens_in: number | null;
  tokens_out: number | null;
  /** USD rounded to 5 decimals; null if the model is unknown in prices.json. */
  cost_usd: number | null;
  latency_ms: number;
  status: MeterStatus;
  error_type: string | null;
  request_id: string | null;
  trace_id: string | null;
}

export interface Transport {
  /** Persist one record. May reject — callers treat recording as fire-and-forget. */
  send(record: MeterRecord): Promise<void>;
}

export interface MeterConfig {
  project: string;
  component: string;
  /** Default: SupabaseTransport(env) with JsonlFallback("./meter.log.jsonl"). */
  transport?: Transport;
  traceId?: string | (() => string | undefined);
}
