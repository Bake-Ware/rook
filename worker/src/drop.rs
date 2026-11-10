//! Large replies over Drop (core v1 14.2), the sending side. A reply over
//! 32 KiB to a request whose `accept` lists `drop` goes out as a stub
//! `{id, from, ok, drop: {id, size, sha256}}`; the caller pulls the bytes
//! with Drop REQUESTs (telesthete SPEC 8) and this worker serves the chunks.
//! Drop packets travel as ordinary band payloads: a DROP packet header,
//! the Drop frame in clear (the band frames around it are the encryption)
//! and 16 zero bytes where a tag would be, as rook/core2/transport/drop.py.
//! The receiving side (`pull`) takes a core release the hub offers.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use telesthete::framing::{ChannelType, Header, HEADER_LEN};
use telesthete::{DropReceiverCore, DropSenderCore};
use tokio::sync::mpsc;

use crate::link::Link;

pub const THRESHOLD: usize = 32 * 1024;
/// How long a reply nobody finished pulling is served.
const TTL: Duration = Duration::from_secs(120);
const BASE: u16 = 0x8000;
const TAG: [u8; 16] = [0; 16];
/// Packets sent back to back before a pause (each is two band frames).
const BURST: usize = 128;
const PAUSE: Duration = Duration::from_micros(12_800);

pub struct Replies {
    serving: Mutex<HashMap<u16, (DropSenderCore, Instant)>>,
    seq: AtomicU64,
    /// One request's chunks go out in order, one request after another:
    /// the caller's Drop receiver refuses a packet older than the last.
    order: tokio::sync::Mutex<()>,
    /// Pulls in progress: drop id -> their incoming frames.
    pulls: Mutex<HashMap<u16, mpsc::UnboundedSender<Vec<u8>>>>,
}

/// The drop id Python's `reply_drop_id` gives the reply to `msg_id` from `worker_id`.
fn reply_id(msg_id: &str, worker_id: &str) -> u16 {
    let h = Sha256::digest(format!("{msg_id}\n{worker_id}").as_bytes());
    BASE | (u16::from_be_bytes([h[0], h[1]]) & 0x7FFF)
}

impl Default for Replies {
    fn default() -> Self {
        Replies {
            serving: Mutex::new(HashMap::new()),
            seq: AtomicU64::new(u64::from_be_bytes(crate::link::random::<8>()) >> 1),
            order: tokio::sync::Mutex::new(()),
            pulls: Mutex::new(HashMap::new()),
        }
    }
}

impl Replies {
    /// Serve `blob` as the reply to `msg_id`; the descriptor for the stub.
    pub fn serve(&self, msg_id: &str, worker_id: &str, blob: Vec<u8>) -> Value {
        let mut serving = self.serving.lock().unwrap();
        serving.retain(|_, (_, until)| *until > Instant::now());
        let mut did = reply_id(msg_id, worker_id);
        while serving.contains_key(&did) {
            did = BASE | (did.wrapping_add(1) & 0x7FFF);
        }
        let size = blob.len();
        let sender = DropSenderCore::new("reply", blob);
        let desc = json!({"id": did, "size": size, "sha256": sender.sha256});
        serving.insert(did, (sender, Instant::now() + TTL));
        desc
    }

    /// A Drop packet for one of the replies served here: answer it. False
    /// when `payload` is not a Drop packet of this band.
    pub async fn feed(&self, link: &Link, payload: &[u8]) -> bool {
        let band = link.band_id();
        if payload.len() < HEADER_LEN + TAG.len() || payload[..16] != band || payload[16] != ChannelType::Drop as u8 {
            return false;
        }
        let did = u16::from_be_bytes([payload[17], payload[18]]);
        let frame = &payload[HEADER_LEN..payload.len() - TAG.len()];
        if let Some(tx) = self.pulls.lock().unwrap().get(&did) {
            let _ = tx.send(frame.to_vec());
            return true;
        }
        let out = {
            let mut serving = self.serving.lock().unwrap();
            let Some((sender, _)) = serving.get_mut(&did) else { return true };
            let (out, done) = sender.handle_frame(ANY, frame);
            if done == Some(true) {
                serving.remove(&did); // the caller has it, verified
            }
            out
        };
        self.send(link, did, &out).await;
        true
    }

    async fn send(&self, link: &Link, did: u16, frames: &[Vec<u8>]) {
        let _ordered = self.order.lock().await;
        for (i, frame) in frames.iter().enumerate() {
            if i > 0 && i % BURST == 0 {
                tokio::time::sleep(PAUSE).await;
            }
            let mut packet = vec![0u8; HEADER_LEN];
            let sequence = self.seq.fetch_add(1, Ordering::Relaxed);
            Header { band_id: link.band_id(), channel_type: ChannelType::Drop, channel_id: did, sequence }.write(&mut packet);
            packet.extend_from_slice(frame);
            packet.extend_from_slice(&TAG);
            if let Err(e) = link.send(&packet).await {
                eprintln!("rook-worker: drop packet not sent: {e}");
                break;
            }
        }
    }

    /// Pull a blob the hub offered (`desc` = {id, size, sha256}): REQUESTs
    /// for the chunks still missing until all arrived and the hash matches.
    pub async fn pull(&self, link: &Link, desc: &Value, timeout: Duration) -> Result<Vec<u8>, String> {
        let (Some(did), Some(size), Some(sha)) = (desc["id"].as_u64(), desc["size"].as_u64(), desc["sha256"].as_str()) else {
            return Err("malformed drop descriptor".into());
        };
        let did = did as u16;
        let offer = json!({"name": "reply", "size": size, "chunk_size": 1024,
                           "total_chunks": size.div_ceil(1024).max(1), "sha256": sha});
        let mut first = vec![1u8]; // an OFFER frame, as if the hub had sent one
        first.extend_from_slice(offer.to_string().as_bytes());
        let (tx, mut rx) = mpsc::unbounded_channel();
        self.pulls.lock().unwrap().insert(did, tx);
        let mut core = DropReceiverCore::new(HashMap::new());
        let deadline = tokio::time::Instant::now() + timeout;
        let mut out = core.handle_frame(ANY, &first, Instant::now());
        let got = loop {
            self.send(link, did, &out).await;
            if let Some((data, ok)) = core.take_completion() {
                break if ok { Ok(data) } else { Err("hash mismatch".to_string()) };
            }
            out = tokio::select! {
                frame = rx.recv() => match frame {
                    Some(frame) => core.handle_frame(ANY, &frame, Instant::now()),
                    None => break Err("stopped".into()),
                },
                _ = tokio::time::sleep(Duration::from_millis(250)) => core.tick(Instant::now()),
                _ = tokio::time::sleep_until(deadline) => break Err(format!("not pulled within {}s", timeout.as_secs())),
            };
        };
        self.pulls.lock().unwrap().remove(&did);
        got
    }
}

const ANY: SocketAddr = SocketAddr::new(std::net::IpAddr::V4(std::net::Ipv4Addr::UNSPECIFIED), 0);

#[cfg(test)]
mod tests {
    use super::*;

    /// Same ids as rook/core2/transport/drop.py `reply_drop_id`.
    #[test]
    fn reply_ids_match_python() {
        assert_eq!(reply_id("abc", "w1"), 0x9fc1); // python3: 0x8000 | sha256(b"abc\nw1")[:2] & 0x7fff
    }
}
