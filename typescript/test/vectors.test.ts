// The wire vectors (docs/watch-wire.md, vectors/): HPKE sealing against the CFRG's own
// vectors, and the split per record.
import { strict as assert } from "node:assert";
import { createDecipheriv } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { test } from "node:test";
import { type Attributes, SpanKind } from "@opentelemetry/api";
import { resourceFromAttributes } from "@opentelemetry/resources";
import type { ReadableLogRecord } from "@opentelemetry/sdk-logs";
import type { ReadableSpan } from "@opentelemetry/sdk-trace";
import { AES_128_GCM, AES_256_GCM, Sealer } from "../src/hpke.js";
import { Resources, SEALED, metricAllowlist, splitAttributes, splitLog, splitSpan } from "../src/split.js";

const VECTORS = join(__dirname, "..", "..", "..", "vectors");
const load = (name: string) => JSON.parse(readFileSync(join(VECTORS, name), "utf8"));
const hex = (s: string) => Buffer.from(s, "hex");

type HpkeVector = {
  suite: string;
  aead_id: typeof AES_128_GCM | typeof AES_256_GCM;
  info: string;
  skEm: string;
  pkRm: string;
  enc: string;
  key: string;
  base_nonce: string;
  encryptions: { seq: number; aad: string; pt: string; nonce: string; ct: string }[];
};

test("sealing with the vectors' ephemeral key gives their enc and ciphertexts", () => {
  for (const v of load("hpke.json").vectors as HpkeVector[]) {
    const s = new Sealer(hex(v.pkRm), { info: hex(v.info), aeadId: v.aead_id, ephemeral: hex(v.skEm) });
    assert.equal(s.enc.toString("hex"), v.enc, v.suite);
    assert.equal(s.key.toString("hex"), v.key);
    assert.equal(s.baseNonce.toString("hex"), v.base_nonce);
    for (const e of v.encryptions) {
      s.seq = e.seq;
      assert.equal(s.nonce(e.seq).toString("hex"), e.nonce);
      const { seq, ciphertext } = s.seal(hex(e.pt), hex(e.aad));
      assert.equal(seq, e.seq);
      assert.equal(ciphertext.toString("hex"), e.ct, `${v.suite} seq ${e.seq}`);
    }
  }
});

/** Opens a record's clovelly.sealed with the sealer's own key, as the runner would. */
function opened(sealer: Sealer, attrs: Attributes): unknown {
  const sealed = attrs[SEALED] as unknown as Buffer | undefined;
  if (!sealed) return null;
  const seq = sealed.readUInt32BE(0);
  const body = sealed.subarray(4, sealed.length - 16);
  const d = createDecipheriv("aes-256-gcm", sealer.key, sealer.nonce(seq));
  d.setAuthTag(sealed.subarray(sealed.length - 16));
  return JSON.parse(Buffer.concat([d.update(body), d.final()]).toString());
}

const clear = (attrs: Attributes) => Object.fromEntries(Object.entries(attrs).filter(([k]) => k !== SEALED));

test("each record splits as the vector says", () => {
  const recipient = hex((load("hpke.json").vectors as HpkeVector[])[0]!.pkRm);
  const resource = resourceFromAttributes({ "service.name": "refunds", "host.name": "box-1" });
  const scope = { name: "test" };
  for (const c of load("split.json").cases) {
    const sealer = new Sealer(recipient);
    const resources = new Resources(sealer.enc);
    if (c.signal === "span") {
      const span = {
        name: c.record.name,
        // The vectors give OTLP's span kind; the API numbers kinds from INTERNAL = 0.
        kind: c.record.kind === 3 ? SpanKind.CLIENT : c.record.kind === 2 ? SpanKind.SERVER : SpanKind.INTERNAL,
        spanContext: () => ({ traceId: "1".repeat(32), spanId: "2".repeat(16), traceFlags: 1 }),
        startTime: [0, 0],
        endTime: [0, 0],
        status: c.record.status,
        attributes: c.record.attributes,
        links: [],
        events: c.record.events.map((e: { name: string; attributes: Attributes }) => ({ ...e, time: [0, 0] })),
        duration: [0, 0],
        ended: true,
        resource,
        instrumentationScope: scope,
        droppedAttributesCount: 0,
        droppedEventsCount: 0,
        droppedLinksCount: 0,
      } as ReadableSpan;
      const out = splitSpan(span, sealer, resources);
      assert.equal(out.name, c.clear.name, c.name);
      assert.equal(out.status.message ?? "", c.clear.status_message);
      assert.deepEqual(clear(out.attributes), c.clear.attributes, c.name);
      assert.deepEqual(out.events.map((e) => ({ name: e.name, attributes: clear(e.attributes ?? {}) })), c.clear.events);
      assert.deepEqual(opened(sealer, out.attributes), c.payload, c.name);
      assert.deepEqual(Object.keys(out.resource.attributes), ["service.name", "clovelly.sealed.enc"]);
    } else if (c.signal === "log") {
      const record = { hrTime: [0, 0], hrTimeObserved: [0, 0], body: c.record.body, attributes: c.record.attributes, resource, instrumentationScope: scope, droppedAttributesCount: 0 } as ReadableLogRecord;
      const out = splitLog(record, sealer, resources);
      assert.equal(out.body, undefined);
      assert.deepEqual(clear(out.attributes), c.clear.attributes, c.name);
      assert.deepEqual(opened(sealer, out.attributes), c.payload, c.name);
    } else {
      assert.deepEqual(splitAttributes(c.record.attributes, metricAllowlist(c.record.metric)).kept, c.clear.attributes, c.name);
    }
  }
});
