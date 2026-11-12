// The sender's promises (SPEC-watch.md "It never hurts the service"), the sampling, and
// local-variable capture.
import { strict as assert } from "node:assert";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { test } from "node:test";
import { Sender, sampled } from "../src/export.js";
import { LocalsCapture } from "../src/hooks.js";

test("an unreachable ingest keeps the queue under its bound, dropping the oldest", async () => {
  const sender = new Sender("http://127.0.0.1:9", "cwt_x", 10_000);
  const started = Date.now();
  for (let i = 0; i < 100; i++) sender.enqueue("/v1/logs", Buffer.alloc(1000, i));
  assert.ok(Date.now() - started < 200, "enqueue must not wait on the network");
  const { bytes } = sender.queued();
  assert.ok(bytes <= 10_000, `${bytes}`);
  assert.ok(sender.stats.droppedBatches >= 89);
  sender.close();
});

test("429 is retried and every batch arrives in order", async () => {
  let refuse = 2;
  const got: number[] = [];
  const server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      if (refuse-- > 0) {
        res.writeHead(429, { "retry-after": "0" }).end();
        return;
      }
      got.push(Buffer.concat(chunks)[0]!);
      res.writeHead(200).end();
    });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const sender = new Sender(`http://127.0.0.1:${(server.address() as AddressInfo).port}`, "cwt_x");
  for (let i = 0; i < 3; i++) sender.enqueue("/v1/traces", Buffer.from([i]));
  assert.ok(await sender.flush(10_000));
  assert.deepEqual(got, [0, 1, 2]);
  assert.equal(sender.stats.failedAttempts, 2);
  sender.close();
  server.close();
});

test("a trace is sampled or not as a whole, at about the rate", () => {
  let kept = 0;
  for (let i = 0; i < 20_000; i++) {
    const id = Buffer.from(Array.from({ length: 16 }, () => Math.floor(Math.random() * 256))).toString("hex");
    if (sampled(id, 0.1)) kept++;
    assert.equal(sampled(id, 0.1), sampled(id, 0.1));
    assert.equal(sampled(id, 0), false);
  }
  assert.ok(kept > 1800 && kept < 2200, `${kept}`);
});

test("a thrown exception's local variables are captured under its stack", () => {
  const capture = new LocalsCapture();
  capture.start(true);
  let stack = "";
  try {
    (function refund(amount: number) {
      if (amount < 0) {
        // block-scoped, as most variables are
        const card = "MARKER-LOCAL-9";
        throw new TypeError(`declined ${card.length}`);
      }
    })(-1);
  } catch (e) {
    stack = (e as Error).stack!;
  }
  capture.stop();
  const found = capture.lookup(stack);
  assert.ok(found, "nothing captured");
  const frames = JSON.parse(found) as { function: string; vars: Record<string, string> }[];
  assert.equal(frames[0]!.function, "refund");
  assert.equal(frames[0]!.vars.card, '"MARKER-LOCAL-9"');
  assert.equal(frames[0]!.vars.amount, "-1");
});
