// Fails unless every installed production dependency is under a licence SPEC.md's
// "Licence rule" allows: MIT, Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, Unicode or CC0.
// Dev dependencies (the compiler and Node's types) are not shipped and not checked.
//
//   node scripts/check-licences.mjs     (after npm install)
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ALLOWED = new Set([
  "MIT",
  "Apache-2.0",
  "BSD-2-Clause",
  "BSD-3-Clause",
  "ISC",
  "Unicode-DFS-2016",
  "Unicode-3.0",
  "CC0-1.0",
]);

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

// npm ls exits non-zero (and execFileSync throws) if the installed tree is missing or
// invalid. The first line is this package itself.
const paths = execFileSync("npm", ["ls", "--omit=dev", "--all", "--parseable"], { cwd: root, encoding: "utf8" })
  .trim()
  .split("\n")
  .slice(1);

/** The package's licence as an SPDX expression, from `license` or the legacy `licenses`. */
function licence({ license, licenses }) {
  if (typeof license === "string") return license;
  if (license?.type) return license.type;
  if (Array.isArray(licenses)) return licenses.map((entry) => entry.type ?? entry).join(" OR ");
  return "UNKNOWN";
}

/** True if some alternative of an `A OR (B AND C)` expression has only allowed licences. */
function allowed(expression) {
  return expression
    .replace(/[()]/g, "")
    .split(/\s+OR\s+/)
    .some((alternative) => alternative.split(/\s+AND\s+/).every((id) => ALLOWED.has(id.trim())));
}

const rows = paths.map((path) => {
  const manifest = JSON.parse(readFileSync(join(path, "package.json"), "utf8"));
  const id = licence(manifest);
  return { name: manifest.name, version: manifest.version, licence: id, ok: allowed(id) };
});
rows.sort((a, b) => a.name.localeCompare(b.name));

const width = (key, header) => Math.max(header.length, ...rows.map((row) => row[key].length));
const [name, version, lic] = [width("name", "package"), width("version", "version"), width("licence", "licence")];
console.log(`${"package".padEnd(name)}  ${"version".padEnd(version)}  ${"licence".padEnd(lic)}  ok`);
for (const row of rows) {
  console.log(`${row.name.padEnd(name)}  ${row.version.padEnd(version)}  ${row.licence.padEnd(lic)}  ${row.ok ? "yes" : "NO"}`);
}

const refused = rows.filter((row) => !row.ok);
if (rows.length === 0 || refused.length > 0) {
  console.error(rows.length === 0 ? "no production dependencies found: run npm install" : `refused: ${refused.map((row) => row.name).join(", ")}`);
  process.exit(1);
}
console.log(`${rows.length} production packages, all allowed`);
