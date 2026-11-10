//! net.open (TCP) and serial.open (worker/manifests/net.yaml, serial.yaml):
//! a connection or a port as a stream. What arrives becomes `bytes` events;
//! input `text` is sent; any signal (stream.close sends HUP) closes it.

use std::io::{ErrorKind, Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::time::Duration;

use serde_json::{json, Map, Value};

use super::{Args, Reply};
use crate::streams::{Body, Streams};

/// How often a quiet reader looks whether it was asked to stop.
const POLL: Duration = Duration::from_millis(200);
const CONNECT: Duration = Duration::from_secs(20);

pub fn tcp(a: Args<'_>, streams: &Arc<Streams>, participants: Vec<String>, mut attrs: Map<String, Value>) -> Reply {
    let host = a.need("host")?;
    let port = a.int("port", 0)?;
    let port = u16::try_from(port).map_err(|_| "port must be 1-65535".to_string())?;
    let addr = (host.as_str(), port)
        .to_socket_addrs()
        .map_err(|e| format!("{host}: {e}"))?
        .next()
        .ok_or(format!("{host}: no address"))?;
    let sock = TcpStream::connect_timeout(&addr, CONNECT).map_err(|e| format!("{host}:{port}: {e}"))?;
    sock.set_read_timeout(Some(POLL)).map_err(|e| e.to_string())?;
    let writer = sock.try_clone().map_err(|e| e.to_string())?;
    attrs.insert("host".into(), json!(host));
    attrs.insert("port".into(), json!(port));
    Ok(pump(
        streams,
        "socket",
        &format!("{host}:{port}"),
        "net.open",
        participants,
        attrs,
        Box::new(sock),
        Box::new(writer),
    ))
}

pub fn serial(a: Args<'_>, streams: &Arc<Streams>, participants: Vec<String>, mut attrs: Map<String, Value>) -> Reply {
    let port = a.need("port")?;
    let baud = a.int("baud", 115200)?;
    let dev = serialport::new(&port, baud as u32)
        .timeout(POLL)
        .open()
        .map_err(|e| format!("{port}: {e}"))?;
    let writer = dev.try_clone().map_err(|e| format!("{port}: {e}"))?;
    attrs.insert("port".into(), json!(port));
    attrs.insert("baud".into(), json!(baud));
    Ok(pump(
        streams,
        "serial",
        &port,
        "serial.open",
        participants,
        attrs,
        Box::new(dev),
        Box::new(writer),
    ))
}

#[allow(clippy::too_many_arguments)]
fn pump(
    streams: &Arc<Streams>,
    kind: &str,
    title: &str,
    producer: &'static str,
    participants: Vec<String>,
    attrs: Map<String, Value>,
    mut r: Box<dyn Read + Send>,
    mut w: Box<dyn Write + Send>,
) -> Value {
    let opened = streams.open(kind, title, producer, participants, attrs);
    let sid = opened.id.clone();
    let stop = Arc::new(AtomicBool::new(false));
    let input: mpsc::Receiver<Value> = opened.input;
    {
        let stop = stop.clone();
        std::thread::spawn(move || {
            while let Ok(body) = input.recv() {
                if let Some(text) = body["text"].as_str() {
                    if w.write_all(text.as_bytes()).and_then(|_| w.flush()).is_err() {
                        break;
                    }
                } else if body.get("signal").is_some() {
                    break;
                }
            }
            stop.store(true, Ordering::Relaxed); // asked to close, or the stream closed
        });
    }
    let streams = streams.clone();
    std::thread::spawn(move || {
        let mut buf = vec![0u8; 64 * 1024];
        let mut status = Map::new();
        loop {
            match r.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    if streams
                        .append(&sid, "bytes", Body::Bytes(buf[..n].to_vec()), producer)
                        .is_err()
                    {
                        break;
                    }
                }
                Err(e)
                    if matches!(
                        e.kind(),
                        ErrorKind::TimedOut | ErrorKind::WouldBlock | ErrorKind::Interrupted
                    ) =>
                {
                    if stop.load(Ordering::Relaxed) {
                        break;
                    }
                }
                Err(e) => {
                    status.insert("error".into(), json!(e.to_string()));
                    break;
                }
            }
        }
        streams.close(&sid, producer, status);
    });
    json!({"stream": opened.id})
}
