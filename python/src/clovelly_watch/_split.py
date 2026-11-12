"""The split (docs/watch-wire.md): each record keeps a closed list of metadata in the
clear, and everything else goes into one HPKE-sealed blob per record. Works on the OTLP
protobuf messages OpenTelemetry's own encoders produce, in place, just before they are
serialised.
"""

from __future__ import annotations

import base64
import json
import re
import struct
from typing import Any

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue

from ._hpke import Sealer

SEALED = "clovelly.sealed"
SEALED_ENC = "clovelly.sealed.enc"
RELEASE = "vcs.ref.head.revision"
RESOURCE_ALLOWLIST = frozenset({"service.name", "deployment.environment.name", RELEASE})
RECORD_ALLOWLIST = frozenset(
    {
        "http.route",
        "http.request.method",
        "http.method",
        "http.response.status_code",
        "http.status_code",
        "server.address",
        "net.peer.name",
        "db.system",
        "db.system.name",
        "db.operation",
        "db.operation.name",
        "exception.type",
        "code.filepath",
        "code.file.path",
        "code.function",
        "code.function.name",
        "clovelly.log.template",
    }
)
# The peer's host is kept only where it names whom the service called: on CLIENT spans and
# client metrics. On a server span instrumentations fill it from the Host or
# X-Forwarded-Host header, which the caller writes.
PEER_KEYS = frozenset({"server.address", "net.peer.name"})
NO_PEER_ALLOWLIST = RECORD_ALLOWLIST - PEER_KEYS
SPAN_KIND_CLIENT = 3
CLIENT_METRIC_PREFIXES = ("http.client.", "db.client.")
# Kept only in these shapes; any other value of these keys is sealed (or, on a data point,
# dropped). The SQL instrumentations take the operation from the query text.
METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "CONNECT", "TRACE", "_OTHER"})
_OPERATION = re.compile(r"[A-Z]{1,32}")
CLEAR_EVENT_NAMES = frozenset({"exception"})
MAX_VALUE = 256
MAX_PAYLOAD = 65536
_CUTS = (16384, 4096, 1024, 256)
_SCALARS = ("string_value", "bool_value", "int_value", "double_value")


def to_json(v: AnyValue) -> Any:
    """OTLP's AnyValue as the payload's JSON value."""
    kind = v.WhichOneof("value")
    if kind is None:
        return None
    if kind == "bytes_value":
        return {"bytes_b64": base64.b64encode(v.bytes_value).decode()}
    if kind == "array_value":
        return [to_json(x) for x in v.array_value.values]
    if kind == "kvlist_value":
        return {kv.key: to_json(kv.value) for kv in v.kvlist_value.values}
    return getattr(v, kind)


def _cut_utf8(s: str, limit: int) -> str:
    return s.encode()[:limit].decode("utf-8", "ignore")


def shaped(key: str, value: AnyValue) -> bool:
    """Whether an allowlisted key's value has the shape the key promises."""
    kind = value.WhichOneof("value")
    if key in ("http.request.method", "http.method"):
        return kind == "string_value" and value.string_value in METHODS
    if key in ("http.response.status_code", "http.status_code"):
        return kind == "int_value"
    if key in ("db.operation", "db.operation.name"):
        return kind == "string_value" and _OPERATION.fullmatch(value.string_value) is not None
    return kind in _SCALARS


def _split_attributes(attrs: Any, allow: frozenset[str]) -> dict[str, Any]:
    """Keeps allowlisted scalars of the right shape in `attrs` (cutting long strings) and
    returns the rest, as the payload's `attributes`."""
    moved: dict[str, Any] = {}
    kept: list[KeyValue] = []
    for kv in attrs:
        kind = kv.value.WhichOneof("value")
        if kv.key in allow and shaped(kv.key, kv.value):
            if kind == "string_value" and len(kv.value.string_value.encode()) > MAX_VALUE:
                moved[kv.key] = kv.value.string_value
                kv.value.string_value = _cut_utf8(kv.value.string_value, MAX_VALUE)
            kept.append(kv)
        else:
            moved[kv.key] = to_json(kv.value)
    del attrs[:]
    attrs.extend(kept)
    return moved


def _drop_attributes(attrs: Any, allow: frozenset[str]) -> None:
    """Metric data points: keeps allowlisted scalars and drops the rest, unsealed."""
    _split_attributes(attrs, allow)


def _cut(value: Any, limit: int, path: str, truncated: list[str]) -> Any:
    if isinstance(value, str):
        if len(value.encode()) > limit:
            truncated.append(path)
            return _cut_utf8(value, limit)
        return value
    if isinstance(value, list):
        return [_cut(v, limit, f"{path}/{i}", truncated) for i, v in enumerate(value)]
    if isinstance(value, dict):
        return {k: _cut(v, limit, f"{path}/{k}" if path else k, truncated) for k, v in value.items()}
    return value


def encode_payload(payload: dict[str, Any]) -> bytes:
    """The payload's JSON, cut to 64 KiB as the wire format says."""

    def dump(p: dict[str, Any]) -> bytes:
        return json.dumps(p, separators=(",", ":"), ensure_ascii=False).encode()

    out = dump(payload)
    if len(out) <= MAX_PAYLOAD:
        return out
    truncated: list[str] = []
    for limit in _CUTS:
        payload = _cut(payload, limit, "", truncated)
        payload["truncated"] = sorted(set(truncated))
        out = dump(payload)
        if len(out) <= MAX_PAYLOAD:
            return out
    return dump({"truncated": ["*"]})


def _seal(sealer: Sealer, attrs: Any, payload: dict[str, Any]) -> None:
    if not payload:
        return
    seq, ct = sealer.seal(encode_payload(payload))
    attrs.append(KeyValue(key=SEALED, value=AnyValue(bytes_value=struct.pack(">I", seq) + ct)))


def _resource(resource: Any, sealer: Sealer | None) -> None:
    _drop_attributes(resource.attributes, RESOURCE_ALLOWLIST)
    if sealer is not None:
        resource.attributes.append(KeyValue(key=SEALED_ENC, value=AnyValue(bytes_value=sealer.enc)))


def _scope(scope_container: Any) -> None:
    del scope_container.scope.attributes[:]
    scope_container.schema_url = ""


def split_span(span: Any, sealer: Sealer) -> None:
    payload: dict[str, Any] = {}
    if span.name:
        payload["name"] = span.name
        span.name = ""
    if span.status.message:
        payload["status_message"] = span.status.message
        span.status.message = ""
    span.trace_state = ""
    allow = RECORD_ALLOWLIST if span.kind == SPAN_KIND_CLIENT else NO_PEER_ALLOWLIST
    moved = _split_attributes(span.attributes, allow)
    if moved:
        payload["attributes"] = moved
    events = []
    for i, event in enumerate(span.events):
        entry: dict[str, Any] = {}
        if event.name and event.name not in CLEAR_EVENT_NAMES:
            entry["name"] = event.name
            event.name = ""
        moved = _split_attributes(event.attributes, NO_PEER_ALLOWLIST)
        if moved:
            entry["attributes"] = moved
        if entry:
            events.append({"index": i, **entry})
    if events:
        payload["events"] = events
    links = []
    for i, link in enumerate(span.links):
        link.trace_state = ""
        moved = _split_attributes(link.attributes, NO_PEER_ALLOWLIST)
        if moved:
            links.append({"index": i, "attributes": moved})
    if links:
        payload["links"] = links
    _seal(sealer, span.attributes, payload)


def split_log(record: Any, sealer: Sealer) -> None:
    payload: dict[str, Any] = {}
    if record.HasField("body"):
        payload["body"] = to_json(record.body)
        record.ClearField("body")
    record.event_name = ""
    moved = _split_attributes(record.attributes, NO_PEER_ALLOWLIST)
    if moved:
        payload["attributes"] = moved
    _seal(sealer, record.attributes, payload)


def seal_traces(request: Any, sealer: Sealer) -> int:
    """Splits every span of an ExportTraceServiceRequest; returns the record count."""
    n = 0
    for rs in request.resource_spans:
        _resource(rs.resource, sealer)
        rs.schema_url = ""
        for ss in rs.scope_spans:
            _scope(ss)
            for span in ss.spans:
                split_span(span, sealer)
                n += 1
    return n


def seal_logs(request: Any, sealer: Sealer) -> int:
    n = 0
    for rl in request.resource_logs:
        _resource(rl.resource, sealer)
        rl.schema_url = ""
        for sl in rl.scope_logs:
            _scope(sl)
            for record in sl.log_records:
                split_log(record, sealer)
                n += 1
    return n


def metric_allowlist(name: str) -> frozenset[str]:
    """The peer's host is a dimension of client metrics only."""
    return RECORD_ALLOWLIST if name.startswith(CLIENT_METRIC_PREFIXES) else NO_PEER_ALLOWLIST


def strip_metrics(request: Any) -> int:
    """Metrics carry no blob: keeps the allowlisted dimensions and drops the rest."""
    n = 0
    for rm in request.resource_metrics:
        _resource(rm.resource, None)
        rm.schema_url = ""
        for sm in rm.scope_metrics:
            _scope(sm)
            for metric in sm.metrics:
                metric.description = ""
                del metric.metadata[:]
                kind = metric.WhichOneof("data")
                if kind is None:
                    continue
                allow = metric_allowlist(metric.name)
                for point in getattr(metric, kind).data_points:
                    _drop_attributes(point.attributes, allow)
                    if kind != "summary":
                        del point.exemplars[:]
                    n += 1
    return n
