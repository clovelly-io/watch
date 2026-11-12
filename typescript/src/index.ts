/**
 * Clovelly Watch for Node: `import "@clovelly/watch/register"` as the service's first import
 * (or `node --import @clovelly/watch/register` for an ES-module service), and the service
 * sends the telemetry Watch's monitors need.
 *
 * It is a thin distribution of OpenTelemetry: its instrumentations for the libraries the
 * service already uses, plus the split (docs/watch-wire.md), which keeps a closed list of
 * metadata readable and seals everything else to the repository's payload key. Nothing it
 * does throws into the application.
 *
 * Configuration is the environment: CLOVELLY_WATCH_TOKEN, CLOVELLY_WATCH_KEY, and
 * optionally CLOVELLY_WATCH_RELEASE, CLOVELLY_WATCH_ENVIRONMENT, CLOVELLY_WATCH_ENDPOINT,
 * CLOVELLY_WATCH_SAMPLE_RATE, CLOVELLY_WATCH_LOCALS (0 turns local-variable capture off),
 * OTEL_SERVICE_NAME and CLOVELLY_WATCH_STATS_FILE (diagnostics). Options passed to `init` override them.
 */
import { writeFileSync } from "node:fs";
import { context, logs, metrics, propagation, trace } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { W3CTraceContextPropagator } from "@opentelemetry/core";
import { type Instrumentation, registerInstrumentations } from "@opentelemetry/instrumentation";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BatchLogRecordProcessor, LoggerProvider } from "@opentelemetry/sdk-logs";
import { MeterProvider, PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";
import { AlwaysOnSampler, BatchSpanProcessor, TracerProvider } from "@opentelemetry/sdk-trace";
import {
  SamplingProcessor,
  SealingLogExporter,
  SealingSpanExporter,
  Sender,
  type SenderStats,
  StrippingMetricExporter,
  WarningProcessor,
} from "./export.js";
import { LocalsCapture, captureBody, captureHeaders, hookConsole, hookUncaught } from "./hooks.js";
import { warnOnce } from "./warn.js";

export const DEFAULT_ENDPOINT = "https://ingest.clovelly.io";
export const DEFAULT_SAMPLE_RATE = 0.1;
/** Where the release comes from, in order: ours, then the platforms that set one. */
export const RELEASE_VARIABLES = [
  "CLOVELLY_WATCH_RELEASE",
  "VERCEL_GIT_COMMIT_SHA",
  "RENDER_GIT_COMMIT",
  "RAILWAY_GIT_COMMIT_SHA",
  "HEROKU_SLUG_COMMIT",
  "SOURCE_VERSION",
  "COMMIT_REF",
  "GITHUB_SHA",
] as const;

export interface Options {
  token?: string;
  key?: string;
  release?: string;
  environment?: string;
  endpoint?: string;
  service?: string;
  sampleRate?: number;
  locals?: boolean;
}

interface Running {
  sender: Sender;
  tracer: TracerProvider;
  meter: MeterProvider;
  logger: LoggerProvider;
  locals?: LocalsCapture;
}

let state: { enabled: false } | ({ enabled: true } & Running) | undefined;

/** The commit the service runs, or undefined if none of the variables holds a full SHA. */
export function findRelease(explicit?: string, env: NodeJS.ProcessEnv = process.env): string | undefined {
  const value = [explicit, ...RELEASE_VARIABLES.map((v) => env[v])].find((v) => v);
  const sha = value?.trim().toLowerCase();
  return sha && /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(sha) ? sha : undefined;
}

/** CLOVELLY_WATCH_KEY (`cwk1_` and base64url) to the recipient's SEC1 point. */
export function parseKey(text: string): Buffer {
  if (!text.startsWith("cwk1_")) throw new Error("CLOVELLY_WATCH_KEY does not start with cwk1_");
  const raw = Buffer.from(text.slice(5), "base64url");
  if (raw.length !== 65 || raw[0] !== 4) throw new Error("CLOVELLY_WATCH_KEY is not an uncompressed P-256 point");
  return raw;
}

/** Instrumentations for the libraries the service may use; each patches its library only
 * if the service loads it. */
function instrumentations(): Instrumentation[] {
  // Required here, not imported at the top: OTEL_SEMCONV_STABILITY_OPT_IN must be set first.
  const { HttpInstrumentation } = require("@opentelemetry/instrumentation-http") as typeof import("@opentelemetry/instrumentation-http");
  const { ExpressInstrumentation } = require("@opentelemetry/instrumentation-express") as typeof import("@opentelemetry/instrumentation-express");
  const { UndiciInstrumentation } = require("@opentelemetry/instrumentation-undici") as typeof import("@opentelemetry/instrumentation-undici");
  const { PgInstrumentation } = require("@opentelemetry/instrumentation-pg") as typeof import("@opentelemetry/instrumentation-pg");
  const { PinoInstrumentation } = require("@opentelemetry/instrumentation-pino") as typeof import("@opentelemetry/instrumentation-pino");
  return [
    new HttpInstrumentation({ requestHook: captureHeaders }),
    new ExpressInstrumentation({ requestHook: captureBody }),
    new UndiciInstrumentation(),
    new PgInstrumentation(),
    new PinoInstrumentation(),
  ];
}

/** Starts sending. Returns whether it did: without a token or a payload key it warns once
 * and sends nothing. Calling it again does nothing. */
export function init(options: Options = {}): boolean {
  if (state) return state.enabled;
  state = { enabled: false };
  try {
    const env = process.env;
    const token = options.token ?? env.CLOVELLY_WATCH_TOKEN ?? "";
    if (!token.startsWith("cwt_")) {
      warnOnce("token", "CLOVELLY_WATCH_TOKEN is not set (or not a cwt_ token); sending nothing");
      return false;
    }
    let recipient: Buffer;
    try {
      recipient = parseKey(options.key ?? env.CLOVELLY_WATCH_KEY ?? "");
    } catch (e) {
      warnOnce("key", `${(e as Error).message}; sending nothing`);
      return false;
    }
    const release = findRelease(options.release, env);
    if (!release) warnOnce("release", "no release: set CLOVELLY_WATCH_RELEASE to the deployed commit; records arm no monitor");
    const rate = Math.min(Math.max(options.sampleRate ?? Number(env.CLOVELLY_WATCH_SAMPLE_RATE ?? DEFAULT_SAMPLE_RATE), 0), 1);
    const environment = options.environment ?? env.CLOVELLY_WATCH_ENVIRONMENT;
    env.OTEL_SEMCONV_STABILITY_OPT_IN ??= "http";

    const resource = resourceFromAttributes({
      "service.name": options.service ?? env.OTEL_SERVICE_NAME ?? "unknown_service",
      ...(release ? { "vcs.ref.head.revision": release } : {}),
      ...(environment ? { "deployment.environment.name": environment } : {}),
    });
    const sender = new Sender(options.endpoint ?? env.CLOVELLY_WATCH_ENDPOINT ?? DEFAULT_ENDPOINT, token);
    let locals: LocalsCapture | undefined;
    if (options.locals ?? env.CLOVELLY_WATCH_LOCALS !== "0") {
      try {
        locals = new LocalsCapture();
        locals.start();
      } catch {
        warnOnce("locals", "local variables are not captured: the inspector is unavailable");
        locals = undefined;
      }
    }
    const tracer = new TracerProvider({
      resource,
      sampler: new AlwaysOnSampler(),
      spanProcessors: [new SamplingProcessor(new BatchSpanProcessor({ exporter: new SealingSpanExporter(sender, recipient, locals), scheduledDelayMillis: 2000, maxQueueSize: 2048 }), rate)],
    });
    const meter = new MeterProvider({
      resource,
      readers: [new PeriodicExportingMetricReader({ exporter: new StrippingMetricExporter(sender), exportIntervalMillis: 10_000 })],
    });
    const logger = new LoggerProvider({
      resource,
      processors: [new WarningProcessor(new BatchLogRecordProcessor({ exporter: new SealingLogExporter(sender, recipient, locals), scheduledDelayMillis: 2000, maxQueueSize: 2048 }))],
    });
    context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable());
    propagation.setGlobalPropagator(new W3CTraceContextPropagator());
    trace.setGlobalTracerProvider(tracer);
    metrics.setGlobalMeterProvider(meter);
    logs.setGlobalLoggerProvider(logger);
    registerInstrumentations({ instrumentations: instrumentations(), tracerProvider: tracer, meterProvider: meter, loggerProvider: logger });
    hookConsole(logger, locals?.lookup);
    hookUncaught(logger, () => void logger.forceFlush());
    state = { enabled: true, sender, tracer, meter, logger, locals };
    flushOnExit();
    // CLOVELLY_WATCH_STATS_FILE: the sender's counters, rewritten every five seconds, for
    // diagnosing a service without touching its code.
    const statsFile = env.CLOVELLY_WATCH_STATS_FILE;
    if (statsFile) {
      setInterval(() => {
        try {
          writeFileSync(statsFile, JSON.stringify(stats()));
        } catch {
          // diagnostics only
        }
      }, 5000).unref();
    }
    return true;
  } catch (e) {
    warnOnce("init", `could not start (${(e as Error).name}: ${(e as Error).message}); sending nothing`);
    return false;
  }
}

/**
 * Sends what is pending when the process ends by itself (`beforeExit`) or on SIGTERM, which
 * is how a deploy stops it. A SIGTERM the service does not handle itself is raised again
 * once we are done, so the process ends as it would have. A process that dies of an
 * uncaught exception exits before anything can be sent, and that record is lost.
 */
function flushOnExit(): void {
  process.once("beforeExit", () => void shutdown());
  process.once("SIGTERM", () => {
    void shutdown().finally(() => {
      if (process.listenerCount("SIGTERM") === 0) process.kill(process.pid, "SIGTERM");
    });
  });
}

/** Exports what is pending and sends what it can within `timeoutMs`. */
export async function shutdown(timeoutMs = 2000): Promise<void> {
  if (!state?.enabled) return;
  const { sender, tracer, meter, logger, locals } = state;
  try {
    await Promise.all([tracer.forceFlush(), logger.forceFlush(), meter.forceFlush()]);
    await sender.flush(timeoutMs);
    sender.close();
    locals?.stop();
  } catch {
    // shutting down is best effort
  }
}

/** The sender's counters and its queue, for diagnostics and tests. */
export function stats(): (SenderStats & { queuedBatches: number; queuedBytes: number }) | undefined {
  if (!state?.enabled) return undefined;
  const { batches, bytes } = state.sender.queued();
  return { ...state.sender.stats, queuedBatches: batches, queuedBytes: bytes };
}
