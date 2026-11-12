"""The runner's decryptor against the wire vectors: the CFRG's HPKE vectors opened at each
sequence number, and the SDK-sealed batch opened record by record."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

from clovelly_watch_runner import Opener, PrivateKey, generate
from clovelly_watch_runner.hpke import Receiver

VECTORS = Path(__file__).resolve().parents[2] / "vectors"


def recipient(v: dict) -> PrivateKey:
    return PrivateKey(ec.derive_private_key(int(v["skRm"], 16), ec.SECP256R1()))


class Vectors(unittest.TestCase):
    def test_hpke_vectors_open_at_their_sequence_numbers(self) -> None:
        for v in json.loads((VECTORS / "hpke.json").read_text())["vectors"]:
            r = Receiver(recipient(v).key, bytes.fromhex(v["enc"]), bytes.fromhex(v["info"]), v["aead_id"])
            self.assertEqual((r.key.hex(), r.base_nonce.hex()), (v["key"], v["base_nonce"]))
            # out of order on purpose: each record opens on its own
            for e in reversed(v["encryptions"]):
                self.assertEqual(r.open(e["seq"], bytes.fromhex(e["ct"]), bytes.fromhex(e["aad"])).hex(), e["pt"])

    def test_the_sdk_sealed_batch_opens_record_by_record(self) -> None:
        key = recipient(json.loads((VECTORS / "hpke.json").read_text())["vectors"][0])
        batch = json.loads((VECTORS / "batch.json").read_text())
        opener = Opener(key, bytes.fromhex(batch["enc_hex"]))
        for r in reversed(batch["records"]):
            self.assertEqual(opener.open(bytes.fromhex(r["sealed_hex"])), r["payload"])

    def test_a_tampered_record_does_not_open(self) -> None:
        key = recipient(json.loads((VECTORS / "hpke.json").read_text())["vectors"][0])
        batch = json.loads((VECTORS / "batch.json").read_text())
        sealed = bytearray.fromhex(batch["records"][0]["sealed_hex"])
        sealed[-1] ^= 1
        with self.assertRaises(Exception):  # noqa: B017 - InvalidTag, whatever its module
            Opener(key, bytes.fromhex(batch["enc_hex"])).open(bytes(sealed))


class Keys(unittest.TestCase):
    def test_a_generated_key_round_trips_through_its_text(self) -> None:
        k = generate()
        back = PrivateKey.parse(k.text())
        self.assertEqual(back.public_text(), k.public_text())
        self.assertTrue(k.public_text().startswith("cwk1_"))
        with self.assertRaises(ValueError):
            PrivateKey.parse("cwk1_" + k.text()[6:])


if __name__ == "__main__":
    unittest.main()
