//! net.open (TCP, optionally TLS), net.udp and serial.open
//! (worker/manifests/net.yaml, serial.yaml): a connection, a UDP socket or a
//! port as a stream. What arrives becomes `bytes` events; input `text` or
//! `bytes` is sent; any signal (stream.close sends HUP) closes it. The hub
//! script speaks the protocol (HTTP, WebSocket, SSDP); the worker carries bytes.

use std::io::{ErrorKind, Read, Write};
use std::net::{TcpStream, ToSocketAddrs, UdpSocket};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;

use rustls::client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier};
use rustls::crypto::{verify_tls12_signature, verify_tls13_signature, CryptoProvider};
use rustls::pki_types::{CertificateDer, ServerName, UnixTime};
use rustls::{ClientConfig, ClientConnection, DigitallySignedStruct, RootCertStore, SignatureScheme};

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
    let (tls, verify) = (a.bool("tls", false)?, a.bool("verify", true)?);
    let addr = (host.as_str(), port)
        .to_socket_addrs()
        .map_err(|e| format!("{host}: {e}"))?
        .next()
        .ok_or(format!("{host}: no address"))?;
    let mut sock = TcpStream::connect_timeout(&addr, CONNECT).map_err(|e| format!("{host}:{port}: {e}"))?;
    attrs.insert("host".into(), json!(host));
    attrs.insert("port".into(), json!(port));
    attrs.insert("tls".into(), json!(tls));
    let title = format!("{host}:{port}");
    if !tls {
        sock.set_read_timeout(Some(POLL)).map_err(|e| e.to_string())?;
        let writer = sock.try_clone().map_err(|e| e.to_string())?;
        return Ok(pump(
            streams,
            "socket",
            &title,
            "net.open",
            participants,
            attrs,
            Box::new(sock),
            Box::new(writer),
        ));
    }
    let name = ServerName::try_from(host.clone()).map_err(|e| format!("{host}: {e}"))?;
    let mut conn = ClientConnection::new(tls_config(verify), name).map_err(|e| e.to_string())?;
    sock.set_read_timeout(Some(CONNECT)).map_err(|e| e.to_string())?;
    while conn.is_handshaking() {
        conn.complete_io(&mut sock)
            .map_err(|e| format!("{host}:{port}: TLS: {e}"))?;
    }
    sock.set_read_timeout(Some(POLL)).map_err(|e| e.to_string())?;
    let conn = Arc::new(Mutex::new(conn));
    let writer = TlsWrite(conn.clone(), sock.try_clone().map_err(|e| e.to_string())?);
    Ok(pump(
        streams,
        "socket",
        &title,
        "net.open",
        participants,
        attrs,
        Box::new(TlsRead {
            conn,
            sock,
            raw: vec![0; 16 * 1024],
            fed: 0,
            got: 0,
        }),
        Box::new(writer),
    ))
}

/// One TLS session shared by the stream's reader and writer threads. Each
/// waits on the socket without the lock and holds it only to decrypt or
/// encrypt in memory, so neither can keep the other waiting.
struct TlsRead {
    conn: Arc<Mutex<ClientConnection>>,
    sock: TcpStream,
    raw: Vec<u8>,
    fed: usize, // raw[fed..got] came from the socket and is not yet in rustls
    got: usize,
}
struct TlsWrite(Arc<Mutex<ClientConnection>>, TcpStream);

impl Read for TlsRead {
    /// Like rustls's own stream: plaintext out before more ciphertext in, so
    /// its plaintext buffer never overfills.
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        loop {
            {
                let mut conn = self.conn.lock().unwrap();
                match conn.reader().read(buf) {
                    Err(e) if e.kind() == ErrorKind::WouldBlock => {}
                    done => return done,
                }
                if self.fed < self.got {
                    let n = conn.read_tls(&mut &self.raw[self.fed..self.got])?;
                    conn.process_new_packets().map_err(std::io::Error::other)?;
                    // 0: rustls has its close_notify and takes no more; what is left is dropped
                    self.fed = if n == 0 { self.got } else { self.fed + n };
                    continue;
                }
            }
            let n = self.sock.read(&mut self.raw)?;
            (self.fed, self.got) = (0, n);
            if n == 0 {
                // the end: the next turn says whether it was a clean close
                self.conn.lock().unwrap().read_tls(&mut &[][..])?;
            }
        }
    }
}

impl Write for TlsWrite {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let mut out = Vec::new();
        let n = {
            let mut conn = self.0.lock().unwrap();
            let n = conn.writer().write(buf)?;
            while conn.wants_write() {
                conn.write_tls(&mut out)?;
            }
            n
        };
        self.1.write_all(&out)?;
        Ok(n)
    }
    fn flush(&mut self) -> std::io::Result<()> {
        self.write(&[]).map(|_| ())
    }
}

/// The web's roots, or (`verify` false: a device with a self-signed
/// certificate) any certificate, its handshake signatures still checked.
fn tls_config(verify: bool) -> Arc<ClientConfig> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let builder = ClientConfig::builder_with_provider(provider.clone())
        .with_safe_default_protocol_versions()
        .expect("ring supports the default versions");
    let config = if verify {
        let roots = RootCertStore {
            roots: webpki_roots::TLS_SERVER_ROOTS.to_vec(),
        };
        builder.with_root_certificates(roots).with_no_client_auth()
    } else {
        builder
            .dangerous()
            .with_custom_certificate_verifier(Arc::new(AnyCertificate(provider)))
            .with_no_client_auth()
    };
    Arc::new(config)
}

#[derive(Debug)]
struct AnyCertificate(Arc<CryptoProvider>);

impl ServerCertVerifier for AnyCertificate {
    fn verify_server_cert(
        &self,
        _: &CertificateDer<'_>,
        _: &[CertificateDer<'_>],
        _: &ServerName<'_>,
        _: &[u8],
        _: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        Ok(ServerCertVerified::assertion())
    }
    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(message, cert, dss, &self.0.signature_verification_algorithms)
    }
    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        dss: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(message, cert, dss, &self.0.signature_verification_algorithms)
    }
    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.0.signature_verification_algorithms.supported_schemes()
    }
}

/// A UDP socket as a stream: input `{text | bytes, to: "host:port"}` sends
/// one datagram (broadcast and multicast addresses too); each datagram that
/// arrives is a `bytes` event whose `by` is its sender (`ip:port`).
pub fn udp(_: Args<'_>, streams: &Arc<Streams>, participants: Vec<String>, mut attrs: Map<String, Value>) -> Reply {
    let sock = UdpSocket::bind("0.0.0.0:0").map_err(|e| format!("udp: {e}"))?;
    sock.set_broadcast(true).map_err(|e| format!("udp: {e}"))?;
    sock.set_read_timeout(Some(POLL)).map_err(|e| e.to_string())?;
    let port = sock.local_addr().map_err(|e| e.to_string())?.port();
    attrs.insert("port".into(), json!(port));
    let opened = streams.open("udp", &format!("udp :{port}"), "net.udp", participants, attrs);
    let (sid, input) = (opened.id.clone(), opened.input);
    let stop = Arc::new(AtomicBool::new(false));
    {
        let (stop, sock, streams, sid) = (
            stop.clone(),
            sock.try_clone().map_err(|e| e.to_string())?,
            streams.clone(),
            sid.clone(),
        );
        std::thread::spawn(move || {
            while let Ok(body) = input.recv() {
                if body.get("signal").is_some() {
                    break;
                }
                let sent = match (crate::streams::input_data(&body), body["to"].as_str()) {
                    (Some(data), Some(to)) => to
                        .to_socket_addrs()
                        .map_err(|e| format!("{to}: {e}"))
                        .and_then(|mut a| a.find(|a| a.is_ipv4()).ok_or(format!("{to}: no IPv4 address")))
                        .and_then(|addr| sock.send_to(&data, addr).map_err(|e| format!("{to}: {e}"))),
                    _ => Err("a datagram needs text or bytes, and to (host:port)".into()),
                };
                if let Err(e) = sent {
                    let _ = streams.append(&sid, "status", Body::Json(json!({"error": e})), "net.udp");
                }
            }
            stop.store(true, Ordering::Relaxed);
        });
    }
    let streams = streams.clone();
    std::thread::spawn(move || {
        let mut buf = vec![0u8; 65536];
        loop {
            match sock.recv_from(&mut buf) {
                Ok((n, from)) => {
                    if streams
                        .append(&sid, "bytes", Body::Bytes(buf[..n].to_vec()), &from.to_string())
                        .is_err()
                    {
                        break;
                    }
                }
                Err(_) if !stop.load(Ordering::Relaxed) => {} // a quiet POLL, or an ICMP refusal from a send
                Err(_) => break,
            }
        }
        streams.close(&sid, "net.udp", Map::new());
    });
    Ok(json!({"stream": opened.id}))
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
                if let Some(data) = crate::streams::input_data(&body) {
                    if w.write_all(&data).and_then(|_| w.flush()).is_err() {
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

#[cfg(test)]
mod tests {
    use super::*;
    use base64::Engine;
    use rustls::pki_types::pem::PemObject;
    use rustls::pki_types::PrivateKeyDer;
    use rustls::{ServerConfig, ServerConnection, StreamOwned};
    use std::io::{BufRead, BufReader};
    use std::net::TcpListener;
    use std::time::Instant;

    // A device's self-signed certificate (P-256, CN device.local).
    const CERT: &str = "-----BEGIN CERTIFICATE-----
MIIBhDCCASugAwIBAgIUV6b6fHsOo8RnRN0GjmXVBOCcV3UwCgYIKoZIzj0EAwIw
FzEVMBMGA1UEAwwMZGV2aWNlLmxvY2FsMCAXDTI2MTExMDA0MzEwMloYDzIxMjYx
MDE3MDQzMTAyWjAXMRUwEwYDVQQDDAxkZXZpY2UubG9jYWwwWTATBgcqhkjOPQIB
BggqhkjOPQMBBwNCAASP1rBX+K1gMttml/MJsFeHvmdLQ5a9itxigdwZjdVXzFqg
4qTYM6wCPFnrWkloa/1LZf/qxxFMkx4GJsmQ5LgBo1MwUTAdBgNVHQ4EFgQU/bf9
HRghHJekenalMddwIPQkEVMwHwYDVR0jBBgwFoAU/bf9HRghHJekenalMddwIPQk
EVMwDwYDVR0TAQH/BAUwAwEB/zAKBggqhkjOPQQDAgNHADBEAiB86rqwYYQOFmSZ
ZaEwclrc8LGfqDF39N8dMahNoohExgIgX0OQlOgXjec737WEdtU1z8DhezdG9HV2
ZDE0NKb05+k=
-----END CERTIFICATE-----";
    const KEY: &str = "-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQge7W9zZqAHxLCwsdK
YzzfK8SfubBIb0y1ZFn7UZM8v3ChRANCAASP1rBX+K1gMttml/MJsFeHvmdLQ5a9
itxigdwZjdVXzFqg4qTYM6wCPFnrWkloa/1LZf/qxxFMkx4GJsmQ5LgB
-----END PRIVATE KEY-----";

    /// A TLS server for one connection: `serve` runs once the handshake is done.
    fn server(serve: impl FnOnce(StreamOwned<ServerConnection, TcpStream>) + Send + 'static) -> u16 {
        let cert = CertificateDer::from_pem_slice(CERT.as_bytes()).unwrap();
        let key = PrivateKeyDer::from_pem_slice(KEY.as_bytes()).unwrap();
        let config = ServerConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(vec![cert], key)
            .unwrap();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            let (mut sock, _) = listener.accept().unwrap();
            let mut conn = ServerConnection::new(Arc::new(config)).unwrap();
            while conn.is_handshaking() {
                conn.complete_io(&mut sock).unwrap();
            }
            serve(StreamOwned::new(conn, sock));
        });
        port
    }

    fn open(port: u16) -> (Arc<Streams>, String) {
        let streams = Streams::new(Box::new(|_, _| {}));
        let a = json!({"host": "127.0.0.1", "port": port, "tls": true, "verify": false});
        let out = tcp(Args(a.as_object().unwrap()), &streams, vec![], Map::new()).unwrap();
        (streams, out["stream"].as_str().unwrap().to_string())
    }

    /// What arrives on the stream after `*next`, until `done` says enough or
    /// the stream closes; the closing status if it did.
    async fn take(
        streams: &Streams,
        sid: &str,
        next: &mut u64,
        done: impl Fn(&[u8]) -> bool,
    ) -> (Vec<u8>, Option<Value>) {
        let mut got = vec![];
        while !done(&got) {
            let q = json!({"stream": sid, "after": *next, "wait": 5});
            let out = streams.read(Args(q.as_object().unwrap())).await.unwrap();
            assert_eq!(out["dropped"], 0);
            let events = out["events"].as_array().unwrap();
            assert!(!events.is_empty(), "nothing in 5 s; got {} bytes", got.len());
            *next = out["next"].as_u64().unwrap();
            for e in events {
                if e["type"] == "bytes" {
                    let b = base64::engine::general_purpose::STANDARD.decode(e["b64"].as_str().unwrap());
                    got.extend(b.unwrap());
                } else if e["data"]["state"] == "closed" {
                    return (got, Some(e["data"].clone()));
                }
            }
        }
        (got, None)
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_write_on_a_tls_stream_goes_out_while_the_reader_waits() {
        let (streams, sid) = open(server(|tls| {
            let mut tls = BufReader::new(tls);
            let mut line = String::new();
            while tls.read_line(&mut line).unwrap_or(0) > 0 {
                tls.get_mut().write_all(line.as_bytes()).unwrap();
                tls.get_mut().flush().unwrap();
                line.clear();
            }
        }));
        let (mut next, mut fastest) = (0, Duration::MAX);
        for i in 0..10 {
            let start = Instant::now();
            let body = json!({"stream": sid, "body": {"text": format!("ping {i}\n")}});
            streams.write(Args(body.as_object().unwrap()), "me").unwrap();
            let (got, _) = take(&streams, &sid, &mut next, |g| g.ends_with(b"\n")).await;
            assert_eq!(got, format!("ping {i}\n").into_bytes());
            fastest = fastest.min(start.elapsed());
        }
        // The reader sits in a read the whole time; a write that waited for it would take most of a POLL.
        assert!(fastest < POLL / 2, "the fastest ping took {fastest:?}");
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_download_in_odd_sized_records_arrives_whole_and_closes_cleanly() {
        let blob: Vec<u8> = (0..240_000u32).map(|i| (i % 251) as u8).collect();
        let sent = blob.clone();
        let (streams, sid) = open(server(move |tls| {
            let mut tls = BufReader::new(tls);
            tls.read_line(&mut String::new()).unwrap();
            let tls = tls.get_mut();
            for chunk in sent.chunks(10_000) {
                tls.write_all(chunk).unwrap();
                tls.flush().unwrap();
            }
            // close_notify, then stray bytes in the same segment, then the end
            tls.conn.send_close_notify();
            let mut out = vec![];
            while tls.conn.wants_write() {
                tls.conn.write_tls(&mut out).unwrap();
            }
            out.extend([b'x'; 5000]);
            tls.sock.write_all(&out).unwrap();
        }));
        let body = json!({"stream": sid, "body": {"text": "GET\n"}});
        streams.write(Args(body.as_object().unwrap()), "me").unwrap();
        let (got, closed) = take(&streams, &sid, &mut 0, |_| false).await;
        assert_eq!(closed, Some(json!({"state": "closed"})));
        assert!(got == blob, "{} of {} bytes", got.len(), blob.len());
    }

    #[tokio::test(flavor = "multi_thread")]
    async fn a_server_that_speaks_first_is_heard() {
        let (streams, sid) = open(server(|mut tls| {
            tls.write_all(b"hello\n").unwrap();
            tls.flush().unwrap();
            tls.read_exact(&mut [0]).ok(); // stay open until the client goes
        }));
        let (got, _) = take(&streams, &sid, &mut 0, |g| g.ends_with(b"\n")).await;
        assert_eq!(got, b"hello\n");
    }
}
