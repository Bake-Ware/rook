// The band transport subset a Rook worker needs (spec "Transport"):
// PSK-derived band id and key, ChaCha20-Poly1305 CHANNEL frames, the
// fragmentation envelope, and a UDP link to the relay.
import { createCipheriv, createDecipheriv, createHash, hkdfSync, randomBytes } from "node:crypto";
import dgram from "node:dgram";

export const HEADER_SIZE = 27;          // band_id 16 | type 1 | channel 2 | seq 8
export const TAG_SIZE = 16;
export const CHANNEL = 0x02;
export const FRAG_HEADER = 21;          // version 1 | fragment_id 16 | seq 2 | total 2
export const MAX_CHUNK = 1003;          // 1024 channel bytes - fragment header
export const KEEPALIVE = Buffer.from([0x00]);

export function bandId(psk: string): Buffer {
  return createHash("sha256").update(psk, "utf8").digest().subarray(0, 16);
}

export function bandKey(psk: string): Buffer {
  return Buffer.from(hkdfSync("sha256", Buffer.from(psk, "utf8"), Buffer.from("telesthete-v1"),
    Buffer.from("encryption-chacha20-poly1305"), 32));
}

export function nonce(seq: bigint): Buffer {
  const n = Buffer.alloc(12);
  n.writeBigUInt64BE(seq, 4);
  return n;
}

/** AEAD seal with EMPTY associated data (the Rook profile). Returns ct || tag. */
export function seal(key: Buffer, seq: bigint, plaintext: Buffer): Buffer {
  const c = createCipheriv("chacha20-poly1305", key, nonce(seq), { authTagLength: TAG_SIZE });
  const ct = Buffer.concat([c.update(plaintext), c.final()]);
  return Buffer.concat([ct, c.getAuthTag()]);
}

export function open(key: Buffer, seq: bigint, sealed: Buffer): Buffer {
  if (sealed.length < TAG_SIZE) throw new Error("short ciphertext");
  const d = createDecipheriv("chacha20-poly1305", key, nonce(seq), { authTagLength: TAG_SIZE });
  d.setAuthTag(sealed.subarray(sealed.length - TAG_SIZE));
  return Buffer.concat([d.update(sealed.subarray(0, sealed.length - TAG_SIZE)), d.final()]);
}

export function packFrame(band: Buffer, seq: bigint, sealed: Buffer): Buffer {
  const h = Buffer.alloc(HEADER_SIZE);
  band.copy(h, 0);
  h.writeUInt8(CHANNEL, 16);
  h.writeUInt16BE(0, 17);
  h.writeBigUInt64BE(seq, 19);
  return Buffer.concat([h, sealed]);
}

export interface Frame { band: Buffer; type: number; channel: number; seq: bigint; sealed: Buffer }

export function unpackFrame(data: Buffer): Frame | null {
  if (data.length < HEADER_SIZE + TAG_SIZE) return null;
  return { band: data.subarray(0, 16), type: data.readUInt8(16), channel: data.readUInt16BE(17),
    seq: data.readBigUInt64BE(19), sealed: data.subarray(HEADER_SIZE) };
}

export function fragment(payload: Buffer, fid: Buffer = randomBytes(16), size = MAX_CHUNK): Buffer[] {
  const pieces: Buffer[] = [];
  for (let i = 0; i < payload.length; i += size) pieces.push(payload.subarray(i, i + size));
  if (pieces.length === 0) pieces.push(Buffer.alloc(0));
  if (pieces.length > 0xffff) throw new Error("payload too large");
  return pieces.map((p, i) => {
    const h = Buffer.alloc(FRAG_HEADER);
    h.writeUInt8(1, 0);
    fid.copy(h, 1);
    h.writeUInt16BE(i, 17);
    h.writeUInt16BE(pieces.length, 19);
    return Buffer.concat([h, p]);
  });
}

export interface Chunk { fid: string; seq: number; total: number; data: Buffer }

export function parseChunk(c: Buffer): Chunk | null {
  if (c.length < FRAG_HEADER || c.readUInt8(0) !== 1) return null;
  const seq = c.readUInt16BE(17), total = c.readUInt16BE(19);
  if (total === 0 || seq >= total) return null;
  return { fid: c.subarray(1, 17).toString("hex"), seq, total, data: c.subarray(FRAG_HEADER) };
}

/** Collects fragments into messages; bounded and time-limited like the reference. */
export class Reassembler {
  private bufs = new Map<string, { total: number; parts: Map<number, Buffer>; first: number }>();
  private timeoutMs: number;
  private limit: number;
  constructor(timeoutMs = 30_000, limit = 256) {
    this.timeoutMs = timeoutMs;
    this.limit = limit;
  }

  feed(chunk: Buffer): Buffer | null {
    const c = parseChunk(chunk);
    if (!c) return null;
    const now = Date.now();
    for (const [k, v] of this.bufs) if (now - v.first > this.timeoutMs) this.bufs.delete(k);
    let b = this.bufs.get(c.fid);
    if (!b || b.total !== c.total) {
      if (!b && this.bufs.size >= this.limit) {
        let oldest: string | null = null, t = Infinity;
        for (const [k, v] of this.bufs) if (v.first < t) { t = v.first; oldest = k; }
        if (oldest) this.bufs.delete(oldest);
      }
      b = { total: c.total, parts: new Map(), first: now };
      this.bufs.set(c.fid, b);
    }
    if (b.parts.has(c.seq)) return null;
    b.parts.set(c.seq, c.data);
    if (b.parts.size < b.total) return null;
    this.bufs.delete(c.fid);
    return Buffer.concat([...Array(b.total).keys()].map((i) => b!.parts.get(i)!));
  }
}

/** One band membership over UDP through the relay. */
export class BandLink {
  readonly band: Buffer;
  private key: Buffer;
  private seq: bigint;
  private sock = dgram.createSocket("udp4");
  private reasm = new Reassembler();
  private timer: NodeJS.Timeout | null = null;
  private queue: Promise<void> = Promise.resolve();
  private host: string;
  private port: number;
  private onMessage: (payload: Buffer) => void;
  private keepaliveMs: number;

  constructor(psk: string, host: string, port: number, onMessage: (payload: Buffer) => void,
              keepaliveMs = 20_000) {
    this.host = host;
    this.port = port;
    this.onMessage = onMessage;
    this.keepaliveMs = keepaliveMs;
    this.band = bandId(psk);
    this.key = bandKey(psk);
    // CSPRNG-seeded 63-bit start: the band key is shared, so the sequence
    // (the nonce) must never repeat across peers or restarts.
    this.seq = randomBytes(8).readBigUInt64BE(0) >> 1n;
  }

  async start(): Promise<void> {
    this.sock.on("message", (data) => this.receive(data));
    await new Promise<void>((resolve) => this.sock.bind(0, resolve));
    await this.send(KEEPALIVE); // registers our address with the relay
    this.timer = setInterval(() => { this.send(KEEPALIVE).catch(() => {}); }, this.keepaliveMs);
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.sock.close();
  }

  /** Fragment, seal and send one message; messages never interleave on the wire. */
  send(payload: Buffer): Promise<void> {
    const run = async () => {
      for (const chunk of fragment(payload)) {
        const seq = this.seq;
        this.seq = (this.seq + 1n) & 0xffffffffffffffffn;
        const frame = packFrame(this.band, seq, seal(this.key, seq, chunk));
        await new Promise<void>((resolve, reject) =>
          this.sock.send(frame, this.port, this.host, (e) => (e ? reject(e) : resolve())));
      }
    };
    this.queue = this.queue.then(run, run);
    return this.queue;
  }

  private receive(data: Buffer): void {
    const f = unpackFrame(data);
    if (!f || f.type !== CHANNEL || !f.band.equals(this.band)) return;
    let chunk: Buffer;
    try { chunk = open(this.key, f.seq, f.sealed); } catch { return; }
    const msg = this.reasm.feed(chunk);
    if (msg && !msg.equals(KEEPALIVE)) this.onMessage(msg);
  }
}
