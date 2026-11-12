/**
 * The split (docs/watch-wire.md): each record keeps a closed list of metadata in the clear,
 * and everything else goes into one HPKE-sealed blob per record. Works on the SDK's own
 * records, returning new ones for OpenTelemetry's protobuf serializers.
 */
import { type AttributeValue, type Attributes, SpanKind } from "@opentelemetry/api";
import type { ReadableLogRecord } from "@opentelemetry/sdk-logs";
import type { MetricData, ResourceMetrics } from "@opentelemetry/sdk-metrics";
import type { ReadableSpan, TimedEvent } from "@opentelemetry/sdk-trace";
import type { Resource } from "@opentelemetry/resources";
import type { Sealer } from "./hpke.js";

export const SEALED = "clovelly.sealed";
export const SEALED_ENC = "clovelly.sealed.enc";
export const LOCALS = "clovelly.locals";
export const RELEASE = "vcs.ref.head.revision";
export const RESOURCE_ALLOWLIST: ReadonlySet<string> = new Set(["service.name", "deployment.environment.name", RELEASE]);
export const RECORD_ALLOWLIST: ReadonlySet<string> = new Set([
  "http.route",
  "http.request.method",
  "http.method",
  "http.response.status_code",
  "http.status_code",
  "server.address",
  "net.peer.name",
  "db.system",
  "db.system.name",
  "db.operation",
  "db.operation.name",
  "exception.type",
  "code.filepath",
  "code.file.path",
  "code.function",
  "code.function.name",
  "clovelly.log.template",
]);
/** The peer's host stays in the clear only where it names whom the service called: on
 * CLIENT spans and client metrics. On a server span the HTTP instrumentation fills it from
 * the Host or X-Forwarded-Host header, which the caller writes. */
const PEER_KEYS: ReadonlySet<string> = new Set(["server.address", "net.peer.name"]);
export const NO_PEER_ALLOWLIST: ReadonlySet<string> = new Set([...RECORD_ALLOWLIST].filter((k) => !PEER_KEYS.has(k)));
const CLIENT_METRIC_PREFIXES = ["http.client.", "db.client."];
/** The peer's host is a dimension of client metrics only. */
export const metricAllowlist = (name: string): ReadonlySet<string> =>
  CLIENT_METRIC_PREFIXES.some((p) => name.startsWith(p)) ? RECORD_ALLOWLIST : NO_PEER_ALLOWLIST;
const METHODS: ReadonlySet<string> = new Set(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE", "_OTHER"]);
const OPERATION = /^[A-Z]{1,32}$/;
const CLEAR_EVENT_NAMES: ReadonlySet<string> = new Set(["exception"]);
const MAX_VALUE = 256;
const MAX_PAYLOAD = 65536;
const CUTS = [16384, 4096, 1024, 256] as const;

/** A payload value: OTLP's AnyValue as JSON. */
export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
export type Payload = { [key: string]: Json };

/** Looks up the local variables captured for an exception, by its stack. */
export type LocalsLookup = (stack: string) => string | undefined;

export function toJson(v: unknown): Json {
  if (v === undefined || v === null) return null;
  if (typeof v === "string" || typeof v === "boolean") return v;
  if (typeof v === "number") return v;
  if (v instanceof Uint8Array) return { bytes_b64: Buffer.from(v).toString("base64") };
  if (Array.isArray(v)) return v.map(toJson);
  if (typeof v === "object") return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, toJson(x)]));
  return String(v);
}

function cutUtf8(s: string, limit: number): string {
  const b = Buffer.from(s);
  if (b.length <= limit) return s;
  let end = limit;
  while (end > 0 && (b[end]! & 0xc0) === 0x80) end--;
  return b.subarray(0, end).toString();
}

const isScalar = (v: unknown): v is string | number | boolean =>
  typeof v === "string" || typeof v === "number" || typeof v === "boolean";

/** Whether an allowlisted key's value has the shape the key promises: methods from the
 * known list, status codes as integers, database operations as one upper-case word (the pg
 * instrumentation takes it from the query text). */
export function shaped(key: string, v: unknown): boolean {
  switch (key) {
    case "http.request.method":
    case "http.method":
      return typeof v === "string" && METHODS.has(v);
    case "http.response.status_code":
    case "http.status_code":
      return typeof v === "number" && Number.isInteger(v);
    case "db.operation":
    case "db.operation.name":
      return typeof v === "string" && OPERATION.test(v);
    default:
      return isScalar(v);
  }
}

/** Keeps allowlisted scalars of the right shape (cutting long strings) and returns the rest
 * as `moved`. */
export function splitAttributes(attrs: Attributes, allow: ReadonlySet<string>): { kept: Attributes; moved: Payload } {
  const kept: Attributes = {};
  const moved: Payload = {};
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined) continue;
    if (allow.has(k) && isScalar(v) && shaped(k, v)) {
      if (typeof v === "string" && Buffer.byteLength(v) > MAX_VALUE) {
        moved[k] = v;
        kept[k] = cutUtf8(v, MAX_VALUE);
      } else {
        kept[k] = v;
      }
    } else {
      moved[k] = toJson(v);
    }
  }
  return { kept, moved };
}

const nonEmpty = (p: Payload): boolean => Object.keys(p).length > 0;

function cut(value: Json, limit: number, path: string, truncated: Set<string>): Json {
  if (typeof value === "string") {
    if (Buffer.byteLength(value) <= limit) return value;
    truncated.add(path);
    return cutUtf8(value, limit);
  }
  if (Array.isArray(value)) return value.map((v, i) => cut(v, limit, `${path}/${i}`, truncated));
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, cut(v, limit, path ? `${path}/${k}` : k, truncated)]));
  }
  return value;
}

/** The payload's JSON, cut to 64 KiB as the wire format says. */
export function encodePayload(payload: Payload): Buffer {
  let out = Buffer.from(JSON.stringify(payload));
  if (out.length <= MAX_PAYLOAD) return out;
  const truncated = new Set<string>();
  let p = payload;
  for (const limit of CUTS) {
    p = cut(p, limit, "", truncated) as Payload;
    p.truncated = [...truncated].sort();
    out = Buffer.from(JSON.stringify(p));
    if (out.length <= MAX_PAYLOAD) return out;
  }
  return Buffer.from(JSON.stringify({ truncated: ["*"] }));
}

/** Bytes travel as OTLP bytesValue: the serializer encodes a Uint8Array attribute so,
 * though the API's attribute type does not name it. */
const bytes = (b: Uint8Array): AttributeValue => b as unknown as AttributeValue;

function sealInto(sealer: Sealer, attrs: Attributes, payload: Payload): Attributes {
  if (!nonEmpty(payload)) return attrs;
  const { seq, ciphertext } = sealer.seal(encodePayload(payload));
  const head = Buffer.alloc(4);
  head.writeUInt32BE(seq);
  return { ...attrs, [SEALED]: bytes(Buffer.concat([head, ciphertext])) };
}

/** A resource as the serializer reads it. The SDK's own resource drops any attribute that
 * is not a simple value, and `clovelly.sealed.enc` is bytes, so the batch's copy is this. */
class SealedResource implements Resource {
  readonly asyncAttributesPending = false;
  readonly schemaUrl = undefined;
  constructor(readonly attributes: Attributes) {}

  merge(other: Resource | null): Resource {
    return other ? new SealedResource({ ...other.attributes, ...this.attributes }) : this;
  }

  getRawAttributes(): [string, AttributeValue | undefined][] {
    return Object.entries(this.attributes);
  }
}

/** One batch's resources: each original resource maps to one filtered copy carrying `enc`. */
export class Resources {
  readonly #made = new Map<Resource, Resource>();
  constructor(private readonly enc: Buffer | undefined) {}

  of(r: Resource): Resource {
    let out = this.#made.get(r);
    if (!out) {
      const { kept } = splitAttributes(r.attributes, RESOURCE_ALLOWLIST);
      out = new SealedResource(this.enc ? { ...kept, [SEALED_ENC]: bytes(this.enc) } : kept);
      this.#made.set(r, out);
    }
    return out;
  }
}

export function splitSpan(span: ReadableSpan, sealer: Sealer, resources: Resources, locals?: LocalsLookup): ReadableSpan {
  const payload: Payload = {};
  if (span.name) payload.name = span.name;
  if (span.status.message) payload.status_message = span.status.message;
  const { kept, moved } = splitAttributes(span.attributes, span.kind === SpanKind.CLIENT ? RECORD_ALLOWLIST : NO_PEER_ALLOWLIST);
  if (nonEmpty(moved)) payload.attributes = moved;
  const events: Json[] = [];
  const clearEvents: TimedEvent[] = span.events.map((e, index) => {
    const entry: Payload = {};
    const clearName = CLEAR_EVENT_NAMES.has(e.name) ? e.name : "";
    if (e.name && !clearName) entry.name = e.name;
    const split = splitAttributes(e.attributes ?? {}, NO_PEER_ALLOWLIST);
    const stack = e.attributes?.["exception.stacktrace"];
    const found = typeof stack === "string" ? locals?.(stack) : undefined;
    if (found) split.moved[LOCALS] = found;
    if (nonEmpty(split.moved)) entry.attributes = split.moved;
    if (nonEmpty(entry)) events.push({ index, ...entry });
    return { ...e, name: clearName, attributes: split.kept };
  });
  if (events.length) payload.events = events;
  const links: Json[] = [];
  const clearLinks = span.links.map((l, index) => {
    const split = splitAttributes(l.attributes ?? {}, NO_PEER_ALLOWLIST);
    if (nonEmpty(split.moved)) links.push({ index, attributes: split.moved });
    return { context: { ...l.context, traceState: undefined }, attributes: split.kept };
  });
  if (links.length) payload.links = links;
  const context = span.spanContext();
  return {
    name: "",
    kind: span.kind,
    spanContext: () => ({ ...context, traceState: undefined }),
    parentSpanContext: span.parentSpanContext,
    startTime: span.startTime,
    endTime: span.endTime,
    status: { code: span.status.code },
    attributes: sealInto(sealer, kept, payload),
    links: clearLinks,
    events: clearEvents,
    duration: span.duration,
    ended: span.ended,
    resource: resources.of(span.resource),
    instrumentationScope: { name: span.instrumentationScope.name, version: span.instrumentationScope.version },
    droppedAttributesCount: span.droppedAttributesCount,
    droppedEventsCount: span.droppedEventsCount,
    droppedLinksCount: span.droppedLinksCount,
  };
}

export function splitLog(record: ReadableLogRecord, sealer: Sealer, resources: Resources, locals?: LocalsLookup): ReadableLogRecord {
  const payload: Payload = {};
  if (record.body !== undefined && record.body !== null) payload.body = toJson(record.body);
  const { kept, moved } = splitAttributes(record.attributes, NO_PEER_ALLOWLIST);
  const stack = record.attributes["exception.stacktrace"];
  const found = typeof stack === "string" ? locals?.(stack) : undefined;
  if (found) moved[LOCALS] = found;
  if (nonEmpty(moved)) payload.attributes = moved;
  return {
    hrTime: record.hrTime,
    hrTimeObserved: record.hrTimeObserved,
    spanContext: record.spanContext,
    severityText: record.severityText,
    severityNumber: record.severityNumber,
    resource: resources.of(record.resource),
    instrumentationScope: { name: record.instrumentationScope.name, version: record.instrumentationScope.version },
    attributes: sealInto(sealer, kept, payload),
    droppedAttributesCount: record.droppedAttributesCount,
  };
}

/** Metrics carry no blob: keeps the allowlisted dimensions, drops the rest and the
 * description. Returns the copy and its data point count. */
export function stripMetrics(rm: ResourceMetrics): { metrics: ResourceMetrics; points: number } {
  let points = 0;
  const resources = new Resources(undefined);
  const scopeMetrics = rm.scopeMetrics.map((sm) => ({
    scope: { name: sm.scope.name, version: sm.scope.version },
    metrics: sm.metrics.map((m): MetricData => {
      points += m.dataPoints.length;
      const allow = metricAllowlist(m.descriptor.name);
      const dataPoints = m.dataPoints.map((p) => ({ ...p, attributes: splitAttributes(p.attributes, allow).kept }));
      return { ...m, descriptor: { ...m.descriptor, description: "" }, dataPoints } as MetricData;
    }),
  }));
  return { metrics: { resource: resources.of(rm.resource), scopeMetrics }, points };
}
