"""The Rust worker (worker/) against a real hub over a real UDP relay, from
the same files enrollment writes for a Python worker: it joins, shows in the
worker list with its caps, runs a command, writes and reads a file, and goes
offline when stopped. Its manifest hashes are the Python worker's."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import yaml

pytest.importorskip("telesthete")
from rook.core2 import wire  # noqa: E402
from rook.core2.hub.runtime import Config, Hub  # noqa: E402
from rook.core2.transport import TelestheteLink, band_of  # noqa: E402

from test_core2_minimal_worker import Relay, door, secret_env, until  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = {"worker": ROOT / "rook/core2/builtin/worker.yaml", "stream": ROOT / "rook/core2/builtin/stream.yaml",
             "shell": ROOT / "rook/plugins/shell/plugin.yaml", "file": ROOT / "rook/plugins/file/plugin.yaml",
             "net": ROOT / "worker/manifests/net.yaml", "serial": ROOT / "worker/manifests/serial.yaml"}


@pytest.fixture(scope="module")
def binary() -> Path:
    if not shutil.which("cargo"):
        pytest.skip("cargo is not installed")
    subprocess.run(["cargo", "build", "--quiet", "--manifest-path", str(ROOT / "worker/Cargo.toml")],
                   check=True, timeout=900)
    return ROOT / "worker/target/debug/rook-worker"



def self_signed(folder):
    """A server TLS context with a fresh self-signed certificate (a device's)."""
    import datetime
    import ssl

    crypto = pytest.importorskip("cryptography")  # noqa: F841
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "device.local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    (folder / "dev.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (folder / "dev.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(folder / "dev.pem", folder / "dev.key")
    return ctx


def can_broadcast() -> bool:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(b"", ("255.255.255.255", 9))
        return True
    except OSError:
        return False

def enrolled(home: Path, psk: str, relay: str, worker_id: str) -> Path:
    """What `python -m rook.core2.worker enroll` leaves: worker.yaml and <data>/bands.json."""
    (home / "data").mkdir(parents=True)
    (home / "data" / "bands.json").write_text(json.dumps({band_of(psk): {"psk": psk, "relay": relay}}))
    config = home / "worker.yaml"
    config.write_text(yaml.safe_dump({"data": "data", "anchor": "anchor.json", "name": "rusty", "worker_id": worker_id}))
    return config


def test_the_rust_worker_on_a_real_hub(tmp_path, binary):
    now = [time.time()]

    async def go():
        relay, _ = await asyncio.get_running_loop().create_datagram_endpoint(Relay, local_addr=("127.0.0.1", 0))
        addr = "127.0.0.1:%d" % relay.get_extra_info("socket").getsockname()[1]
        psk = secrets.token_hex(24)
        library = tmp_path / "library"
        library.mkdir()
        (library / "kvm").symlink_to(ROOT / "rook/plugins/kvm", target_is_directory=True)
        hub = Hub(Config(data=tmp_path / "hub", users={"bake": {"preset": "owner"}}, library=library,
                         doors={"http": {"bind": "127.0.0.1:0"}},
                         transport={"bands": [{"psk": psk, "relay": addr}]}), clock=lambda: now[0])
        await hub.start()
        token = (hub.data / "owner.token").read_text().strip()
        wid = secrets.token_hex(16)
        heard: list[dict] = []

        async def overheard(payload: bytes) -> None:
            try:
                msg = json.loads(payload)
            except ValueError:
                return
            if isinstance(msg, dict) and msg.get("kind") == "announce" and msg.get("worker_id") == wid:
                heard.append(msg)

        async def nothing() -> None:
            pass

        ear = TelestheteLink(psk, relay=addr)
        await ear.start(overheard, nothing)
        proc = await asyncio.create_subprocess_exec(str(binary), "--config", str(enrolled(tmp_path / "w", psk, addr, wid)))

        async def listed():
            got = (await door(hub, token, "/workers"))["workers"]
            return next((x for x in got if x["name"] == "rusty"), None)

        async def call(cap, **args):
            return await door(hub, token, "/call", {"worker": "rusty", "cap": cap, "args": args,
                                                    "why": "Test the Rust worker."})

        async def beat_heard():
            return (await listed())["last_seen"] == now[0]
        try:
            me = await until(listed)
            assert (me["id"], me["online"], me["problem"]) == (wid, True, None)
            assert {"shell", "file"} <= set(me["plugins"]), me
            assert me["unavailable"]["worker.log"].startswith("the hub's journal"), me

            # The same manifests, the same hashes as a Python worker announces.
            entries = {e["plugin"]: e["hash"] for e in heard[0]["manifests"]}
            assert entries == {p: wire.manifest_hash(yaml.safe_load(f.read_text())) for p, f in MANIFESTS.items()}

            async def learned():        # the hub has fetched its manifests (core.manifests)
                caps = (await door(hub, token, "/caps?worker=rusty"))["workers"].get("rusty", {}).get("caps", {})
                return {"worker.ping", "shell.exec", "file.read", "net.open", "serial.open",
                        "appliance.discover", "kvm.power.status"} <= set(caps)   # hub scripts for it too
            await until(learned)
            out = await call("worker.ping")
            assert out["ok"] and out["result"]["pong"] is True, out
            out = await call("shell.exec", argv=["echo", "hello"])
            assert out["ok"] and out["result"] == {"ok": True, "code": 0, "stdout": "hello\n", "stderr": ""}, out
            out = await call("shell.exec", cmd="echo $((40+2)) >&2; exit 3")
            assert out["result"] == {"ok": False, "code": 3, "stdout": "", "stderr": "42\n"}, out
            out = await call("shell.which", name="sh")
            assert out["result"] and out["result"].endswith("/sh"), out
            # A secret reaches a command's environment by name.
            value = await secret_env(hub, call)

            target = tmp_path / "files" / "note.txt"
            out = await call("file.write", path=str(target), content="héllo\nworld\n", create_parents=True)
            assert out["result"] == {"path": str(target), "written": 13}, out
            out = await call("file.read", path=str(target))
            assert out["result"] == {"content": "héllo\nworld\n", "size": 13, "encoding": "utf-8"}, out
            out = await call("file.search", pattern="^wor", path=str(tmp_path / "files"))
            assert out["result"] == {"matches": [{"path": str(target), "line": 2, "text": "world"}]}, out
            out = await call("file.list", path=str(tmp_path / "files"))
            assert [(e["name"], e["type"], e["size"]) for e in out["result"]["entries"]] == [("note.txt", "file", 13)]
            big = secrets.token_bytes(3 * 1024 * 1024)      # a 4 MiB reply: it comes over Drop
            (tmp_path / "files" / "big.bin").write_bytes(big)
            out = await call("file.read", path=str(tmp_path / "files" / "big.bin"), encoding="base64")
            assert out["ok"] and base64.b64decode(out["result"]["content"]) == big, str(out)[:300]
            out = await call("file.stat", path=str(tmp_path / "nope"))
            assert out["result"] == {"exists": False}, out
            out = await call("file.read", path=str(tmp_path / "nope"))
            assert not out["ok"] and "not found" in out["error"], out
            out = await call("worker.log")
            assert not out["ok"] and "journal" in out["error"], out

            # A terminal, the way an agent drives one: open it, type, read the output back, close it.
            out = await call("shell.open", argv=["/bin/sh"], cols=80, rows=24)
            assert out["ok"], out
            sid = out["result"]["stream"]
            out = await call("stream.write", stream=sid, body={"text": "echo $((6*7))\r"})
            assert out["ok"], out
            seen, after = b"", 0
            while b"42\r\n" not in seen:
                got = (await call("stream.read", stream=sid, after=after, wait=5))["result"]
                seen += b"".join(base64.b64decode(e["b64"]) for e in got["events"] if e["type"] == "bytes")
                after = got["next"]
            assert (await call("worker.status"))["result"]["streams_open"] == 1

            async def indexed(open_only=True):
                out = await door(hub, token, "/call", {"worker": "rook", "cap": "stream.list",
                                                       "args": {"worker": "rusty", "open": open_only},
                                                       "why": "Test the Rust worker."})
                return {r["id"]: r for r in out["result"]["streams"]}
            row = (await until(indexed))[sid]
            assert (row["kind"], row["worker"], row["attrs"]["pty"]) == ("terminal", "rusty", True), row
            out = await call("stream.close", stream=sid)
            assert out["ok"] and out["result"]["closed"], out
            got = (await call("stream.read", stream=sid, after=after))["result"]
            assert got["closed"] and got["events"][-1]["data"]["state"] == "closed", got

            async def unindexed():
                return sid not in await indexed() and sid in await indexed(False)
            await until(unindexed)

            # A process on pipes: output merged, closed with its exit code.
            out = await call("shell.open", cmd="echo out; echo err >&2; exit 4", pty=False)
            pid = out["result"]["stream"]

            async def finished_on(stream):
                got = (await call("stream.read", stream=stream, wait=5))["result"]
                return got if got["closed"] else None
            got = await until(lambda: finished_on(pid))
            assert b"".join(base64.b64decode(e["b64"]) for e in got["events"] if e["type"] == "bytes") == b"out\nerr\n"
            assert got["events"][-1]["data"] == {"exit": 4, "state": "closed"}, got
            listed_here = (await call("stream.list"))["result"]["streams"]
            assert {r["id"]: r["kind"] for r in listed_here} == {sid: "terminal", pid: "process"}
            # A process's env takes a vault reference too; what it prints on its
            # stream is its output, not a reply: the hub does not redact it.
            out = await call("shell.open", argv=["sh", "-c", 'printf %s "$ROOK_T"'], pty=False,
                             env={"ROOK_T": "{{secret:env-test}}"})
            printed = (await until(lambda: finished_on(out["result"]["stream"])))["events"]
            assert b"".join(base64.b64decode(e["b64"]) for e in printed if e["type"] == "bytes") == value.encode()
            assert value not in json.dumps(hub.journal.query(limit=1000))

            async def output(stream, want: bytes):
                seen, after = b"", 0
                while want not in seen:
                    got = (await call("stream.read", stream=stream, after=after, wait=5))["result"]
                    seen += b"".join(base64.b64decode(e["b64"]) for e in got["events"] if e["type"] == "bytes")
                    after = got["next"]

            # TCP: a local echo server, through the hub.
            async def echo(reader, writer):
                while data := await reader.read(1024):
                    writer.write(data)
                    await writer.drain()
                writer.close()
            server = await asyncio.start_server(echo, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            out = await call("net.open", host="127.0.0.1", port=port)
            assert out["ok"], out
            tcp = out["result"]["stream"]
            await call("stream.write", stream=tcp, body={"text": "ping\n"})
            await output(tcp, b"ping\n")
            assert (await call("stream.close", stream=tcp))["result"]["closed"]
            server.close()
            out = await call("net.open", host="127.0.0.1", port=port)
            assert not out["ok"] and "refused" in out["error"].lower(), out

            # TLS to a device with a self-signed certificate, raw bytes both ways.
            blob = bytes(range(256)) * 4
            tls_server = await asyncio.start_server(echo, "127.0.0.1", 0, ssl=self_signed(tmp_path))
            port = tls_server.sockets[0].getsockname()[1]
            out = await call("net.open", host="127.0.0.1", port=port, tls=True)
            assert not out["ok"] and "TLS" in out["error"] and "certificate" in out["error"], out
            out = await call("net.open", host="127.0.0.1", port=port, tls=True, verify=False)
            assert out["ok"], out
            tls = out["result"]["stream"]
            await call("stream.write", stream=tls, body={"bytes": base64.b64encode(blob).decode()})
            await output(tls, blob)
            assert (await call("stream.close", stream=tls))["result"]["closed"]
            tls_server.close()

            # UDP: datagrams out (unicast, broadcast) and in, each with its sender.
            class Echo(asyncio.DatagramProtocol):
                def connection_made(self, transport):
                    self.transport = transport

                def datagram_received(self, data, addr):
                    self.transport.sendto(b"re:" + data, addr)
            peer, _ = await asyncio.get_running_loop().create_datagram_endpoint(Echo, local_addr=("127.0.0.1", 0))
            peer_at = "127.0.0.1:%d" % peer.get_extra_info("socket").getsockname()[1]
            out = await call("net.udp")
            assert out["ok"], out
            udp = out["result"]["stream"]
            if can_broadcast():           # Wake-on-LAN's broadcast; a send error would be a status event
                await call("stream.write", stream=udp, body={"bytes": base64.b64encode(b"\xff" * 6).decode(),
                                                             "to": "255.255.255.255:9"})
            await call("stream.write", stream=udp, body={"bytes": base64.b64encode(b"\x00M-SEARCH").decode(),
                                                         "to": peer_at})

            async def datagrams():
                got = (await call("stream.read", stream=udp, wait=5))["result"]["events"]
                return got if any(e["type"] == "bytes" for e in got) else None
            got = await until(datagrams)
            assert [(e["by"], base64.b64decode(e["b64"])) for e in got if e["type"] == "bytes"] == \
                [(peer_at, b"re:\x00M-SEARCH")], got
            assert not [e for e in got if e["type"] == "status" and "error" in e["data"]], got
            worker_port = next(r for r in (await call("stream.list"))["result"]["streams"] if r["id"] == udp)["attrs"]["port"]
            peer.sendto(b"hello", ("127.0.0.1", worker_port))           # unsolicited, the other way
            await output(udp, b"hello")
            await call("stream.write", stream=udp, body={"text": "x"})    # no destination
            got = (await call("stream.read", stream=udp, types=["status"], wait=5))["result"]["events"]
            assert any("to (host:port)" in e["data"].get("error", "") for e in got), got
            assert (await call("stream.close", stream=udp))["result"]["closed"]
            peer.close()

            # Serial: one end of a pty pair is the "device".
            device, port_fd = os.openpty()
            try:
                out = await call("serial.open", port=os.ttyname(port_fd), baud=9600)
                assert out["ok"], out
                ser = out["result"]["stream"]
                await call("stream.write", stream=ser, body={"text": "AT\r"})
                got = b""
                while b"AT\r" not in got:
                    got += await asyncio.to_thread(os.read, device, 64)
                os.write(device, b"OK\r\n")
                await output(ser, b"OK\r\n")
                assert (await call("stream.close", stream=ser))["result"]["closed"]
            finally:
                os.close(device)
                os.close(port_fd)

            # A device plugin on the hub through this worker: kvm against a fake kvmd over HTTPS with a
            # self-signed certificate (TLS, an HTTP request and a binary JPEG through net.open's stream).
            jpeg = b"\xff\xd8\xff\xe0" + secrets.token_bytes(5000) + b"\xff\xd9"

            async def kvmd(reader, writer):
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
                path = head.split()[1]
                body, ctype = (b'{"ok": true, "result": {"busy": false, "leds": {"power": true, "hdd": false}}}',
                               "application/json") if path == "/api/atx" else (jpeg, "image/jpeg")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %d\r\n\r\n%s"
                             % (ctype.encode(), len(body), body))
                await writer.drain()
                writer.close()
            kvm_server = await asyncio.start_server(kvmd, "127.0.0.1", 0, ssl=self_signed(tmp_path))
            kvm_at = "127.0.0.1:%d" % kvm_server.sockets[0].getsockname()[1]
            out = await door(hub, token, "/call", {"worker": "rook", "cap": "appliance.add", "why": "Test kvm.",
                                                   "args": {"name": "rack-kvm", "plugin": "kvm", "worker": "rusty",
                                                            "host": kvm_at}})
            assert out["ok"], out
            out = await door(hub, token, "/call", {"worker": "rack-kvm", "cap": "kvm.power.status", "why": "Test kvm."})
            assert out["ok"] and out["result"] == {"power": True, "hdd": False, "busy": False}, out
            out = await door(hub, token, "/call", {"worker": "rack-kvm", "cap": "kvm.screen.capture", "why": "Test kvm."})
            assert out["ok"] and base64.b64decode(out["result"]["data"]) == jpeg, str(out)[:300]
            kvm_server.close()

            async def answered_hub():   # its announce on first hearing the hub's, so what comes next is a heartbeat
                return len(heard) >= 2
            await until(answered_hub)
            now[0] += 60            # a minute of hub time: its next heartbeat (24-36 s) keeps it online
            await until(beat_heard, 40)
            hub._liveness()
            assert (await listed())["online"] is True

            proc.terminate()
            await proc.wait()
            await asyncio.sleep(0.5)
            now[0] += 91
            hub._liveness()
            assert (await listed())["online"] is False
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await ear.stop()
            await hub.stop()
            relay.close()
    asyncio.run(asyncio.wait_for(go(), 180))
