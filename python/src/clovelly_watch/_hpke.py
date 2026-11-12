"""HPKE (RFC 9180) base mode, sender side, for one suite: DHKEM(P-256, HKDF-SHA256),
HKDF-SHA256 and AES-GCM (AES-256-GCM is Watch's; AES-128-GCM is here only for RFC 9180's
own vectors). Built from `cryptography`'s primitives and checked against the CFRG vectors
in `vectors/hpke.json`. The runner has its own implementation of the receiving side.
"""

from __future__ import annotations

import struct

from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

KEM_ID, KDF_ID = 0x0010, 0x0001
AES_128_GCM, AES_256_GCM = 0x0001, 0x0002
_NK = {AES_128_GCM: 16, AES_256_GCM: 32}
NN = 12
INFO = b"clovelly-watch payload v1"


def _hmac(key: bytes, data: bytes) -> bytes:
    h = hmac.HMAC(key or b"\x00" * 32, hashes.SHA256())
    h.update(data)
    return h.finalize()


def _labeled_extract(suite: bytes, salt: bytes, label: bytes, ikm: bytes) -> bytes:
    return _hmac(salt, b"HPKE-v1" + suite + label + ikm)


def _labeled_expand(suite: bytes, prk: bytes, label: bytes, info: bytes, length: int) -> bytes:
    labeled = struct.pack(">H", length) + b"HPKE-v1" + suite + label + info
    out, t, i = b"", b"", 1
    while len(out) < length:
        t = _hmac(prk, t + labeled + bytes([i]))
        out += t
        i += 1
    return out[:length]


def public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def private_key(scalar: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1())


def public_key(point: bytes) -> ec.EllipticCurvePublicKey:
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)


class Sealer:
    """One HPKE sender context: `enc` for the batch, then `seal` per record. `ephemeral`
    is for test vectors only; normally a fresh key is generated."""

    def __init__(
        self,
        recipient: ec.EllipticCurvePublicKey,
        info: bytes = INFO,
        aead_id: int = AES_256_GCM,
        ephemeral: ec.EllipticCurvePrivateKey | None = None,
    ) -> None:
        sk_e = ephemeral or ec.generate_private_key(ec.SECP256R1())
        self.enc = public_bytes(sk_e.public_key())
        dh = sk_e.exchange(ec.ECDH(), recipient)
        kem_suite = b"KEM" + struct.pack(">H", KEM_ID)
        eae_prk = _labeled_extract(kem_suite, b"", b"eae_prk", dh)
        shared = _labeled_expand(kem_suite, eae_prk, b"shared_secret", self.enc + public_bytes(recipient), 32)
        suite = b"HPKE" + struct.pack(">HHH", KEM_ID, KDF_ID, aead_id)
        context = b"\x00" + _labeled_extract(suite, b"", b"psk_id_hash", b"") + _labeled_extract(suite, b"", b"info_hash", info)
        secret = _labeled_extract(suite, shared, b"secret", b"")
        self.key = _labeled_expand(suite, secret, b"key", context, _NK[aead_id])
        self.base_nonce = _labeled_expand(suite, secret, b"base_nonce", context, NN)
        self._aead = AESGCM(self.key)
        self.seq = 0

    def nonce(self, seq: int) -> bytes:
        return (int.from_bytes(self.base_nonce, "big") ^ seq).to_bytes(NN, "big")

    def seal(self, plaintext: bytes, aad: bytes = b"") -> tuple[int, bytes]:
        """The next sequence number and the ciphertext sealed at it."""
        seq = self.seq
        self.seq += 1
        return seq, self._aead.encrypt(self.nonce(seq), plaintext, aad)
