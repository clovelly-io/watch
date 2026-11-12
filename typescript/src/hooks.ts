/**
 * What the SDK hooks besides OpenTelemetry's instrumentations: `console.warn` and
 * `console.error` (OpenTelemetry has no console instrumentation), uncaught exceptions, and
 * the local variables of the frames an exception is thrown through.
 */
import type { ClientRequest } from "node:http";
import { IncomingMessage } from "node:http";
import { Session } from "node:inspector";
import { format } from "node:util";
import { type Attributes, SeverityNumber, type Span, context } from "@opentelemetry/api";
import { getRPCMetadata } from "@opentelemetry/core";
import type { LoggerProvider } from "@opentelemetry/sdk-logs";
import type { LocalsLookup } from "./split.js";

const MAX_FRAMES = 10;
const MAX_VARS = 50;
const MAX_VALUE = 512;
const KEPT = 200;
/** Above this many pauses a second the capture stops for a while: a hot throw loop must not
 * turn into a hot debugger loop. */
const MAX_PER_SECOND = 20;
const COOL_DOWN_MS = 10_000;

type Frame = { function: string; file: string; line: number; vars: Record<string, string> };

const MAX_BODY = 16384;

/** HTTP instrumentation's request hook: every header of a request the service receives,
 * as `http.request.header.*` attributes, which are off the allowlist and so sealed. */
export function captureHeaders(span: Span, request: ClientRequest | IncomingMessage): void {
  if (!(request instanceof IncomingMessage)) return;
  for (const [name, value] of Object.entries(request.headers)) {
    if (value !== undefined) span.setAttribute(`http.request.header.${name}`, Array.isArray(value) ? value : [value]);
  }
}

/** Express instrumentation's request hook: at the route handler, after the body parsers,
 * the first 16 KiB of the parsed body go on the server span as `http.request.body`
 * (sealed). The HTTP instrumentation keeps the server span in the context's RPC metadata. */
export function captureBody(_span: Span, info: { request: { body?: unknown }; layerType: string }): void {
  try {
    if (info.layerType !== "request_handler" || info.request.body === undefined) return;
    const body = info.request.body;
    const text = typeof body === "string" ? body : Buffer.isBuffer(body) ? body.toString("utf8") : JSON.stringify(body);
    getRPCMetadata(context.active())?.span?.setAttribute("http.request.body", text.slice(0, MAX_BODY));
  } catch {
    // the request goes on whatever happens here
  }
}

/** The attributes of an exception, as OpenTelemetry names them. */
export function exceptionAttributes(err: Error): Attributes {
  return {
    "exception.type": err.name || err.constructor?.name || "Error",
    "exception.message": err.message,
    ...(err.stack ? { "exception.stacktrace": err.stack } : {}),
  };
}

/**
 * Captures local variables when an exception is thrown, through the inspector on this
 * thread: V8 pauses on the throw, the frames' local scopes are read, and the result is kept
 * under the exception's stack, which is how a span event or log record finds it later. Only
 * the service's own frames are read (not node: or node_modules), and at most
 * MAX_PER_SECOND pauses a second. Library code is blackboxed, so V8 does not pause at all
 * on an exception a library throws and catches itself: those cost nothing and do not use
 * up the limit.
 */
/** Scripts whose exceptions are not the service's: Node's own and its dependencies'. */
const BLACKBOX = ["^node:", "/node_modules/"];
const SCRIPTS_CACHE_BYTES = 10_000_000;
const own = (url: string): boolean => !!url && !url.startsWith("node:") && !url.includes("/node_modules/");

export class LocalsCapture {
  readonly #session = new Session();
  readonly #kept = new Map<string, string>();
  /** The service's own script urls by id: a CommonJS module's call frames carry no url of
   * their own. Library and internal scripts are not kept, so this grows only with the
   * service's own code. */
  readonly #scripts = new Map<string, string>();
  #second = 0;
  #inSecond = 0;
  #cooling = false;
  #quiet = false;

  /** Starts capturing once the main module has loaded: loading throws (and catches) many
   * exceptions of its own, which would only spend the rate limit. `now` is for tests. */
  start(now = false): void {
    this.#session.connect();
    this.#session.on("Debugger.scriptParsed", (m) => {
      if (own(m.params.url)) this.#scripts.set(m.params.scriptId, m.params.url);
    });
    this.#session.on("Debugger.paused", (m) => this.#paused(m.params));
    // V8 keeps collected scripts for a debugger without limit unless told one.
    this.#session.post("Debugger.enable", { maxScriptsCacheSize: SCRIPTS_CACHE_BYTES });
    this.#session.post("Debugger.setBlackboxPatterns", { patterns: BLACKBOX });
    const pause = () => this.#pauseOn("all");
    if (now) pause();
    else setImmediate(pause).unref();
  }

  stop(): void {
    try {
      this.#session.disconnect();
    } catch {
      // already gone
    }
  }

  readonly lookup: LocalsLookup = (stack) => this.#kept.get(stack);

  /** Runs the SDK's own synchronous work without pausing: node:crypto throws and catches
   * on every HMAC and cipher it makes, and those are not the service's exceptions. No
   * service code runs meanwhile, since this is one synchronous call. */
  quiet<T>(fn: () => T): T {
    if (this.#cooling || this.#quiet) return fn();
    this.#quiet = true;
    this.#pauseOn("none");
    try {
      return fn();
    } finally {
      this.#quiet = false;
      if (!this.#cooling) this.#pauseOn("all");
    }
  }

  #pauseOn(state: "all" | "none"): void {
    this.#session.post("Debugger.setPauseOnExceptions", { state });
  }

  #paused(params: import("node:inspector").Debugger.PausedEventDataType): void {
    try {
      if (params.reason !== "exception" && params.reason !== "promiseRejection") return;
      const now = Math.floor(Date.now() / 1000);
      if (now !== this.#second) [this.#second, this.#inSecond] = [now, 0];
      if (++this.#inSecond > MAX_PER_SECOND) return this.#coolDown();
      const stack = (params.data as { description?: string } | undefined)?.description;
      if (!stack) return;
      const frames: Frame[] = [];
      for (const f of params.callFrames.slice(0, MAX_FRAMES)) {
        const url = f.url || this.#scripts.get(f.location.scriptId) || "";
        if (!own(url)) continue;
        // The function's own scope and the blocks inside it, innermost first, so a
        // variable declared in an `if` is found and shadows an outer one.
        const vars: Record<string, string> = {};
        for (const scope of f.scopeChain.filter((sc) => sc.type === "block" || sc.type === "local" || sc.type === "catch")) {
          if (!scope.object.objectId) continue;
          // In-thread sessions answer synchronously while paused.
          this.#session.post("Runtime.getProperties", { objectId: scope.object.objectId, ownProperties: true }, (err, res) => {
            if (err || !res) return;
            for (const p of res.result) {
              const v = p.value;
              if (!v || p.name in vars || Object.keys(vars).length >= MAX_VARS) continue;
              const text =
                v.type === "string" ? JSON.stringify(v.value)
                : v.type === "function" ? `function ${v.className === "Function" ? "" : (v.className ?? "")}`.trim()
                : v.value !== undefined ? String(v.value)
                : (v.description ?? v.type);
              vars[p.name] = text.slice(0, MAX_VALUE);
            }
          });
        }
        frames.push({ function: f.functionName || "<anonymous>", file: url, line: f.location.lineNumber + 1, vars });
      }
      if (!frames.length) return;
      // The latest capture wins: two throws with one stack string (a constant message) must
      // not leave one request's values on every later one's record.
      this.#kept.delete(stack);
      this.#kept.set(stack, JSON.stringify(frames));
      if (this.#kept.size > KEPT) this.#kept.delete(this.#kept.keys().next().value!);
    } catch {
      // never let the capture break the throw it is watching
    } finally {
      this.#session.post("Debugger.resume");
    }
  }

  #coolDown(): void {
    if (this.#cooling) return;
    this.#cooling = true;
    this.#pauseOn("none");
    setTimeout(() => {
      this.#cooling = false;
      this.#pauseOn("all");
    }, COOL_DOWN_MS).unref();
  }
}

/** Sends `console.warn` and `console.error` calls as WARN and ERROR log records, then
 * prints them as before. The rendered message is the body (sealed); an Error argument adds
 * its exception attributes, and a stack printed as a string (Express prints `err.stack`)
 * finds its local variables. */
export function hookConsole(provider: LoggerProvider, locals?: LocalsLookup): void {
  const logger = provider.getLogger("console");
  let inside = false;
  for (const [method, severityNumber, severityText] of [
    ["warn", SeverityNumber.WARN, "WARN"],
    ["error", SeverityNumber.ERROR, "ERROR"],
  ] as const) {
    const original = console[method].bind(console);
    console[method] = (...args: unknown[]) => {
      if (!inside) {
        inside = true;
        try {
          const err = args.find((a): a is Error => a instanceof Error);
          const attributes: Attributes = err ? exceptionAttributes(err) : {};
          const stack = err?.stack ?? args.find((a): a is string => typeof a === "string" && locals?.(a) !== undefined);
          if (!err && stack) {
            attributes["exception.stacktrace"] = stack;
            const type = /^(\w+)(?::|$)/.exec(stack)?.[1];
            if (type) attributes["exception.type"] = type;
          }
          logger.emit({ severityNumber, severityText, body: format(...args), attributes });
        } catch {
          // printing still happens below
        } finally {
          inside = false;
        }
      }
      original(...args);
    };
  }
}

/** Uncaught exceptions and unhandled rejections are sent as FATAL records. The monitor
 * changes nothing about how the process handles them; delivery is best effort, since the
 * process is usually about to exit. */
export function hookUncaught(provider: LoggerProvider, flush: () => void): void {
  const logger = provider.getLogger("uncaught");
  process.on("uncaughtExceptionMonitor", (err, origin) => {
    try {
      const e = err instanceof Error ? err : new Error(String(err));
      logger.emit({
        severityNumber: SeverityNumber.FATAL,
        severityText: "FATAL",
        body: `${origin}: ${e.stack ?? e.message}`,
        attributes: exceptionAttributes(e),
      });
      flush();
    } catch {
      // the process is going down; nothing to add
    }
  });
}
