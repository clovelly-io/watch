# @clovelly/watch (Node)

```js
require("@clovelly/watch/register");   // or: import "@clovelly/watch/register"; as the first import
const express = require("express");
```

That is the whole change to a CommonJS service (or TypeScript compiled to CommonJS). An
ES-module service starts with `node --import @clovelly/watch/register app.mjs` instead,
because an ES module's static imports load before its first line runs. Node 22.15 or
later.

The environment is the same as the Python SDK's: `CLOVELLY_WATCH_TOKEN`,
`CLOVELLY_WATCH_KEY`, `CLOVELLY_WATCH_RELEASE` (or a platform's variable),
`CLOVELLY_WATCH_ENVIRONMENT`, `OTEL_SERVICE_NAME`, `CLOVELLY_WATCH_SAMPLE_RATE`, and
`CLOVELLY_WATCH_LOCALS=0` to turn off local-variable capture.

## What it sends

OpenTelemetry's instrumentation for `http` (servers, Next.js's included, and clients),
Express, `fetch` (undici), `pg` and pino, plus `console.warn` and `console.error` and
uncaught exceptions: request and outbound-call metrics, every failed span and a sampled
share of the rest, log records at WARN and above, and every unhandled exception. Local
variables of the service's own frames are captured when an exception is thrown, through
Node's inspector on the same thread, at most 20 throws a second. Fastify is not
instrumented yet (its instrumentation's dependencies fall outside our licence allowlist);
its requests still show at the `http` level, without route templates.

## It never hurts the service

As in Python: an 8 MiB queue that drops the oldest, no exception into the application,
nothing that keeps the process alive, warnings once each to stderr.
