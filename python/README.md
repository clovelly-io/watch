# clovelly-watch (Python)

```python
import clovelly_watch
clovelly_watch.init()          # before the web framework's app is created

from fastapi import FastAPI
app = FastAPI()
```

That is the whole change to the service. Set, in its environment:

| Variable | |
|---|---|
| `CLOVELLY_WATCH_TOKEN` | the repository's ingest token (`cwt_…`), which can only send |
| `CLOVELLY_WATCH_KEY` | the repository's payload public key (`cwk1_…`), from the runner |
| `CLOVELLY_WATCH_RELEASE` | the deployed commit SHA, if your platform does not set one of `VERCEL_GIT_COMMIT_SHA`, `RENDER_GIT_COMMIT`, `RAILWAY_GIT_COMMIT_SHA`, `HEROKU_SLUG_COMMIT`, `SOURCE_VERSION`, `COMMIT_REF` or `GITHUB_SHA`. Without one the SDK warns once and its records arm no monitor |
| `CLOVELLY_WATCH_ENVIRONMENT` | optional, e.g. `production` |
| `OTEL_SERVICE_NAME` | optional, the service's name |
| `CLOVELLY_WATCH_SAMPLE_RATE` | optional, the share of successful request spans sent (default 0.1); failed ones are always sent |

## What it sends

OpenTelemetry's own instrumentation for FastAPI, Django, Flask, `requests`, `httpx`,
`psycopg` and SQLAlchemy (each only if installed), plus Python `logging` at WARNING and
above and uncaught exceptions in any thread: request and outbound-call metrics (delta
counts and duration histograms per route, method and status), every failed span and a
sampled share of the rest (decided per trace when the span ends), every log record at
WARNING or above, and every unhandled exception with the local variables of its frames.
What is sealed and what is not is `docs/watch-wire.md`. Pass values to log calls as
arguments (`log.warning("refund failed for %s", card)`): the template is readable to us,
and an f-string puts the value in the template.

## It never hurts the service

Records are exported off the request path into a queue of at most 8 MiB that drops the
oldest batch when full. Nothing the SDK does raises into the application; an unreachable
ingest or a 429 changes nothing the service's callers see. Its own warnings go to stderr,
once each.

## Licences

Every dependency is pinned in `pyproject.toml` and checked by `check_licences.py`.
OpenTelemetry's API and SDK depend on `typing-extensions`, which is under the PSF licence,
outside the allowlist our other SDKs keep to; the check reports it.
