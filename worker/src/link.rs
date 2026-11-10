//! One band over a Telesthete relay (UDP), the carrier of core v2 section 2.1:
//! band id and key from the PSK, CHANNEL frames sealed with ChaCha20-Poly1305
//! and EMPTY associated data, the 21-byte fragment envelope, a keepalive
//! payload `0x00`. The crate's own encoder always binds the 3-byte spec AAD,
//! so frames are sealed and opened here.

use std::io;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use chacha20poly1305::aead::{Aead, KeyInit, Payload};
use chacha20poly1305::{ChaCha20Poly1305, Nonce};
use telesthete::framing::{ChannelType, Header, HEADER_LEN, MIN_PACKET_LEN};
use telesthete::wire::fragment::{fragment, Reassembler, MAX_CHUNK_PAYLOAD};
use telesthete::{derive_band_id, derive_key, BandId};
use tokio::net::UdpSocket;

pub const DEFAULT_RELAY_PORT: u16 = 7474;
pub const KEEPALIVE: &[u8] = b"\x00";
pub const KEEPALIVE_EVERY: Duration = Duration::from_secs(5);
/// Frames sent back to back before a pause: 256 frames per 12.8 ms is the
/// Python link's 20,000 frames/s, which the relay's queue keeps up with.
const BURST: usize = 256;
const PAUSE: Duration = Duration::from_micros(12_800);

pub struct Link {
    sock: UdpSocket,
    relay: SocketAddr,
    band_id: BandId,
    aead: ChaCha20Poly1305,
    seq: AtomicU64,
}

pub fn band_id_hex(psk: &str) -> String {
    hex(&derive_band_id(psk.as_bytes()))
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

pub fn random<const N: usize>() -> [u8; N] {
    let mut out = [0u8; N];
    getrandom::getrandom(&mut out).expect("the OS random source");
    out
}

impl Link {
    /// `relay` is `host:port`, `host` (port 7474) or `[v6]:port`.
    pub async fn connect(psk: &str, relay: &str) -> io::Result<Link> {
        let (host, port) = match relay.rsplit_once(':') {
            Some((h, p)) if !h.is_empty() => (
                h.trim_matches(['[', ']']),
                p.parse().map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, format!("bad relay port: {relay}")))?,
            ),
            _ => (relay, DEFAULT_RELAY_PORT),
        };
        let relay = tokio::net::lookup_host((host, port))
            .await?
            .next()
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, format!("relay {host} not found")))?;
        let sock = UdpSocket::bind(if relay.is_ipv4() { "0.0.0.0:0" } else { "[::]:0" }).await?;
        Ok(Link::new(sock, relay, psk))
    }

    fn new(sock: UdpSocket, relay: SocketAddr, psk: &str) -> Link {
        let key = derive_key(psk.as_bytes());
        // A CSPRNG start (63 bits) then +1 per frame: the band key is shared,
        // so peers must never reuse a (key, sequence) nonce.
        let start = u64::from_be_bytes(random::<8>()) >> 1;
        Link {
            sock,
            relay,
            band_id: derive_band_id(psk.as_bytes()),
            aead: ChaCha20Poly1305::new_from_slice(&key).expect("32-byte key"),
            seq: AtomicU64::new(start),
        }
    }

    pub fn band_id(&self) -> BandId {
        self.band_id
    }

    /// One payload as sealed CHANNEL frames on route 0 (to everyone).
    pub fn frames(&self, payload: &[u8]) -> Vec<Vec<u8>> {
        fragment(payload, MAX_CHUNK_PAYLOAD, &random::<16>())
            .iter()
            .map(|chunk| self.seal(self.seq.fetch_add(1, Ordering::Relaxed), chunk))
            .collect()
    }

    fn seal(&self, seq: u64, chunk: &[u8]) -> Vec<u8> {
        let mut out = vec![0u8; HEADER_LEN];
        Header { band_id: self.band_id, channel_type: ChannelType::Channel, channel_id: 0, sequence: seq }.write(&mut out);
        let sealed = self
            .aead
            .encrypt(&nonce(seq), Payload { msg: chunk, aad: b"" })
            .expect("sealing cannot fail");
        out.extend_from_slice(&sealed);
        out
    }

    /// The chunk inside one frame of this band (CHANNEL or ADDRESSED), or None.
    pub fn open(&self, frame: &[u8]) -> Option<Vec<u8>> {
        if frame.len() < MIN_PACKET_LEN {
            return None;
        }
        let h = Header::parse(frame).ok()?;
        if h.band_id != self.band_id || !matches!(h.channel_type, ChannelType::Channel | ChannelType::Addressed) {
            return None;
        }
        self.aead.decrypt(&nonce(h.sequence), Payload { msg: &frame[HEADER_LEN..], aad: b"" }).ok()
    }

    pub async fn send(&self, payload: &[u8]) -> io::Result<()> {
        for (i, frame) in self.frames(payload).iter().enumerate() {
            if i > 0 && i % BURST == 0 {
                tokio::time::sleep(PAUSE).await;
            }
            self.sock.send_to(frame, self.relay).await?;
        }
        Ok(())
    }

    /// The next whole payload from the band (keepalives and partial messages skipped).
    pub async fn recv(&self, reassembler: &mut Reassembler) -> io::Result<Vec<u8>> {
        let mut buf = vec![0u8; 65536];
        loop {
            let n = match self.sock.recv_from(&mut buf).await {
                Ok((n, _)) => n,
                // Windows reports an earlier send's ICMP "port unreachable"
                // here (a relay that restarted): not this socket's end.
                Err(e) if matches!(e.kind(), std::io::ErrorKind::ConnectionReset | std::io::ErrorKind::ConnectionRefused) => continue,
                Err(e) => return Err(e),
            };
            if let Some(chunk) = self.open(&buf[..n]) {
                if let Some(payload) = reassembler.feed(&chunk) {
                    if payload != KEEPALIVE {
                        return Ok(payload);
                    }
                }
            }
        }
    }
}

pub fn reassembler() -> Reassembler {
    Reassembler::for_chunk_size(MAX_CHUNK_PAYLOAD)
}

fn nonce(seq: u64) -> Nonce {
    let mut n = [0u8; 12];
    n[4..].copy_from_slice(&seq.to_be_bytes());
    *Nonce::from_slice(&n)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn unhex(s: &str) -> Vec<u8> {
        (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
    }

    /// conformance/vectors/frames.json: the whole pipeline, byte for byte.
    #[tokio::test]
    async fn frames_match_the_conformance_vectors() {
        let v: Value = serde_json::from_str(include_str!("../../conformance/vectors/frames.json")).unwrap();
        for case in v["cases"].as_array().unwrap() {
            let sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();
            let link = Link::new(sock, "127.0.0.1:9".parse().unwrap(), case["psk"].as_str().unwrap());
            let fid: [u8; 16] = unhex(case["fragment_id"].as_str().unwrap()).try_into().unwrap();
            let first = case["first_sequence"].as_u64().unwrap();
            let message = unhex(case["message"].as_str().unwrap());
            let got: Vec<String> = fragment(&message, MAX_CHUNK_PAYLOAD, &fid)
                .iter()
                .enumerate()
                .map(|(i, c)| hex(&link.seal(first + i as u64, c)))
                .collect();
            let want: Vec<String> = case["frames"].as_array().unwrap().iter().map(|f| f.as_str().unwrap().to_string()).collect();
            assert_eq!(got, want, "{}", case["label"]);
            let mut r = reassembler();
            let back = want.iter().filter_map(|f| r.feed(&link.open(&unhex(f)).unwrap())).next().unwrap();
            assert_eq!(back, message);
        }
    }

    #[test]
    fn band_id_matches_the_vectors() {
        let v: Value = serde_json::from_str(include_str!("../../conformance/vectors/band_crypto.json")).unwrap();
        for case in v["cases"].as_array().unwrap() {
            assert_eq!(band_id_hex(case["psk"].as_str().unwrap()), case["band_id"].as_str().unwrap());
        }
    }
}
