"""HPKE (RFC 9180) base mode, receiving side, for DHKEM(P-256, HKDF-SHA256), HKDF-SHA256
and AES-GCM. Written apart from the SDKs' sending side, and checked against the CFRG
vectors in `vectors/hpke.json`. Opens any sequence number directly, which the wire format
needs (each record carries its own)."""

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


def _extract(salt: bytes, ikm: bytes) -> bytes:
    h = hmac.HMAC(salt if salt else bytes(32), hashes.SHA256())
    h.update(ikm)
    return h.finalize()


def _expand(prk: bytes, info: bytes, length: int) -> bytes:
    out, block, counter = b"", b"", 1
    while len(out) < length:
        h = hmac.HMAC(prk, hashes.SHA256())
        h.update(block + info + bytes([counter]))
        block = h.finalize()
        out += block
        counter += 1
    return out[:length]


def _lx(suite: bytes, salt: bytes, label: bytes, ikm: bytes) -> bytes:
    return _extract(salt, b"HPKE-v1" + suite + label + ikm)


def _le(suite: bytes, prk: bytes, label: bytes, info: bytes, length: int) -> bytes:
    return _expand(prk, struct.pack(">H", length) + b"HPKE-v1" + suite + label + info, length)


class Receiver:
    """The receiver's context for one `enc`."""

    def __init__(self, sk_r: ec.EllipticCurvePrivateKey, enc: bytes, info: bytes, aead_id: int = AES_256_GCM) -> None:
        pk_e = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), enc)
        dh = sk_r.exchange(ec.ECDH(), pk_e)
        pk_rm = sk_r.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        kem = b"KEM" + struct.pack(">H", KEM_ID)
        shared = _le(kem, _lx(kem, b"", b"eae_prk", dh), b"shared_secret", enc + pk_rm, 32)
        suite = b"HPKE" + struct.pack(">HHH", KEM_ID, KDF_ID, aead_id)
        context = b"\x00" + _lx(suite, b"", b"psk_id_hash", b"") + _lx(suite, b"", b"info_hash", info)
        secret = _lx(suite, shared, b"secret", b"")
        self.key = _le(suite, secret, b"key", context, _NK[aead_id])
        self.base_nonce = _le(suite, secret, b"base_nonce", context, NN)
        self._aead = AESGCM(self.key)

    def open(self, seq: int, ciphertext: bytes, aad: bytes = b"") -> bytes:
        nonce = (int.from_bytes(self.base_nonce, "big") ^ seq).to_bytes(NN, "big")
        return self._aead.decrypt(nonce, ciphertext, aad)
