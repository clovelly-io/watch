"""The wire vectors (docs/watch-wire.md, vectors/): HPKE sealing against the CFRG's own
vectors, the split per record, and the sealed batch, which this file also writes:

    python -m tests.test_vectors write      (from python/, after a deliberate change)
"""

from __future__ import annotations

import json
import struct
import sys
import unittest
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue
from opentelemetry.proto.logs.v1.logs_pb2 import LogRecord
from opentelemetry.proto.metrics.v1.metrics_pb2 import NumberDataPoint
from opentelemetry.proto.trace.v1.trace_pb2 import Span

from clovelly_watch import _hpke, _split

VECTORS = Path(__file__).resolve().parents[2] / "vectors"


def any_value(v: Any) -> AnyValue:
    if isinstance(v, bool):
        return AnyValue(bool_value=v)
    if isinstance(v, int):
        return AnyValue(int_value=v)
    if isinstance(v, float):
        return AnyValue(double_value=v)
    if isinstance(v, list):
        return AnyValue(array_value=ArrayValue(values=[any_value(x) for x in v]))
    return AnyValue(string_value=v)


def kvs(d: dict[str, Any]) -> list[KeyValue]:
    return [KeyValue(key=k, value=any_value(v)) for k, v in d.items()]


def clear_attrs(attrs: Any) -> dict[str, Any]:
    return {kv.key: _split.to_json(kv.value) for kv in attrs if kv.key != _split.SEALED}


def opened(sealer: _hpke.Sealer, attrs: Any) -> dict[str, Any] | None:
    sealed = [kv.value.bytes_value for kv in attrs if kv.key == _split.SEALED]
    if not sealed:
        return None
    (seq,) = struct.unpack(">I", sealed[0][:4])
    return json.loads(AESGCM(sealer.key).decrypt(sealer.nonce(seq), sealed[0][4:], b""))


def recipient() -> Any:
    v = json.loads((VECTORS / "hpke.json").read_text())["vectors"][0]
    return v, _hpke.public_key(bytes.fromhex(v["pkRm"])), _hpke.private_key(bytes.fromhex(v["skEm"]))


def batch_request() -> tuple[ExportLogsServiceRequest, _hpke.Sealer]:
    """vectors/batch.json's request: two log records sealed to the first HPKE vector's
    recipient, with its ephemeral key."""
    _, pk_r, sk_e = recipient()
    request = ExportLogsServiceRequest()
    rl = request.resource_logs.add()
    rl.resource.attributes.extend(kvs({"service.name": "refunds", "vcs.ref.head.revision": "0" * 40}))
    sl = rl.scope_logs.add()
    sl.scope.name = "billing.refunds"
    for i, card in enumerate(["MARKER-1111", "MARKER-2222"]):
        sl.log_records.append(
            LogRecord(
                time_unix_nano=1_794_441_600_000_000_000 + i,
                severity_number=13,
                severity_text="WARNING",
                body=AnyValue(string_value=f"refund failed for {card}"),
                attributes=kvs({"clovelly.log.template": "refund failed for %s", "card": card}),
            )
        )
    sealer = _hpke.Sealer(pk_r, ephemeral=sk_e)
    _split.seal_logs(request, sealer)
    return request, sealer


def batch_document() -> dict[str, Any]:
    request, sealer = batch_request()
    records = request.resource_logs[0].scope_logs[0].log_records
    return {
        "about": (
            "An ExportLogsServiceRequest sealed by the Python SDK to vectors/hpke.json's first recipient (skRm), "
            "with its skEm as the ephemeral key. Each record's clovelly.sealed opens to its payload."
        ),
        "request_hex": request.SerializeToString().hex(),
        "enc_hex": sealer.enc.hex(),
        "records": [
            {
                "sealed_hex": next(kv.value.bytes_value for kv in r.attributes if kv.key == _split.SEALED).hex(),
                "payload": opened(sealer, r.attributes),
            }
            for r in records
        ],
    }


class HpkeVectors(unittest.TestCase):
    def test_sealing_with_the_vectors_ephemeral_key_gives_their_enc_and_ciphertexts(self) -> None:
        for v in json.loads((VECTORS / "hpke.json").read_text())["vectors"]:
            sealer = _hpke.Sealer(
                _hpke.public_key(bytes.fromhex(v["pkRm"])),
                info=bytes.fromhex(v["info"]),
                aead_id=v["aead_id"],
                ephemeral=_hpke.private_key(bytes.fromhex(v["skEm"])),
            )
            self.assertEqual(sealer.enc.hex(), v["enc"])
            self.assertEqual(sealer.key.hex(), v["key"])
            self.assertEqual(sealer.base_nonce.hex(), v["base_nonce"])
            for e in v["encryptions"]:
                sealer.seq = e["seq"]
                seq, ct = sealer.seal(bytes.fromhex(e["pt"]), bytes.fromhex(e["aad"]))
                self.assertEqual((seq, ct.hex()), (e["seq"], e["ct"]), v["suite"])
                self.assertEqual(sealer.nonce(seq).hex(), e["nonce"])


class SplitVectors(unittest.TestCase):
    def test_each_record_splits_as_the_vector_says(self) -> None:
        _, pk_r, _ = recipient()
        for case in json.loads((VECTORS / "split.json").read_text())["cases"]:
            with self.subTest(case["name"]):
                sealer = _hpke.Sealer(pk_r)
                r, clear = case["record"], case["clear"]
                if case["signal"] == "span":
                    span = Span(name=r["name"], kind=r.get("kind", 0), attributes=kvs(r["attributes"]))
                    span.status.code = r["status"]["code"]
                    span.status.message = r["status"]["message"]
                    for e in r["events"]:
                        span.events.add(name=e["name"], attributes=kvs(e["attributes"]))
                    _split.split_span(span, sealer)
                    self.assertEqual(span.name, clear["name"])
                    self.assertEqual(span.status.message, clear["status_message"])
                    self.assertEqual(clear_attrs(span.attributes), clear["attributes"])
                    got = [{"name": e.name, "attributes": clear_attrs(e.attributes)} for e in span.events]
                    self.assertEqual(got, clear["events"])
                    self.assertEqual(opened(sealer, span.attributes), case["payload"])
                elif case["signal"] == "log":
                    record = LogRecord(body=AnyValue(string_value=r["body"]), attributes=kvs(r["attributes"]))
                    _split.split_log(record, sealer)
                    self.assertFalse(record.HasField("body"))
                    self.assertEqual(clear_attrs(record.attributes), clear["attributes"])
                    self.assertEqual(opened(sealer, record.attributes), case["payload"])
                else:
                    point = NumberDataPoint(attributes=kvs(r["attributes"]))
                    _split._drop_attributes(point.attributes, _split.metric_allowlist(r["metric"]))
                    self.assertEqual(clear_attrs(point.attributes), clear["attributes"])


class BatchVector(unittest.TestCase):
    def test_the_sealed_batch_is_what_the_sdk_produces(self) -> None:
        self.assertEqual(batch_document(), json.loads((VECTORS / "batch.json").read_text()))


if __name__ == "__main__":
    if sys.argv[1:] == ["write"]:
        (VECTORS / "batch.json").write_text(json.dumps(batch_document(), indent=1) + "\n")
    else:
        unittest.main()
