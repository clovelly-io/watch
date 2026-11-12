/**
 * `import "@clovelly/watch/register"` as the service's first import (CommonJS, or
 * TypeScript compiled to it), or `node --import @clovelly/watch/register app.mjs` for an
 * ES-module service: an ES module's static imports are loaded before its first line runs,
 * so only `--import` reaches them.
 */
import { register } from "node:module";
import { pathToFileURL } from "node:url";
import { init } from "./index.js";

try {
  // OpenTelemetry's loader hook, so ES modules imported from here on are instrumented too.
  register("@opentelemetry/instrumentation/hook.mjs", pathToFileURL(__filename));
} catch {
  // CommonJS services do not need it
}
init();
