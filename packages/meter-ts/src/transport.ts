import { appendFile } from "node:fs/promises";
import { createClient } from "@supabase/supabase-js";
import type { MeterRecord, Transport } from "./types.js";

/** Minimal structural view of the supabase client, so tests can inject a stub. */
export interface SupabaseLike {
  from(table: string): {
    insert(row: MeterRecord): PromiseLike<{ error: { message: string } | null }>;
  };
}

export interface SupabaseTransportOptions {
  /** Defaults to METER_SUPABASE_URL || SUPABASE_URL. */
  url?: string;
  /** Defaults to METER_SUPABASE_ANON_KEY || SUPABASE_ANON_KEY. */
  key?: string;
  /** Defaults to "llm_calls". */
  table?: string;
  /** Injectable client (tests). */
  client?: SupabaseLike;
}

/** Writes records to the shared Supabase llm_calls table. Throws on failure. */
export class SupabaseTransport implements Transport {
  private client: SupabaseLike | null;
  private readonly table: string;

  constructor(private readonly opts: SupabaseTransportOptions = {}) {
    this.client = opts.client ?? null;
    this.table = opts.table ?? "llm_calls";
  }

  async send(record: MeterRecord): Promise<void> {
    const { error } = await this.getClient().from(this.table).insert(record);
    if (error) throw new Error(`meter: supabase insert failed: ${error.message}`);
  }

  private getClient(): SupabaseLike {
    if (this.client) return this.client;
    const url = this.opts.url ?? process.env.METER_SUPABASE_URL ?? process.env.SUPABASE_URL;
    const key =
      this.opts.key ?? process.env.METER_SUPABASE_ANON_KEY ?? process.env.SUPABASE_ANON_KEY;
    if (!url || !key) {
      throw new Error("meter: supabase transport not configured (SUPABASE_URL / SUPABASE_ANON_KEY)");
    }
    this.client = createClient(url, key, { auth: { persistSession: false } });
    return this.client;
  }
}

/** Appends records as JSON lines to a local file. */
export class JsonlFallback implements Transport {
  constructor(private readonly path: string = "./meter.log.jsonl") {}

  async send(record: MeterRecord): Promise<void> {
    await appendFile(this.path, JSON.stringify(record) + "\n", "utf8");
  }
}

/** Tries the primary transport; on any failure writes to the fallback instead. */
export class FallbackTransport implements Transport {
  constructor(
    private readonly primary: Transport,
    private readonly fallback: Transport,
  ) {}

  async send(record: MeterRecord): Promise<void> {
    try {
      await this.primary.send(record);
    } catch {
      await this.fallback.send(record);
    }
  }
}

/** Default per contract: SupabaseTransport(env) with JsonlFallback("./meter.log.jsonl"). */
export function defaultTransport(): Transport {
  return new FallbackTransport(new SupabaseTransport(), new JsonlFallback("./meter.log.jsonl"));
}
