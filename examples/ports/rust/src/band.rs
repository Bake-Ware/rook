//! The band transport subset a Rook worker needs (spec "Transport"): band id
//! and key from the PSK, ChaCha20-Poly1305 CHANNEL frames with empty AAD, the
//! fragmentation envelope, and a UDP link to the relay.

use chacha20poly1305::aead::{Aead, KeyInit, Payload};
use chacha20poly1305::{ChaCha20Poly1305, Nonce};
use hkdf::Hkdf;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::net::UdpSocket;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

pub const HEADER_SIZE: usize = 27;
pub const TAG_SIZE: usize = 16;
pub const CHANNEL: u8 = 0x02;
pub const FRAG_HEADER: usize = 21;
pub const MAX_CHUNK: usize = 1003;
pub const KEEPALIVE: &[u8] = &[0x00];

pub fn band_id(psk: &str) -> [u8; 16] {
    let d = Sha256::digest(psk.as_bytes());
    let mut out = [0u8; 16];
    out.copy_from_slice(&d[..16]);
    out
}

pub fn band_key(psk: &str) -> [u8; 32] {
    let hk = Hkdf::<Sha256>::new(Some(b"telesthete-v1"), psk.as_bytes());
    let mut okm = [0u8; 32];
    hk.expand(b"encryption-chacha20-poly1305", &mut okm).expect("32 bytes is a valid length");
    okm
}

fn nonce(seq: u64) -> [u8; 12] {
    let mut n = [0u8; 12];
    n[4..].copy_from_slice(&seq.to_be_bytes());
    n
}

/// AEAD seal with empty associated data (the Rook profile): ciphertext || tag.
pub fn seal(key: &[u8; 32], seq: u64, plaintext: &[u8]) -> Vec<u8> {
    let c = ChaCha20Poly1305::new(key.into());
    c.encrypt(Nonce::from_slice(&nonce(seq)), Payload { msg: plaintext, aad: b"" })
        .expect("encryption cannot fail")
}

pub fn open(key: &[u8; 32], seq: u64, sealed: &[u8]) -> Option<Vec<u8>> {
    let c = ChaCha20Poly1305::new(key.into());
    c.decrypt(Nonce::from_slice(&nonce(seq)), Payload { msg: sealed, aad: b"" }).ok()
}

pub fn pack_frame(band: &[u8; 16], seq: u64, sealed: &[u8]) -> Vec<u8> {
    let mut f = Vec::with_capacity(HEADER_SIZE + sealed.len());
    f.extend_from_slice(band);
    f.push(CHANNEL);
    f.extend_from_slice(&0u16.to_be_bytes());
    f.extend_from_slice(&seq.to_be_bytes());
    f.extend_from_slice(sealed);
    f
}

pub struct Frame<'a> {
    pub band: &'a [u8],
    pub kind: u8,
    pub channel: u16,
    pub seq: u64,
    pub sealed: &'a [u8],
}

pub fn unpack_frame(d: &[u8]) -> Option<Frame<'_>> {
    if d.len() < HEADER_SIZE + TAG_SIZE {
        return None;
    }
    Some(Frame {
        band: &d[..16],
        kind: d[16],
        channel: u16::from_be_bytes([d[17], d[18]]),
        seq: u64::from_be_bytes(d[19..27].try_into().ok()?),
        sealed: &d[HEADER_SIZE..],
    })
}

pub fn fragment(payload: &[u8], fid: [u8; 16], size: usize) -> Vec<Vec<u8>> {
    let pieces: Vec<&[u8]> = if payload.is_empty() { vec![&[][..]] } else { payload.chunks(size).collect() };
    assert!(pieces.len() <= 0xffff, "payload too large");
    let total = pieces.len() as u16;
    pieces
        .iter()
        .enumerate()
        .map(|(i, p)| {
            let mut c = Vec::with_capacity(FRAG_HEADER + p.len());
            c.push(1);
            c.extend_from_slice(&fid);
            c.extend_from_slice(&(i as u16).to_be_bytes());
            c.extend_from_slice(&total.to_be_bytes());
            c.extend_from_slice(p);
            c
        })
        .collect()
}

pub struct Chunk<'a> {
    pub fid: [u8; 16],
    pub seq: u16,
    pub total: u16,
    pub data: &'a [u8],
}

pub fn parse_chunk(c: &[u8]) -> Option<Chunk<'_>> {
    if c.len() < FRAG_HEADER || c[0] != 1 {
        return None;
    }
    let seq = u16::from_be_bytes([c[17], c[18]]);
    let total = u16::from_be_bytes([c[19], c[20]]);
    if total == 0 || seq >= total {
        return None;
    }
    Some(Chunk { fid: c[1..17].try_into().ok()?, seq, total, data: &c[FRAG_HEADER..] })
}

struct Partial {
    total: u16,
    parts: HashMap<u16, Vec<u8>>,
    first: Instant,
}

/// Collects fragments into messages; bounded (256) and time-limited (30 s).
#[derive(Default)]
pub struct Reassembler {
    bufs: HashMap<[u8; 16], Partial>,
}

impl Reassembler {
    pub fn feed(&mut self, chunk: &[u8]) -> Option<Vec<u8>> {
        let c = parse_chunk(chunk)?;
        let now = Instant::now();
        self.bufs.retain(|_, p| now.duration_since(p.first) < Duration::from_secs(30));
        let restart = match self.bufs.get(&c.fid) {
            None => {
                if self.bufs.len() >= 256 {
                    if let Some(old) = self.bufs.iter().min_by_key(|(_, p)| p.first).map(|(k, _)| *k) {
                        self.bufs.remove(&old);
                    }
                }
                true
            }
            Some(p) => p.total != c.total,
        };
        if restart {
            self.bufs.insert(c.fid, Partial { total: c.total, parts: HashMap::new(), first: now });
        }
        let p = self.bufs.get_mut(&c.fid)?;
        if p.parts.contains_key(&c.seq) {
            return None;
        }
        p.parts.insert(c.seq, c.data.to_vec());
        if p.parts.len() < p.total as usize {
            return None;
        }
        let p = self.bufs.remove(&c.fid)?;
        Some((0..p.total).flat_map(|i| p.parts[&i].clone()).collect())
    }
}

pub fn random<const N: usize>() -> [u8; N] {
    let mut b = [0u8; N];
    getrandom::getrandom(&mut b).expect("OS randomness");
    b
}

/// One band membership over UDP through the relay.
pub struct Link {
    pub band: [u8; 16],
    key: [u8; 32],
    sock: UdpSocket,
    relay: String,
    seq: Mutex<u64>, // held for a whole message so fragments stay contiguous
}

impl Link {
    pub fn connect(psk: &str, relay: &str) -> std::io::Result<Arc<Link>> {
        let sock = UdpSocket::bind("0.0.0.0:0")?;
        // CSPRNG-seeded 63-bit start: the key is band-wide, so the sequence
        // (the nonce) must not repeat across peers or restarts.
        let start = u64::from_be_bytes(random::<8>()) >> 1;
        let link = Arc::new(Link { band: band_id(psk), key: band_key(psk), sock, relay: relay.to_string(),
                                   seq: Mutex::new(start) });
        link.send(KEEPALIVE)?; // registers our address with the relay
        Ok(link)
    }

    pub fn send(&self, payload: &[u8]) -> std::io::Result<()> {
        let mut seq = self.seq.lock().unwrap();
        for chunk in fragment(payload, random::<16>(), MAX_CHUNK) {
            let s = *seq;
            *seq = seq.wrapping_add(1);
            self.sock.send_to(&pack_frame(&self.band, s, &seal(&self.key, s, &chunk)), &self.relay)?;
        }
        Ok(())
    }

    /// Blocking receive loop: calls `on_message` with each reassembled message.
    pub fn recv_loop(&self, mut on_message: impl FnMut(Vec<u8>)) {
        let mut reasm = Reassembler::default();
        let mut buf = vec![0u8; 65535];
        loop {
            let Ok((n, _)) = self.sock.recv_from(&mut buf) else { continue };
            let Some(f) = unpack_frame(&buf[..n]) else { continue };
            if f.kind != CHANNEL || f.band != self.band {
                continue;
            }
            let Some(chunk) = open(&self.key, f.seq, f.sealed) else { continue };
            if let Some(msg) = reasm.feed(&chunk) {
                if msg != KEEPALIVE {
                    on_message(msg);
                }
            }
        }
    }
}
