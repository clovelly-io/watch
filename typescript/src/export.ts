/**
 * Getting batches out without touching the service: OpenTelemetry's batch processors hand
 * records to the exporters here, which split and seal them and put the encoded request on
 * a bounded queue. One loop sends the queue to ingest. When the queue is full the oldest
 * batch is dropped, so memory stays under the bound whatever ingest does; nothing here
 * throws into the application, and nothing keeps the process alive.
 */
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { type Context, SpanStatusCode, context } from "@opentelemetry/api";
import { type ExportResult, ExportResultCode, suppressTracing } from "@opentelemetry/core";
import type { LogRecordExporter, LogRecordProcessor, ReadWriteLogRecord, ReadableLogRecord } from "@opentelemetry/sdk-logs";
import { AggregationTemporality, InstrumentType, type PushMetricExporter, type ResourceMetrics } from "@opentelemetry/sdk-metrics";
import type { ReadableSpan, Span, SpanExporter, SpanProcessor } from "@opentelemetry/sdk-trace";
import { ProtobufLogsSerializer, ProtobufMetricsSerializer, ProtobufTraceSerializer } from "@opentelemetry/otlp-transformer";
import { Sealer } from "./hpke.js";
import type { LocalsCapture } from "./hooks.js";
import { Resources, splitLog, splitSpan, stripMetrics } from "./split.js";
import { warnOnce } from "./warn.js";

/** The queue's bound, in encoded bytes. The batch processors in front of it hold at most
 * 2,048 spans and 2,048 log records of their own. */
export const MAX_QUEUE_BYTES = 8 << 20;
/** The largest request sent: an export over this is split in two, each half sealed on its
 * own, until every part fits. Ingest refuses bodies over 4 MiB. */
export const MAX_REQUEST_BYTES = 2 << 20;
const REQUEST_TIMEOUT_MS = 10_000;
const MAX_BACKOFF_MS = 30_000;

export type Path = "/v1/traces" | "/v1/logs" | "/v1/metrics";

export interface SenderStats {
  sentBatches: number;
  droppedBatches: number;
  failedAttempts: number;
  refusedBatches: number;
}

/** A bounded queue of encoded requests and the one loop that sends them. */
export class Sender {
  readonly stats: SenderStats = { sentBatches: 0, droppedBatches: 0, failedAttempts: 0, refusedBatches: 0 };
  readonly #url: URL;
  readonly #token: string;
  readonly #maxBytes: number;
  readonly #queue: { path: Path; body: Uint8Array }[] = [];
  #bytes = 0;
  #running = false;
  #closed = false;
  #wake: (() => void) | undefined;

  constructor(endpoint: string, token: string, maxBytes = MAX_QUEUE_BYTES) {
    this.#url = new URL(endpoint);
    this.#token = token;
    this.#maxBytes = maxBytes;
  }

  /** Adds a request, dropping the oldest until it fits. Never waits on the network. */
  enqueue(path: Path, body: Uint8Array): void {
    if (this.#closed) return;
    if (body.length > this.#maxBytes) {
      this.stats.droppedBatches++;
      return;
    }
    while (this.#queue.length && this.#bytes + body.length > this.#maxBytes) {
      this.#bytes -= this.#queue.shift()!.body.length;
      this.stats.droppedBatches++;
    }
    this.#queue.push({ path, body });
    this.#bytes += body.length;
    if (!this.#running) void this.#run();
  }

  queued(): { batches: number; bytes: number } {
    return { batches: this.#queue.length, bytes: this.#bytes };
  }

  /** Resolves when the queue is empty, or after `timeoutMs`; says which. */
  async flush(timeoutMs: number): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    while (this.#running || this.#queue.length) {
      if (Date.now() >= deadline) return false;
      await new Promise((r) => setTimeout(r, 20).unref());
    }
    return true;
  }

  close(): void {
    this.#closed = true;
    this.#wake?.();
  }

  #post(path: Path, body: Uint8Array): Promise<{ status: number; retryAfterMs?: number }> {
    const send = this.#url.protocol === "https:" ? httpsRequest : httpRequest;
    return new Promise((resolve) => {
      // Our own requests are not the service's: no spans or metrics for them.
      context.with(suppressTracing(context.active()), () => {
        const req = send(
          new URL(this.#url.pathname.replace(/\/$/, "") + path, this.#url),
          {
            method: "POST",
            timeout: REQUEST_TIMEOUT_MS,
            headers: {
              "content-type": "application/x-protobuf",
              "content-length": body.length,
              authorization: `Bearer ${this.#token}`,
            },
          },
          (res) => {
            res.resume();
            res.on("end", () => {
              const retry = Number(res.headers["retry-after"]);
              resolve({ status: res.statusCode ?? 0, retryAfterMs: Number.isFinite(retry) ? retry * 1000 : undefined });
            });
            res.on("error", () => resolve({ status: 0 }));
          },
        );
        req.on("timeout", () => req.destroy());
        req.on("error", () => resolve({ status: 0 }));
        req.end(body);
      });
    });
  }

  async #run(): Promise<void> {
    this.#running = true;
    let backoff = 1000;
    try {
      while (this.#queue.length && !this.#closed) {
        const head = this.#queue[0]!;
        const { status, retryAfterMs } = await this.#post(head.path, head.body);
        const retry = status === 0 || status === 429 || status >= 500;
        if (!retry) {
          // The head may have been dropped while it was in flight; remove only it.
          if (this.#queue[0] === head) {
            this.#queue.shift();
            this.#bytes -= head.body.length;
          }
          backoff = 1000;
          if (status === 200) this.stats.sentBatches++;
          else {
            this.stats.refusedBatches++;
            warnOnce(`status-${status}`, `ingest refused a batch with HTTP ${status}; check CLOVELLY_WATCH_TOKEN`);
          }
          continue;
        }
        this.stats.failedAttempts++;
        const wait = Math.min(retryAfterMs ?? backoff, 60_000) * (0.5 + Math.random() / 2);
        backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
        await new Promise<void>((resolve) => {
          const timer = setTimeout(resolve, wait);
          timer.unref();
          this.#wake = () => {
            clearTimeout(timer);
            resolve();
          };
        });
        this.#wake = undefined;
      }
    } catch (e) {
      warnOnce("sender", `the sender stopped: ${(e as Error).name}`);
    } finally {
      this.#running = false;
    }
  }
}

/**
 * Seals `records` into one request, or, when that is over `limit`, into halves sealed on
 * their own, recursively; one record goes alone whatever its size (its payload is capped at
 * 64 KiB). Error bursts are when requests grow, and a request ingest refuses loses every
 * record in it.
 */
export function enqueueSealed<T>(
  sender: Pick<Sender, "enqueue">,
  path: Path,
  records: T[],
  seal: (records: T[]) => Uint8Array | undefined,
  limit = MAX_REQUEST_BYTES,
): void {
  const body = seal(records);
  if (!body) return;
  if (body.length > limit && records.length > 1) {
    const half = Math.floor(records.length / 2);
    enqueueSealed(sender, path, records.slice(0, half), seal, limit);
    enqueueSealed(sender, path, records.slice(half), seal, limit);
    return;
  }
  sender.enqueue(path, body);
}

/** Runs `fn` and reports success; any exception becomes one warning and a failure. */
function guarded(what: string, done: (r: ExportResult) => void, fn: () => void): void {
  try {
    fn();
    done({ code: ExportResultCode.SUCCESS });
  } catch (e) {
    warnOnce(`export-${what}`, `could not export ${what}: ${(e as Error).name}`);
    done({ code: ExportResultCode.FAILED });
  }
}

export class SealingSpanExporter implements SpanExporter {
  constructor(
    private readonly sender: Sender,
    private readonly recipient: Buffer,
    private readonly locals?: LocalsCapture,
  ) {}

  export(spans: ReadableSpan[], done: (r: ExportResult) => void): void {
    guarded("spans", done, () => {
      const seal = (part: ReadableSpan[]) => {
        const run = () => {
          const sealer = new Sealer(this.recipient);
          const resources = new Resources(sealer.enc);
          return ProtobufTraceSerializer.serializeRequest(part.map((s) => splitSpan(s, sealer, resources, this.locals?.lookup)));
        };
        return this.locals ? this.locals.quiet(run) : run();
      };
      enqueueSealed(this.sender, "/v1/traces", spans, seal);
    });
  }

  async shutdown(): Promise<void> {}
}

export class SealingLogExporter implements LogRecordExporter {
  constructor(
    private readonly sender: Sender,
    private readonly recipient: Buffer,
    private readonly locals?: LocalsCapture,
  ) {}

  export(logs: ReadableLogRecord[], done: (r: ExportResult) => void): void {
    guarded("logs", done, () => {
      const seal = (part: ReadableLogRecord[]) => {
        const run = () => {
          const sealer = new Sealer(this.recipient);
          const resources = new Resources(sealer.enc);
          return ProtobufLogsSerializer.serializeRequest(part.map((l) => splitLog(l, sealer, resources, this.locals?.lookup)));
        };
        return this.locals ? this.locals.quiet(run) : run();
      };
      enqueueSealed(this.sender, "/v1/logs", logs, seal);
    });
  }

  async forceFlush(): Promise<void> {}
  async shutdown(): Promise<void> {}
}

export class StrippingMetricExporter implements PushMetricExporter {
  constructor(private readonly sender: Sender) {}

  export(metrics: ResourceMetrics, done: (r: ExportResult) => void): void {
    guarded("metrics", done, () => {
      const { metrics: stripped, points } = stripMetrics(metrics);
      if (!points) return;
      const body = ProtobufMetricsSerializer.serializeRequest(stripped);
      if (body) this.sender.enqueue("/v1/metrics", body);
    });
  }

  /** Delta for what monitors count, so ingest adds per-minute counters without differencing. */
  selectAggregationTemporality(kind: InstrumentType): AggregationTemporality {
    return kind === InstrumentType.UP_DOWN_COUNTER ||
      kind === InstrumentType.OBSERVABLE_UP_DOWN_COUNTER ||
      kind === InstrumentType.OBSERVABLE_GAUGE ||
      kind === InstrumentType.GAUGE
      ? AggregationTemporality.CUMULATIVE
      : AggregationTemporality.DELTA;
  }

  async forceFlush(): Promise<void> {}
  async shutdown(): Promise<void> {}
}

/** The same answer for every span of a trace: the trace id's low 64 bits against the rate. */
export function sampled(traceId: string, rate: number): boolean {
  return BigInt(`0x${traceId.slice(-16) || "0"}`) < BigInt(Math.floor(rate * 2 ** 64));
}

/** Decides when a span ends: every failed span, and a share of the rest by trace. */
export class SamplingProcessor implements SpanProcessor {
  constructor(
    private readonly inner: SpanProcessor,
    private readonly rate: number,
  ) {}

  onStart(_span: Span, _parent: Context): void {}

  onEnd(span: ReadableSpan): void {
    if (span.status.code === SpanStatusCode.ERROR || sampled(span.spanContext().traceId, this.rate)) this.inner.onEnd(span);
  }

  forceFlush(): Promise<void> {
    return this.inner.forceFlush();
  }

  shutdown(): Promise<void> {
    return this.inner.shutdown();
  }
}

/** Only records at WARN (13) and above go on: pino's instrumentation sends every level. */
export class WarningProcessor implements LogRecordProcessor {
  constructor(private readonly inner: LogRecordProcessor) {}

  onEmit(record: ReadWriteLogRecord, ctx?: Context): void {
    if ((record.severityNumber ?? 0) >= 13) this.inner.onEmit(record, ctx);
  }

  forceFlush(): Promise<void> {
    return this.inner.forceFlush();
  }

  shutdown(): Promise<void> {
    return this.inner.shutdown();
  }
}
