/**
 * HPKE (RFC 9180) base mode, sender side, for DHKEM(P-256, HKDF-SHA256), HKDF-SHA256 and
 * AES-GCM (AES-256-GCM is Watch's; AES-128-GCM is here for RFC 9180's own vectors). Built
 * on node:crypto and checked against the CFRG vectors in `vectors/hpke.json`.
 */
import { createCipheriv, createECDH, createHmac } from "node:crypto";

const KEM_ID = 0x0010;
const KDF_ID = 0x0001;
export const AES_128_GCM = 0x0001;
export const AES_256_GCM = 0x0002;
const NN = 12;
export const INFO = Buffer.from("clovelly-watch payload v1");

type AeadId = typeof AES_128_GCM | typeof AES_256_GCM;
const NK: Record<AeadId, number> = { [AES_128_GCM]: 16, [AES_256_GCM]: 32 };

const u16 = (n: number): Buffer => Buffer.from([n >> 8, n & 0xff]);

function hmac(key: Buffer, data: Buffer): Buffer {
  return createHmac("sha256", key.length > 0 ? key : Buffer.alloc(32)).update(data).digest();
}

function labeledExtract(suite: Buffer, salt: Buffer, label: string, ikm: Buffer): Buffer {
  return hmac(salt, Buffer.concat([Buffer.from("HPKE-v1"), suite, Buffer.from(label), ikm]));
}

function labeledExpand(suite: Buffer, prk: Buffer, label: string, info: Buffer, length: number): Buffer {
  const labeled = Buffer.concat([u16(length), Buffer.from("HPKE-v1"), suite, Buffer.from(label), info]);
  const blocks: Buffer[] = [];
  let t: Buffer = Buffer.alloc(0);
  for (let i = 1; blocks.length * 32 < length; i++) {
    t = hmac(prk, Buffer.concat([t, labeled, Buffer.from([i])]));
    blocks.push(t);
  }
  return Buffer.concat(blocks).subarray(0, length);
}

/** One sender context: `enc` for the batch, then `seal` per record. `ephemeral` (a P-256
 * scalar) is for test vectors only. */
export class Sealer {
  readonly enc: Buffer;
  readonly key: Buffer;
  readonly baseNonce: Buffer;
  seq = 0;
  readonly #cipher: "aes-128-gcm" | "aes-256-gcm";

  constructor(recipient: Buffer, options: { info?: Buffer; aeadId?: AeadId; ephemeral?: Buffer } = {}) {
    const { info = INFO, aeadId = AES_256_GCM, ephemeral } = options;
    const ecdh = createECDH("prime256v1");
    if (ephemeral) ecdh.setPrivateKey(ephemeral);
    else ecdh.generateKeys();
    this.enc = ecdh.getPublicKey();
    const dh = ecdh.computeSecret(recipient);
    const kem = Buffer.concat([Buffer.from("KEM"), u16(KEM_ID)]);
    const eaePrk = labeledExtract(kem, Buffer.alloc(0), "eae_prk", dh);
    const shared = labeledExpand(kem, eaePrk, "shared_secret", Buffer.concat([this.enc, recipient]), 32);
    const suite = Buffer.concat([Buffer.from("HPKE"), u16(KEM_ID), u16(KDF_ID), u16(aeadId)]);
    const empty = Buffer.alloc(0);
    const context = Buffer.concat([
      Buffer.from([0]),
      labeledExtract(suite, empty, "psk_id_hash", empty),
      labeledExtract(suite, empty, "info_hash", info),
    ]);
    const secret = labeledExtract(suite, shared, "secret", empty);
    this.key = labeledExpand(suite, secret, "key", context, NK[aeadId]);
    this.baseNonce = labeledExpand(suite, secret, "base_nonce", context, NN);
    this.#cipher = aeadId === AES_128_GCM ? "aes-128-gcm" : "aes-256-gcm";
  }

  nonce(seq: number): Buffer {
    const n = Buffer.from(this.baseNonce);
    let s = BigInt(seq);
    for (let i = NN - 1; i >= 0 && s > 0n; i--, s >>= 8n) n[i]! ^= Number(s & 0xffn);
    return n;
  }

  /** The next sequence number and the ciphertext (with its tag) sealed at it. */
  seal(plaintext: Buffer, aad: Buffer = Buffer.alloc(0)): { seq: number; ciphertext: Buffer } {
    const seq = this.seq++;
    const c = createCipheriv(this.#cipher, this.key, this.nonce(seq));
    c.setAAD(aad);
    return { seq, ciphertext: Buffer.concat([c.update(plaintext), c.final(), c.getAuthTag()]) };
  }
}
