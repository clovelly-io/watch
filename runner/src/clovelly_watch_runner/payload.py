"""The repository's payload key pair and opening sealed records.

The public half goes to the service as CLOVELLY_WATCH_KEY (`cwk1_` and base64url); the
private half (`cwsk1_` and base64url of the scalar) stays with the runner.
"""

from __future__ import annotations

import base64
import json
import struct
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .hpke import Receiver

INFO = b"clovelly-watch payload v1"


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class PrivateKey:
    """A repository's payload private key."""

    def __init__(self, key: ec.EllipticCurvePrivateKey) -> None:
        self.key = key

    @classmethod
    def parse(cls, text: str) -> PrivateKey:
        text = text.strip()
        if not text.startswith("cwsk1_"):
            raise ValueError("a payload private key starts with cwsk1_")
        scalar = _unb64(text[6:])
        if len(scalar) != 32:
            raise ValueError("a payload private key is a 32-byte scalar")
        return cls(ec.derive_private_key(int.from_bytes(scalar, "big"), ec.SECP256R1()))

    def text(self) -> str:
        return "cwsk1_" + _b64(self.key.private_numbers().private_value.to_bytes(32, "big"))

    def public_text(self) -> str:
        """CLOVELLY_WATCH_KEY for the service."""
        return "cwk1_" + _b64(self.key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint))


def generate() -> PrivateKey:
    return PrivateKey(ec.generate_private_key(ec.SECP256R1()))


class Opener:
    """Opens records sealed under one batch's `enc`."""

    def __init__(self, key: PrivateKey, enc: bytes) -> None:
        self._receiver = Receiver(key.key, enc, INFO)

    def open(self, sealed: bytes) -> dict[str, Any]:
        """A `clovelly.sealed` value (sequence number, then ciphertext) to its payload."""
        if len(sealed) < 4 + 16:
            raise ValueError("a sealed payload is a sequence number and a ciphertext")
        (seq,) = struct.unpack(">I", sealed[:4])
        return json.loads(self._receiver.open(seq, sealed[4:]))


#: The HPKE ``info`` of a hosted organization's GitHub token, as Watch seals it (crates/watch,
#: ``hosted::INFO``).
HOSTED_INFO = b"clovelly-watch hosted token v1"


def open_hosted(key: PrivateKey, sealed: str) -> bytes:
    """Opens a ``cwh1_`` sealed GitHub token (Watch's hosted mode): the 65-byte encapsulated key,
    then the AES-256-GCM ciphertext of the context's first message."""
    if not sealed.startswith("cwh1_"):
        raise ValueError("a sealed hosted token starts with cwh1_")
    raw = _unb64(sealed[5:])
    if len(raw) < 65 + 16:
        raise ValueError("a sealed hosted token is an encapsulated key and a ciphertext")
    return Receiver(key.key, raw[:65], HOSTED_INFO).open(0, raw[65:])


def open_record(key: PrivateKey, enc: bytes, sealed: bytes) -> dict[str, Any]:
    return Opener(key, enc).open(sealed)


#: A record's opened payload handed into a session is cut to this many bytes of JSON.
MAX_OPENED = 64 * 1024
#: The resource attribute carrying the commit a record's service was running.
RELEASE = "vcs.ref.head.revision"


def open_found(key: PrivateKey, record: dict[str, Any]) -> dict[str, Any]:
    """A record as Watch's lookups return it (cleartext fields, hex ``sealed`` and ``enc``)
    with its payload opened. What comes out is untrusted: anyone who can send the service a
    request chose it. A payload that does not open is reported as such, never guessed."""
    out = {k: record.get(k) for k in ("signal", "time_unix_nano", "trace_id", "span_id", "status", "attributes", "resource")}
    sealed, enc = record.get("sealed"), record.get("enc")
    out["payload"], out["payload_error"] = None, None
    if not sealed or not enc:
        out["payload_error"] = "no sealed payload"
        return out
    try:
        opened = open_record(key, bytes.fromhex(enc), bytes.fromhex(sealed))
    except Exception as e:  # noqa: BLE001 (a payload that does not open is reported, whatever the reason)
        out["payload_error"] = f"does not open: {type(e).__name__}"
        return out
    text = json.dumps(opened)
    if len(text) > MAX_OPENED:
        out["payload_error"] = f"cut at {MAX_OPENED} bytes"
        out["payload"] = {"cut": text[:MAX_OPENED]}
    else:
        out["payload"] = opened
    return out
