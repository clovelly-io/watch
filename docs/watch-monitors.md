# Watch monitors

A monitor is a rule Watch evaluates over the metadata your service already sends (`docs/watch-wire.md`). The
monitors job writes monitors as JSON against this schema; Watch refuses anything else, naming the field. There is no
query language and no free text.

```json
{ "name": "refund-webhook-5xx",
  "source": { "pr": 412, "merge_sha": "<40 or 64 lowercase hex>", "file": "billing/refunds.py", "function": "handle_webhook" },
  "signal": { "request": { "route": "/webhooks/refund", "method": "POST", "status_class": "5xx" } },
  "condition": { "ratio_over": 0.05 },
  "window_minutes": 10 }
```

## Fields

- `name`: a slug, 1 to 64 characters of `a-z`, `0-9` and single hyphens, not starting or ending with one. Unique in
  the repository among monitors that are not deleted.
- `source`: the pull request that added the code: `pr` (a positive number), `merge_sha` (its merge commit on the
  default branch), `file`, and optionally `function`.
- `signal`: exactly one of the four below.
- `condition`: exactly one of `count_over`, `ratio_over`, `p95_over`, `absent`, on a signal that allows it.
- `window_minutes`: 1 to 60.

Every string is 1 to 256 bytes of printable ASCII with no spaces. Any other field, anywhere, is refused.

## Signals and the conditions each allows

| Signal | Fields | Counted from | Conditions |
|---|---|---|---|
| `request` | `route` (a route template starting with `/`), `method`?, `status_class`? | the `http.server.request.duration` histograms | `count_over`, `ratio_over`, `p95_over`, `absent` |
| `outbound` | `host` (the peer's `server.address`), `method`?, `status_class`? | `http.client.request.duration` | `count_over`, `ratio_over`, `p95_over`, `absent` |
| `error` | at least one of `type` (`exception.type`), `route`, `file`, `function` | span events named `exception` and log records with `exception.type` | `count_over` |
| `log` | `level` (`warning` or `error`), `logger`?, `file`?, `function`? | log records at or above the level; the logger is the record's scope name | `count_over`, `absent` |

`method` is one of `GET`, `HEAD`, `POST`, `PUT`, `PATCH`, `DELETE`, `OPTIONS`; `status_class` one of `1xx` to `5xx`.

## Conditions, over the window

- `{"count_over": N}`: more than N matching requests or records (N from 0).
- `{"ratio_over": R}`: more than R (between 0 and 1) of the route's (or host's) requests are in `status_class`,
  which a ratio requires.
- `{"p95_over": MS}`: the 95th percentile duration is over MS milliseconds (1 to 600,000): more than 5% of matching
  requests fall in histogram buckets that lie wholly above MS (a bucket's lower bound at or above it). Percentiles
  are read at the SDK's bucket resolution (5, 10, 25, 50, 75, 100, 250, 500, 750 ms, 1, 2.5, 5, 7.5, 10 s): a
  threshold between two bounds is read at the next bound up, so it never fires while the percentile is under it but
  may fire later. Pick a bound.
- `{"absent": true}`: nothing matched in the whole window. An absent monitor is first evaluated on the first whole
  window after it armed.

## Lifecycle

A monitor is `pending` until a release at or after its merge commit on the default branch reports in (the SDK's
`vcs.ref.head.revision`); a release that is not on the default branch arms nothing. Then it is `armed`, and only
minutes from then on count. Windows are counted by the minute Watch received the records in, and each minute is
evaluated a few seconds after it ends. A firing monitor has one open alert, which closes after two quiet windows.
`muted` and `deleted` monitors count nothing.
