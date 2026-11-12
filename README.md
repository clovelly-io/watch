# Clovelly Watch

Watch adds monitors to each pull request you merge, and when one fires, an agent in a
confidential sandbox reproduces the bug, fixes it and opens a pull request for you to
review. This repository holds its public parts, under the Apache License 2.0:

- `python/`: the Python SDK, `clovelly-watch` (`clovelly_watch.init()`).
- `typescript/`: the Node SDK, `@clovelly/watch` (`import "@clovelly/watch/register"`).
- `runner/`: the runner, which holds your payload key; so far its key generation and the
  decryptor for sealed records.
- `docs/watch-wire.md`: what the SDKs send, what ingest keeps, and how payloads are sealed.
- `vectors/`: test vectors both SDKs and the runner pass.

## What we can read

The SDKs send a closed list of metadata in the clear (service, release, route template,
method, status, duration, peer host, database system and operation, exception type, code
file and function, log level and logger, and in Python a log call's unformatted template),
because monitors are evaluated on it. Everything else (bodies, headers, query strings, log
arguments, exception messages, stack traces, local variables, SQL) is sealed per record with
HPKE to a public key whose private half only your runner holds. Ingest enforces the same
list on whatever arrives. `docs/watch-wire.md` has the details.

## Tests

```bash
cd python && .venv/bin/python -m unittest discover -s tests -t .     # and ruff check src tests
cd runner && ../python/.venv/bin/python -m unittest discover -s tests -t .
cd typescript && npm test && npm run lint && npm run licences
```
