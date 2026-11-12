// What the phase 16 review found missing (A3, A4, A10): an export too large for one request
// is split before sealing; a process sends what is pending when it ends by itself or on
// SIGTERM; and the latest capture of a stack's locals wins.
import { strict as assert } from "node:assert";
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { join } from "node:path";
import { test } from "node:test";
import { enqueueSealed } from "../src/export.js";
import { LocalsCapture } from "../src/hooks.js";

test("an export over the limit is sealed in parts under it", () => {
  const got: Uint8Array[] = [];
  const sink = { enqueue: (_: string, body: Uint8Array) => void got.push(body) };
  const seal = (rs: string[]) => Buffer.from(rs.join(""));
  enqueueSealed(sink, "/v1/logs", Array.from({ length: 10 }, () => "x".repeat(300)), seal, 1000);
  assert.ok(got.every((b) => b.length <= 1000));
  assert.equal(Buffer.concat(got).length, 3000);
  got.length = 0;
  enqueueSealed(sink, "/v1/logs", ["y".repeat(5000)], seal, 1000);
  assert.equal(got.length, 1, "one record goes alone, whatever its size");
});

test("the latest capture of a stack wins", () => {
  const capture = new LocalsCapture();
  capture.start(true);
  const stacks: string[] = [];
  for (const card of ["MARKER-FIRST", "MARKER-SECOND"]) {
    try {
      (function refund(c: string) {
        if (c) throw new Error("refund rejected");
      })(card);
    } catch (e) {
      stacks.push((e as Error).stack!);
    }
  }
  capture.stop();
  assert.equal(stacks[0], stacks[1], "one stack string for both");
  const found = capture.lookup(stacks[1]!) ?? "";
  assert.match(found, /MARKER-SECOND/);
  assert.doesNotMatch(found, /MARKER-FIRST/);
});

/** Runs `script` with the SDK registered against a local sink; resolves with the paths it was sent and how the
 * process ended. `signal` is sent after `afterMs`. */
async function run(script: string, signal?: { name: NodeJS.Signals; afterMs: number }) {
  const paths: string[] = [];
  const server = createServer((req, res) => {
    paths.push(req.url ?? "");
    req.resume();
    req.on("end", () => res.writeHead(200).end());
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const vector = JSON.parse(readFileSync(join(__dirname, "../../../vectors/hpke.json"), "utf8")).vectors[0];
  const key = `cwk1_${Buffer.from(vector.pkRm, "hex").toString("base64url")}`;
  const child = spawn(process.execPath, ["-e", `require(${JSON.stringify(join(__dirname, "../src/register.js"))}); ${script}`], {
    env: {
      ...process.env,
      CLOVELLY_WATCH_TOKEN: `cwt_${"a".repeat(64)}`,
      CLOVELLY_WATCH_KEY: key,
      CLOVELLY_WATCH_RELEASE: "b".repeat(40),
      CLOVELLY_WATCH_ENDPOINT: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
      CLOVELLY_WATCH_LOCALS: "0",
    },
    stdio: "ignore",
  });
  if (signal) setTimeout(() => child.kill(signal.name), signal.afterMs);
  const ended = await new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((r) =>
    child.on("exit", (code, sig) => r({ code, signal: sig })),
  );
  server.close();
  return { paths, ended };
}

test("a process that ends by itself sends its last warning", async () => {
  const { paths, ended } = await run('console.warn("bye"); setTimeout(() => {}, 300);');
  assert.equal(ended.code, 0);
  assert.ok(paths.includes("/v1/logs"), `sent ${paths.join(" ")}`);
});

test("SIGTERM sends what is pending, then ends the process as SIGTERM would", async () => {
  const { paths, ended } = await run('console.warn("stopping"); setInterval(() => {}, 1000);', { name: "SIGTERM", afterMs: 1500 });
  assert.equal(ended.signal, "SIGTERM");
  assert.ok(paths.includes("/v1/logs"), `sent ${paths.join(" ")}`);
});
