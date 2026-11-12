# The Watch wire format, version 1

What the Watch SDKs send, what ingest keeps of it, and how the runner opens the sealed
part. The SDKs (`python/`, `typescript/`) and the runner's decryptor (`runner/`) all pass
the test vectors in `vectors/`.

The short version: OpenTelemetry's own OTLP over HTTP, with each record split in two.
A closed list of metadata stays readable, because monitors are evaluated on it. Everything
else about the record (its message, body, headers, query string, SQL, exception message,
stack trace, local variables) goes into one blob per record, sealed with HPKE to a public
key whose private half only the runner holds. Ingest can read the metadata and cannot
open the blob.

## Transport

- `POST {endpoint}/v1/traces`, `/v1/logs` and `/v1/metrics`, each body one OTLP
  `Export{Trace,Logs,Metrics}ServiceRequest` in binary protobuf
  (`Content-Type: application/x-protobuf`), uncompressed. The endpoint is
  `https://ingest.clovelly.io` unless `CLOVELLY_WATCH_ENDPOINT` says otherwise.
- `Authorization: Bearer <CLOVELLY_WATCH_TOKEN>`. The token (`cwt_` and 64 hex characters)
  belongs to one repository and can only send.
- Answers: `200` with an empty export response (accepted); `400` (not an OTLP request);
  `401` (no valid token); `403` (a token that cannot send); `413` (over 4 MiB); `415` (not
  binary protobuf, or compressed); `429` (over the repository's rate limit; `Retry-After`
  says when to try again); `503` (try again). A sender retries `429` and `5xx` with backoff
  and drops the batch on any other `4xx`.

## Records and what stays readable

A record is a span, a log record or a metric data point. Each keeps OTLP's structural
fields: ids, parent, flags, kind, timestamps, status code, severity number and text,
instrumentation scope name and version (for a Python log record the scope is the logger
name), metric name and unit, and data point values.

Attributes kept in the clear, the allowlist:

| Where | Keys |
|---|---|
| Resource | `service.name`, `deployment.environment.name`, `vcs.ref.head.revision` (the release), `clovelly.sealed.enc` |
| Spans, span events, links, log records, data points | `http.route`, `http.request.method`, `http.method`, `http.response.status_code`, `http.status_code`, `server.address`, `net.peer.name`, `db.system`, `db.system.name`, `db.operation`, `db.operation.name`, `exception.type`, `code.filepath`, `code.file.path`, `code.function`, `code.function.name`, `clovelly.log.template` |
| Spans and log records | `clovelly.sealed` |

Old and stable semantic-convention names are both listed because instrumentations still
emit either. An allowlisted value is a string, boolean, integer or double, never an array,
map or bytes; a string over 256 bytes is cut at a character boundary. A key appears once.
Some keys are kept only in their shape, since instrumentations fill them from what a
caller sent; otherwise they are sealed (or, on a data point, dropped):

- `server.address` and `net.peer.name` only on CLIENT spans and on client metrics
  (`http.client.*`, `db.client.*`): on a server span they come from the Host and
  X-Forwarded-Host headers.
- `http.request.method` and `http.method` only as one of `GET`, `HEAD`, `POST`, `PUT`,
  `PATCH`, `DELETE`, `OPTIONS`, `CONNECT`, `TRACE`, `_OTHER`.
- `http.response.status_code` and `http.status_code` only as integers.
- `db.operation` and `db.operation.name` only as one upper-case word of at most 32 letters:
  SQL instrumentations take them from the query text.

Ingest applies the same rules to whatever arrives.
`clovelly.log.template` is a Python log call's unformatted message (`"refund failed for
%s"`), sent only when the call passed arguments; without them (an f-string, `str(exc)`)
the message is already rendered, and the rendered message is sealed in both languages.

Not kept in the clear: span names, status messages, span event names other than
`exception`, trace state, log bodies and event names, metric descriptions and metadata,
scope attributes, schema URLs, exemplars, and every attribute off the list.

## The release

The resource attribute `vcs.ref.head.revision` is the running commit: 40 (or 64) lowercase
hex characters, from `CLOVELLY_WATCH_RELEASE` or the first set of the platform variables
the SDK README lists. Anything else is not a release. Records without one are stored and
arm no monitor.

## Sealing

**Suite.** HPKE (RFC 9180) base mode, `DHKEM(P-256, HKDF-SHA256)` (`0x0010`),
`HKDF-SHA256` (`0x0001`), `AES-256-GCM` (`0x0002`). `info` is the ASCII string
`clovelly-watch payload v1`; `aad` is empty.

**Keys.** `CLOVELLY_WATCH_KEY` is `cwk1_` followed by the base64url (no padding) of the
recipient public key, SEC1 uncompressed (65 bytes). The runner's private key is `cwsk1_`
followed by the base64url of its 32-byte scalar.

**One encapsulation per request.** The sender runs `SetupBaseS(pkR, info)` once per export
request and puts `enc` (65 bytes) on every resource in the request as the bytes attribute
`clovelly.sealed.enc`.

**One blob per record.** Each span and log record that has anything to seal gets the
bytes attribute `clovelly.sealed`:

```
clovelly.sealed = seq (4 bytes, big-endian) || Seal(key, ComputeNonce(seq), aad = "", pt)
```

`seq` counts the request's sealed records from 0, so each nonce is used once.
`ComputeNonce(seq)` is RFC 9180's: `base_nonce` XOR `seq` as a 12-byte big-endian
integer. Because the record carries `seq`, a receiver that holds `enc` opens any one record
on its own: `SetupBaseR(enc, skR, info)`, then AES-256-GCM open with `key` and
`ComputeNonce(seq)`. Standard HPKE libraries open in sequence only; the runner computes the
nonce itself from the key schedule.

**Metrics carry no blob.** A data point's attributes are its dimensions; the SDK sends the
allowlisted ones and drops the rest, unsealed.

## The payload

`pt` is a UTF-8 JSON object. Every member is optional:

```
{
  "name": "POST /refund",                    span name
  "status_message": "…",                     span status message
  "body": <value>,                           log body
  "attributes": {"key": <value>, …},         the record's attributes off the allowlist
  "events": [{"index": 0, "name": "…", "attributes": {…}}, …],
                                             span events: name if not "exception",
                                             attributes off the allowlist
  "links": [{"index": 0, "attributes": {…}}, …],
  "truncated": ["attributes/http.request.body", …]
}
```

Local variables travel as an attribute like any other off the list: `clovelly.locals`, a
JSON string, on the exception event of a span or on a log record that carries an
exception. It is an array of the frames the exception passed through, innermost first,
each `{"function": "…", "file": "…", "line": 12, "vars": {"name": "<repr>", …}}`, at most
10 frames of 50 variables, each value cut to 512 bytes.

A `<value>` is OTLP's `AnyValue` as JSON: a string, boolean or number as itself, an array
as an array, a map as an object, bytes as `{"bytes_b64": "<base64>"}`.

**At most 64 KiB.** If the JSON is over 65,536 bytes, the sender cuts every string longer
than 16,384 bytes to that length (at a character boundary) and lists its path in
`truncated`, then 4,096, 1,024 and 256 bytes in turn until the payload fits. If even that
is too large, the payload is `{"truncated": ["*"]}`. A path is the member names and array
indexes from the top, joined by `/`.

## What ingest keeps

Ingest applies the same rules to whatever arrives, so a sender that does not split (or
splits wrongly) still cannot put content in the store: every field and attribute above
that is not kept in the clear is removed and counted, an allowlisted string over 256
bytes is cut and counted, a `clovelly.sealed` that is not bytes of 5 to 65,556 bytes and a
`clovelly.sealed.enc` that is not 65 bytes are removed. What is left is re-encoded and
stored as the batch.

## Test vectors

- `vectors/hpke.json`: the CFRG's HPKE vectors for Watch's suite (and RFC 9180 A.3's,
  which differs only in key length), base mode. An implementation derives `key` and
  `base_nonce` from `skRm` and `enc`, opens each `ct` at its `seq`, and, sealing with
  `skEm` as the ephemeral key, produces `enc` and each `ct`.
- `vectors/split.json`: records before and after the split, as the attributes kept in
  the clear and the payload JSON.
- `vectors/batch.json`: a logs export request sealed to `vectors/hpke.json`'s first
  recipient key, with the payloads it opens to.
