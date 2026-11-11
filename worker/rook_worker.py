#!/usr/bin/env python3
"""Rook worker in one file, for machines where nothing can be installed.
Python 3.8+, standard library only. It speaks the minimal worker profile
(docs/spec/worker-profile.md) over a Telesthete UDP relay (or, when UDP
hears nothing, the hub's /band web socket) and serves commands and files;
streams, terminals and self-update it doesn't (to update it, replace this
file).

    python3 rook_worker.py enroll <hub address> [--code CODE] [--name NAME]
    python3 rook_worker.py

Its files are in ~/.rook-worker: worker.yaml (JSON, which is YAML),
anchor.json and data/ (bands.json, enrollment.json, services.json), the
same files the other workers write.
"""
from __future__ import annotations

import base64, binascii, codecs, hashlib, hmac, json, os, platform, random, re, shutil, signal, socket  # noqa: E401
import ssl, stat as st_, struct, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request  # noqa: E401
from fnmatch import fnmatchcase
from pathlib import Path

VERSION, CORE = "0.5.0", "2.0"
HOME = Path("~/.rook-worker").expanduser()
LIMIT = 256 * 1024          # largest reply sent inline: this worker has no Drop (see send_reply)
OUT_MAX = 8 << 20
FALLBACK = 5                # UDP silent this long after the announce: the hub's web socket instead
UNAVAILABLE = {
    "shell.open": "rook_worker.py has no streams or terminals; use shell.exec",
    "worker.log": "the hub's journal has every call this worker ran: read it there",
    "worker.update": "rook_worker.py updates by replacing the file",
}


class Err(Exception):
    """A refusal: its text is the reply's error."""


# -- ChaCha20-Poly1305 (RFC 8439), empty associated data -------------------------------

def _block(key, counter, nonce):
    s = [0x61707865, 0x3320646E, 0x79622D32, 0x6B206574, *key, counter & 0xFFFFFFFF, *nonce]
    x = list(s)
    for _ in range(10):
        for a, b, c, d in ((0, 4, 8, 12), (1, 5, 9, 13), (2, 6, 10, 14), (3, 7, 11, 15),
                           (0, 5, 10, 15), (1, 6, 11, 12), (2, 7, 8, 13), (3, 4, 9, 14)):
            x[a] = (x[a] + x[b]) & 0xFFFFFFFF; x[d] ^= x[a]; x[d] = ((x[d] << 16) | (x[d] >> 16)) & 0xFFFFFFFF  # noqa: E702
            x[c] = (x[c] + x[d]) & 0xFFFFFFFF; x[b] ^= x[c]; x[b] = ((x[b] << 12) | (x[b] >> 20)) & 0xFFFFFFFF  # noqa: E702
            x[a] = (x[a] + x[b]) & 0xFFFFFFFF; x[d] ^= x[a]; x[d] = ((x[d] << 8) | (x[d] >> 24)) & 0xFFFFFFFF  # noqa: E702
            x[c] = (x[c] + x[d]) & 0xFFFFFFFF; x[b] ^= x[c]; x[b] = ((x[b] << 7) | (x[b] >> 25)) & 0xFFFFFFFF  # noqa: E702
    return struct.pack("<16I", *((x[i] + s[i]) & 0xFFFFFFFF for i in range(16)))


def chacha20(key: bytes, counter: int, nonce: bytes, data: bytes) -> bytes:
    k, n, out = struct.unpack("<8I", key), struct.unpack("<3I", nonce), bytearray()
    for i in range(0, len(data), 64):
        chunk = data[i:i + 64]
        pad = _block(k, counter + i // 64, n)[:len(chunk)]
        out += (int.from_bytes(chunk, "little") ^ int.from_bytes(pad, "little")).to_bytes(len(chunk), "little")
    return bytes(out)


def poly1305(key: bytes, msg: bytes) -> bytes:
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    p, acc = (1 << 130) - 5, 0
    for i in range(0, len(msg), 16):
        acc = (acc + int.from_bytes(msg[i:i + 16] + b"\x01", "little")) * r % p
    return ((acc + int.from_bytes(key[16:], "little")) & ((1 << 128) - 1)).to_bytes(16, "little")


def _tag(key, nonce, ct):
    otk = chacha20(key, 0, nonce, bytes(32))
    return poly1305(otk, ct + bytes(-len(ct) % 16) + bytes(8) + struct.pack("<Q", len(ct)))


def seal(key: bytes, seq: int, plain: bytes) -> bytes:
    nonce = bytes(4) + seq.to_bytes(8, "big")
    ct = chacha20(key, 1, nonce, plain)
    return ct + _tag(key, nonce, ct)


def open_(key: bytes, seq: int, sealed: bytes):
    nonce, ct = bytes(4) + seq.to_bytes(8, "big"), sealed[:-16]
    if len(sealed) < 16 or not hmac.compare_digest(_tag(key, nonce, ct), sealed[-16:]):
        return None
    return chacha20(key, 1, nonce, ct)


# -- the Telesthete carrier (core v2 2.1) ---------------------------------------------

def band_id(psk: str) -> bytes:
    return hashlib.sha256(psk.encode()).digest()[:16]


def band_key(psk: str) -> bytes:
    prk = hmac.new(b"telesthete-v1", psk.encode(), hashlib.sha256).digest()
    return hmac.new(prk, b"encryption-chacha20-poly1305\x01", hashlib.sha256).digest()


def split(payload: bytes, fid: bytes) -> list:
    """The 21-byte fragment envelope: version, fragment id, seq, total; at most 1003 data bytes."""
    pieces = [payload[i:i + 1003] for i in range(0, len(payload), 1003)] or [b""]
    return [b"\x01" + fid + struct.pack(">HH", i, len(pieces)) + p for i, p in enumerate(pieces)]


def parse_chunk(c: bytes):
    if len(c) < 21 or c[0] != 1:
        return None
    seq, total = struct.unpack(">HH", c[17:21])
    return (c[1:17], seq, total, c[21:]) if total and seq < total else None


class Reassembler:
    def __init__(self):
        self.parts = {}

    def feed(self, chunk: bytes):
        got = parse_chunk(chunk)
        if got is None:
            return None
        fid, seq, total, data = got
        if total == 1:
            return data
        now = time.monotonic()
        for k in [k for k, v in self.parts.items() if now - v[2] > 30]:
            del self.parts[k]
        if len(self.parts) >= 256 and fid not in self.parts:
            del self.parts[min(self.parts, key=lambda k: self.parts[k][2])]
        entry = self.parts.setdefault(fid, [total, {}, now])
        entry[1].setdefault(seq, data)
        if len(entry[1]) == entry[0]:
            del self.parts[fid]
            return b"".join(entry[1][i] for i in range(entry[0]))
        return None


class Link:
    """One band over a UDP relay: CHANNEL frames sealed with empty associated
    data. :meth:`first` moves it to the hub's /band web socket (one frame per
    binary message) when UDP hears nothing; a dropped web socket connects
    again."""

    def __init__(self, psk: str, relay: str, ws_url: str | None = None):
        host, _, port = relay.rpartition(":")
        host, port = (host, int(port)) if host else (relay, 7474)
        self.addr = socket.getaddrinfo(host.strip("[]"), port, type=socket.SOCK_DGRAM)[0][4]
        self.sock = socket.socket(socket.AF_INET6 if ":" in self.addr[0] else socket.AF_INET, socket.SOCK_DGRAM)
        self.band, self.key, self.lock = band_id(psk), band_key(psk), threading.Lock()
        self.seq = random.SystemRandom().getrandbits(63)
        self.reasm = Reassembler()
        self.ws_url, self.ws = ws_url, None

    def frame(self, seq: int, chunk: bytes) -> bytes:
        return self.band + b"\x02" + bytes(2) + seq.to_bytes(8, "big") + seal(self.key, seq, chunk)

    def send(self, payload: bytes) -> None:
        for chunk in split(payload, os.urandom(16)):
            with self.lock:
                self.seq += 1
                if not self.ws_url or self.ws is None:
                    self.sock.sendto(self.frame(self.seq, chunk), self.addr)
                    continue
                try:
                    self._ws_send(2, self.frame(self.seq, chunk))
                except OSError:
                    pass                                # recv connects again

    def first(self, announce):
        """With a web socket address: UDP's first payload within FALLBACK
        seconds, else the web socket from now on (and None); neither: both
        again."""
        while self.ws_url:
            self.sock.settimeout(FALLBACK)
            try:
                return self.recv()
            except socket.timeout:
                pass
            finally:
                self.sock.settimeout(None)
            print(f"rook_worker.py: nothing heard over UDP in {FALLBACK}s; trying {self.ws_url}", file=sys.stderr)
            try:
                self.ws = self._ws_connect()
            except OSError as e:
                print(f"rook_worker.py: {e}", file=sys.stderr)
            announce()
            if self.ws is not None:
                return None
        return None

    def _ws_connect(self):
        u = urllib.parse.urlsplit(self.ws_url)
        conn = socket.create_connection((u.hostname, u.port or (443 if u.scheme == "wss" else 80)), timeout=30)
        if u.scheme == "wss":
            conn = ssl.create_default_context().wrap_socket(conn, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        conn.sendall(f"GET {u.path or '/'} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\n"
                     f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        reader = conn.makefile("rb")
        status = reader.readline()
        while reader.readline() not in (b"\r\n", b"\n", b""):
            pass
        if not status.startswith(b"HTTP/1.1 101"):
            conn.close()
            raise OSError(f"no web socket at {self.ws_url}: {status.strip().decode(errors='replace')}")
        conn.settimeout(None)
        print(f"rook_worker.py: on the band through {self.ws_url}", file=sys.stderr)
        return conn, reader

    def _ws_send(self, op: int, data: bytes) -> None:
        n, mask = len(data), os.urandom(4)
        size = bytes([0x80 | n]) if n < 126 else (bytes([0xFE]) + n.to_bytes(2, "big") if n < 65536
                                                  else bytes([0xFF]) + n.to_bytes(8, "big"))
        masked = (int.from_bytes(data, "big") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
        self.ws[0].sendall(bytes([0x80 | op]) + size + mask + masked)

    def _ws_frame(self) -> bytes:
        """The next binary message (answering pings); OSError when the socket ends."""
        reader = self.ws[1]
        while True:
            head = reader.read(2)
            if len(head) < 2:
                raise OSError("the web socket closed")
            n = head[1] & 0x7F
            n = int.from_bytes(reader.read(2 if n == 126 else 8), "big") if n >= 126 else n
            mask = reader.read(4) if head[1] & 0x80 else b""
            data = reader.read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            op = head[0] & 0x0F
            if op == 2:
                return data
            if op == 8:
                raise OSError("the web socket closed")
            if op == 9:
                with self.lock:
                    self._ws_send(10, data)

    def _ws_again(self) -> None:
        wait = 1
        while True:
            print(f"rook_worker.py: the web socket dropped; connecting again in {wait}s", file=sys.stderr)
            time.sleep(wait)
            try:
                ws = self._ws_connect()
            except OSError as e:
                print(f"rook_worker.py: {e}", file=sys.stderr)
                wait = min(wait * 2, 30)
                continue
            with self.lock:
                self.ws = ws
            return

    def recv(self) -> bytes:
        while True:
            if self.ws is not None:
                try:
                    data = self._ws_frame()
                except OSError:
                    self._ws_again()
                    continue
            else:
                try:
                    data = self.sock.recv(65536)
                except ConnectionResetError:        # Windows: an earlier send's ICMP refusal
                    continue
            if len(data) < 43 or data[:16] != self.band or data[16] not in (2, 6):
                continue
            chunk = open_(self.key, int.from_bytes(data[19:27], "big"), data[27:])
            whole = self.reasm.feed(chunk) if chunk is not None else None
            if whole is not None and whole != b"\x00":
                return whole


# -- the profile -------------------------------------------------------------------

def canonical(v) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Worker:
    def __init__(self):
        cfg = load(HOME / "worker.yaml")
        data = HOME / cfg.get("data", "data")
        bands = load(data / "bands.json")
        spec = next((b for b in bands.values() if b.get("psk") and b.get("relay")), None)
        if not spec:
            raise Err("no band with a psk and a relay: enroll first (python3 rook_worker.py enroll <hub>)")
        self.id, self.name = cfg["worker_id"], cfg["name"]
        site = load(data / "enrollment.json").get("url")
        door = spec.get("ws_url") or (site and "%s://%s/band" % ("wss" if site.startswith("https:") else "ws",
                                                                 urllib.parse.urlsplit(site).netloc))
        self.link, self.started = Link(spec["psk"], spec["relay"], door or None), time.time()
        self.entries = [{"plugin": m["plugin"], "version": m["version"], "hash": sha(text),
                         **({"builtin": True} if m.get("builtin") else {})}
                        for text, m in ((t, json.loads(t)) for t in MANIFESTS)]
        self.hub, self.heard = None, 0.0

    def send(self, msg: dict) -> None:
        self.link.send(json.dumps(msg).encode())

    def announce(self) -> None:
        self.send({"kind": "announce", "worker_id": self.id, "name": self.name, "core": CORE, "version": VERSION,
                   "facts": facts(), "manifests": self.entries, "manifests_hash": sha(canonical(self.entries)),
                   "unavailable": UNAVAILABLE})

    def beat(self) -> None:
        n = 0
        while True:
            time.sleep(5)
            n += 5
            self.link.send(b"\x00")                      # the relay's keepalive
            if n >= 30 * random.uniform(0.8, 1.2):
                n = 0
                self.send({"kind": "heartbeat", "worker_id": self.id, "core": CORE,
                           "manifests_hash": sha(canonical(self.entries))})

    def run(self) -> None:
        self.announce()
        threading.Thread(target=self.beat, daemon=True).start()
        first = self.link.first(self.announce)
        while True:
            payload, first = first or self.link.recv(), None
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("kind") == "announce" and msg.get("name") == "rook":
                fresh = msg.get("worker_id") != self.hub or time.monotonic() - self.heard > 90
                self.hub, self.heard = msg.get("worker_id"), time.monotonic()
                if fresh:                                # the hub may have restarted: announce again
                    self.announce()
            elif isinstance(msg.get("cap"), str) and msg.get("target") == self.id and isinstance(msg.get("id"), str):
                threading.Thread(target=self.answer, args=(msg,), daemon=True).start()

    def answer(self, msg: dict) -> None:
        args = msg.get("args") if isinstance(msg.get("args"), dict) else {}
        reply = {"id": msg["id"], "from": self.id}
        try:
            reply.update(ok=True, result=self.call(msg["cap"], args))
        except Err as e:
            reply.update(ok=False, error=str(e))
        except Exception as e:                           # a bug, or an OS error not caught below
            reply.update(ok=False, error=f"{type(e).__name__}: {e}")
        data = json.dumps(reply).encode()
        if len(data) > LIMIT:
            # No Drop here: a large reply would be hundreds of unacknowledged
            # UDP frames, each sealed by this file's pure-Python cipher.
            data = json.dumps({"id": msg["id"], "from": self.id, "ok": False, "error":
                               f"result too large: {len(data)} bytes; rook_worker.py sends at most {LIMIT} bytes "
                               "inline (no Drop): ask for less (max_bytes, limit)"}).encode()
        self.link.send(data)

    def call(self, cap: str, a: dict):
        if cap in UNAVAILABLE:
            raise Err(f"{cap} is unavailable: {UNAVAILABLE[cap]}")
        if cap == "core.manifests":
            want = a.get("plugins") or []
            return {"manifests": [m for m in map(json.loads, MANIFESTS) if not want or m["plugin"] in want]}
        if cap == "worker.ping":
            return {"pong": True, "at": time.time()}
        if cap == "worker.status":
            return {"name": self.name, "worker_id": self.id, "version": VERSION, "core": CORE, "facts": facts(),
                    "pid": os.getpid(), "started": round(self.started, 3), "uptime_s": int(time.time() - self.started),
                    "band": {"connected": True, "hub_contact": time.monotonic() - self.heard <= 90},
                    "plugins": [f"{e['plugin']}@{e['version']}" for e in self.entries if not e.get("builtin")],
                    "unavailable": UNAVAILABLE, "streams_open": 0, "release": None}
        if cap == "worker.restart":
            threading.Timer(0.5, lambda: os.execv(sys.executable, [sys.executable, *sys.argv])).start()
            return {"restarting": True}
        fn = CAPS.get(cap)
        if fn is None:
            raise Err(f"unknown capability: {cap}")
        return fn(**a)


def facts() -> dict:
    os_name = "windows" if platform.system() == "Windows" else "linux"
    return {"os": os_name, "arch": platform.machine().lower(), "cpus": os.cpu_count() or 1,
            "python": "{}.{}".format(*sys.version_info[:2])}


def load(path: Path) -> dict:
    try:
        got = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError:
        raise Err(f"{path}: not JSON (rook_worker.py reads the JSON form of YAML)") from None
    return got if isinstance(got, dict) else {}


# -- shell (rook/plugins/shell: the same contract) ---------------------------------

IS_WIN = os.name == "nt"
SECRET = re.compile(r"(TOKEN|SECRET|PASSW|PSK|API_?KEY|PRIVATE|CREDENTIAL|_KEY$|^ROOK_)", re.I)
DENIED = ("permission denied", "operation not permitted", "access is denied", "must be run as root", "are you root",
          "requires root", "eacces", "eperm", "requires elevation", "administrator privileges")
PS = ("$ProgressPreference='SilentlyContinue'\ntry{$__u=New-Object Text.UTF8Encoding $false;"
      "[Console]::OutputEncoding=$__u;[Console]::InputEncoding=$__u;$OutputEncoding=$__u}catch{}\n",
      "\nif($?){exit 0}elseif($LASTEXITCODE){exit $LASTEXITCODE}else{exit 1}")


def _command(argv, cmd, shell):
    if (argv is None) == (cmd is None):
        raise Err("give argv or cmd, not both" if argv is not None else "give argv or cmd")
    if argv is not None:
        if shell is not None:
            raise Err("shell applies to cmd; argv runs with no shell")
        if not argv:
            raise Err("argv is empty")
        return [str(x) for x in argv], None
    name = {"pwsh": "powershell", "powershell.exe": "powershell", "pwsh.exe": "powershell", "cmd.exe": "cmd",
            "/bin/sh": "sh", "sh.exe": "sh", "bash.exe": "bash"}.get(str(shell or "").strip().lower(),
                                                                     str(shell or "").strip().lower())
    name = name or ("powershell" if IS_WIN else "sh")
    if name == "powershell":
        exe = shutil.which("pwsh") or (shutil.which("powershell") if IS_WIN else None)
        if not exe:
            raise Err("PowerShell is not installed on this worker")
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", PS[0] + cmd + PS[1]], name
    if name == "cmd":
        if not IS_WIN:
            raise Err("cmd.exe only exists on Windows workers")
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", cmd], name
    if name not in ("sh", "bash"):
        raise Err("shell must be one of sh, bash, powershell, cmd")
    if IS_WIN or name == "bash":
        exe = shutil.which(name)
        if not exe or (IS_WIN and re.search(r"[\\/](System32|WindowsApps)[\\/]", exe, re.I)):
            raise Err(f"no {name} on this worker" + (" (install Git for Windows; WSL's bash is not used)" if IS_WIN
                                                      else ""))
        return [exe, "-c", cmd], name
    return ["/bin/sh", "-c", cmd], name


def _drain(pipe, out: list) -> None:
    buf, cut = bytearray(), False
    while True:
        chunk = pipe.read(65536)
        if not chunk:
            break
        cut |= len(buf) + len(chunk) > OUT_MAX
        buf += chunk[:max(0, OUT_MAX - len(buf))]
    out.extend([bytes(buf), cut])


def exec_(argv=None, cmd=None, stdin=None, timeout=30.0, cwd=None, env=None, shell=None):
    command, ran = _command(argv, cmd, shell)
    child_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}} if env else None
    extra = {"creationflags": 0x08000000} if IS_WIN else {"start_new_session": True}
    try:
        proc = subprocess.Popen(command, cwd=cwd, env=child_env, stdin=subprocess.PIPE if stdin is not None else
                                subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **extra)
    except FileNotFoundError:
        raise Err(f"not found: {cwd if cwd and not os.path.isdir(cwd) else command[0]}") from None
    except NotADirectoryError:
        raise Err(f"not a directory: {cwd}") from None
    out, err = [], []
    readers = [threading.Thread(target=_drain, args=(p, o)) for p, o in ((proc.stdout, out), (proc.stderr, err))]
    for t in readers:
        t.start()
    if stdin is not None:
        try:
            proc.stdin.write(stdin.encode())
            proc.stdin.close()
        except OSError:
            pass
    deadline = time.monotonic() + float(timeout)
    try:
        code = proc.wait(timeout=float(timeout))
        for t in readers:            # a child that outlives the process holds its output open
            t.join(max(0.0, deadline - time.monotonic()))
        if any(t.is_alive() for t in readers):
            raise subprocess.TimeoutExpired(command, timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL) if not IS_WIN else proc.kill()
        except OSError:
            pass
        raise Err(f"timed out after {float(timeout):g}s and was killed; anything it already did "
                  "still happened. Use shell.open for long jobs.") from None
    stderr = err[0].decode("utf-8", "replace")
    reply = {"ok": code == 0, "code": code, "stdout": out[0].decode("utf-8", "replace"), "stderr": stderr}
    if out[1] or err[1]:
        reply["truncated"] = True
    if ran and (ran != "sh" or IS_WIN):
        reply["shell"] = ran
    if code == 126 or (code != 0 and any(m in stderr.lower() for m in DENIED)):
        reply["denied"] = True
    return reply


def which(name):
    exts = (".exe", ".cmd") if IS_WIN and not os.path.splitext(name)[1] else ()
    return next((found for c in (name, *(name + e for e in exts)) for found in [shutil.which(c)] if found), None)


def env(name=None, prefix=None, default=None):
    if (name is None) == (prefix is None):
        raise Err("give name or prefix")
    if name is not None:
        return default if name not in os.environ else "***" if SECRET.search(name) else os.environ[name]
    return {k: ("***" if SECRET.search(k) else v) for k, v in sorted(os.environ.items()) if k.startswith(prefix)}


# -- files (rook/plugins/file: the same contract) ----------------------------------

def _error(e: OSError, path) -> Err:
    what = {FileNotFoundError: "not found", IsADirectoryError: "is a directory", NotADirectoryError: "not a directory",
            PermissionError: "permission denied", FileExistsError: "exists"}.get(type(e))
    return Err(f"{what}: {path}" if what else f"{path}: {e.strerror or e}")


def _meta(s) -> dict:
    kind = ("dir" if st_.S_ISDIR(s.st_mode) else "symlink" if st_.S_ISLNK(s.st_mode)
            else "file" if st_.S_ISREG(s.st_mode) else "other")
    return {"type": kind, "size": s.st_size, "modified": s.st_mtime, "mode": oct(st_.S_IMODE(s.st_mode))}


def _codec(encoding):
    try:
        codecs.lookup(encoding)
    except LookupError:
        raise Err(f"unknown encoding: {encoding}") from None


def read(path, encoding="utf-8", max_bytes=8 * 1024 * 1024):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as f:
            s = os.fstat(fd)
            if not st_.S_ISREG(s.st_mode):
                raise Err(f"not a regular file: {path}")
            if hasattr(os, "set_blocking"):
                os.set_blocking(fd, True)
            raw = f.read(max_bytes + 1)
    except OSError as e:
        raise _error(e, path) from None
    out = {"size": s.st_size, "encoding": encoding}
    if len(raw) > max_bytes:
        raw, out["truncated"] = raw[:max_bytes], True
    if encoding == "base64":
        out["content"] = base64.b64encode(raw).decode("ascii")
    else:
        _codec(encoding)
        out["content"] = raw.decode(encoding, errors="replace")
    return out


def write(path, content, encoding="utf-8", append=False, create_parents=False):
    if encoding == "base64":
        try:
            raw = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError):
            raise Err("content is not base64") from None
    else:
        _codec(encoding)
        try:
            raw = content.encode(encoding)
        except UnicodeEncodeError as e:
            raise Err(f"content can't be written as {encoding}: {e.reason}") from None
    try:
        if create_parents:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab" if append else "wb") as f:
            n = f.write(raw)
    except OSError as e:
        raise _error(e, path) from None
    return {"path": str(Path(path)), "written": n}


def list_(path, recursive=False, hidden=True, limit=5000):
    p = Path(path)
    if not p.is_dir():
        raise Err(f"{'not a directory' if p.exists() else 'not found'}: {path}")
    entries = []
    try:
        for child in (p.rglob("*") if recursive else p.iterdir()):
            rel = child.relative_to(p)
            if not hidden and any(part.startswith(".") for part in rel.parts):
                continue
            if len(entries) >= limit:
                return {"entries": entries, "truncated": True}
            try:
                meta = _meta(child.lstat())
            except OSError:
                continue
            entries.append({"name": child.name, **meta, **({"path": str(rel)} if recursive else {})})
    except OSError as e:
        raise _error(e, path) from None
    return {"entries": entries}


def search(pattern, path=".", recursive=True, glob="*", ignore_case=False, limit=100):
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise Err(f"bad pattern: {e}") from None
    root = Path(path)
    if not root.exists():
        raise Err(f"not found: {path}")
    files = iter([root]) if root.is_file() else (
        f for f in (root.rglob(glob) if recursive else root.glob(glob)) if f.is_file())
    deadline, matches = time.monotonic() + 50, []
    for fp in files:
        if time.monotonic() > deadline:
            return {"matches": matches, "truncated": True, "timed_out": True}
        try:
            with open(fp, "rb") as f:
                if b"\x00" in f.read(4096):
                    continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                for n, line in enumerate(f, 1):
                    if rx.search(line):
                        if len(matches) >= limit:
                            return {"matches": matches, "truncated": True}
                        matches.append({"path": str(fp), "line": n, "text": line.rstrip("\r\n")[:1000]})
        except OSError:
            continue
    return {"matches": matches}


def stat(path):
    try:
        return {"exists": True, **_meta(Path(path).lstat())}
    except FileNotFoundError:
        return {"exists": False}
    except OSError as e:
        raise _error(e, path) from None


CAPS = {"shell.exec": exec_, "shell.which": which, "shell.env": env, "file.read": read, "file.write": write,
        "file.list": list_, "file.search": search, "file.stat": stat}


# -- enroll (spec 12), the same flow and files as the other workers ----------------------

def _post(url: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "rook-worker-enroll/0.5"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return {**json.loads(e.read() or b"{}"), "ok": False}
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Err(f"can't reach the hub at {url}: {getattr(e, 'reason', e)}") from None


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def enroll(target: str, code=None, name=None) -> None:
    from urllib.parse import parse_qs, urlsplit
    pinned = None
    if target.startswith("rook://enroll"):
        q = parse_qs(urlsplit(target).query)
        target, code, pinned = q.get("url", [""])[0], code or q.get("code", [None])[0], q.get("hub", [None])[0]
    url = (target if "://" in target else f"https://{target}").rstrip("/")
    host = urlsplit(url).hostname or ""
    if not url.startswith("https://") and host not in ("localhost", "127.0.0.1", "::1"):
        raise Err(f"{url} is plain http: the band key would cross the network in the clear; use https")
    cfg = load(HOME / "worker.yaml")
    if (HOME / cfg.get("anchor", "anchor.json")).exists():
        raise Err(f"this worker is enrolled already; to enroll again, move {HOME} aside")
    name = name or cfg.get("name") or socket.gethostname().split(".")[0] or "worker"
    if code:
        got = _post(url, "/enroll/claim", {"code": code, "name": name})
    else:
        start = _post(url, "/enroll/ask", {"name": name})
        if not start.get("ok") or not isinstance(start.get("ticket"), str):
            raise Err(f"the hub refused: {start.get('error')}")
        pinned = pinned or start.get("hub")
        print(f"{name} is waiting for someone to let this machine in on the Workers page of hub {start.get('hub')}.",
              flush=True)
        deadline = time.monotonic() + 600
        while True:
            got = _post(url, "/enroll/poll", {"ticket": start["ticket"]})
            if not got.get("pending"):
                break
            if time.monotonic() > deadline:
                raise Err("nobody let this machine in in time")
            time.sleep(min(max(float(got.get("interval") or 2), 1), 10))
    if not got.get("ok"):
        raise Err(f"the hub refused: {got.get('error')}")
    b = got.get("bundle") or {}
    try:
        hub, band = b["hub"], b["band"]
        root = base64.b64decode(hub["root"].split(":", 1)[-1])
        assert hashlib.sha256(root).hexdigest()[:16] == hub["hub_id"], "the hub id is not its root key's"
        assert band_id(band["psk"]).hex() == band["id"], "the band key is for another band"
        assert band.get("relay"), "no UDP relay for the band (rook_worker.py speaks UDP only)"
        assert not pinned or hub["hub_id"] == pinned, f"the bundle is from hub {hub['hub_id']}, not {pinned}"
    except (KeyError, TypeError, ValueError, AssertionError) as e:
        raise Err(f"refused bundle: {e}") from None
    data = HOME / cfg.get("data", "data")
    _write(HOME / cfg.get("anchor", "anchor.json"),
           json.dumps({"hub": hub["hub_id"], "root": hub["root"], "band": band["id"]}) + "\n", 0o644)
    _write(data / "bands.json", json.dumps({band["id"]: {k: band[k] for k in ("psk", "relay", "ws_url")
                                                         if band.get(k)}}, indent=1))
    if b.get("services"):
        _write(data / "services.json", json.dumps(b["services"], indent=1))
    _write(data / "enrollment.json", json.dumps({"hub": hub["hub_id"], "url": hub.get("url") or url,
                                                 "owner": b.get("owner"), "at": int(time.time())}, indent=1))
    cfg.update(data=cfg.get("data", "data"), anchor=cfg.get("anchor", "anchor.json"), name=b["name"],
               worker_id=b["worker_id"])
    _write(HOME / "worker.yaml", json.dumps(cfg, indent=1) + "\n")
    print(f"Enrolled {b['name']} ({b['worker_id']}) with hub {hub['hub_id']}, owned by {b.get('owner')}.\n"
          f"Start it:  python3 {sys.argv[0]}")


def main(argv) -> int:
    try:
        if argv[:1] == ["enroll"] and len(argv) >= 2:
            opts = dict(zip(argv[2::2], argv[3::2]))
            if len(argv) % 2 or set(opts) - {"--code", "--name"}:
                raise Err("usage: rook_worker.py enroll <hub address> [--code CODE] [--name NAME]")
            enroll(argv[1], opts.get("--code"), opts.get("--name"))
        elif not argv:
            Worker().run()
        else:
            raise Err("usage: rook_worker.py [enroll <hub address> [--code CODE] [--name NAME]]")
    except Err as e:
        print(f"rook_worker.py: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


# -- manifests: canonical JSON of worker.yaml, shell and file, written by worker/gen_script_manifests.py
# -- BEGIN GENERATED MANIFESTS (do not edit) --
MANIFESTS = [
    '{"builtin":true,"caps":{"log":{"args":{"agent":{"type":"str"},"cap":{"doc":"Only caps starting with this (shell., tv.power).","type":"str"},"failed":{"default":false,"doc":"Only calls that failed.","type":"bool"},"limit":{"default":50,"max":500,"min":1,"type":"int"},"person":{"type":"str"},"since":{"min":0,"type":"float"}},"doc":"What this worker actually ran, from its own record (newest last): one entry per call a hub ticket let through, with cap, person, agent, session, why, ok or the error, and how long it took. Filter by cap prefix, person, agent, since (epoch seconds) or failures only.","effects":["sensitive"],"idempotent":true,"result":{"fields":{"entries":{"type":"list"}},"type":"map"},"risk":"read","scope":"self"},"ping":{"doc":"Liveness check. Answers {pong, at} (the worker\'s clock).","idempotent":true,"result":{"fields":{"at":{"type":"float"},"pong":{"type":"bool"}},"type":"map"},"risk":"read","scope":"self"},"restart":{"doc":"Restart this worker process in place with its current config. Every live terminal, process and browser session on it ends, so whoever runs them loses their work.","effects":["destructive"],"result":{"fields":{"restarting":{"type":"bool"}},"type":"map"},"risk":"admin","scope":"local"},"status":{"doc":"One read of this worker: name, id, version, core, OS facts, uptime, the bands it is on (with hub contact), plugins loaded and caps unavailable (with why), open streams, and its core release (running, current, previous, a trial waiting to prove itself, the last failure).","idempotent":true,"result":{"type":"map"},"risk":"read","scope":"self"},"update":{"args":{"bundle":{"doc":"The release\'s Drop descriptor: id, size, sha256.","type":"map"},"version":{"doc":"A kept core to go back to, or installed.","type":"str"}},"doc":"Change this worker\'s core. bundle (sent by update.rollout): pull a hub-signed release, check its signature and files, install its requirements, smoke-check it, then restart into it on trial; the new core must serve a call from its home hub within two minutes (and within three starts), or the worker drops it and restarts on the core it had. version: restart on a core this worker already keeps (worker.status release.kept), or \\"installed\\" for the installed package: going back by hand. One of the two. Refused while terminals or streams are open here.","effects":[],"result":{"fields":{"from":{"doc":"The core it replaces; blank: the installed one.","type":"str"},"restarting":{"type":"bool"},"version":{"doc":"The core it restarts on; blank: the installed one.","type":"str"}},"type":"map"},"risk":"admin","scope":"self","timeout":"20m"}},"doc":"Liveness, status, the local record of calls this worker ran, and restart.","license":"Apache-2.0","manifest":1,"package":"core","plugin":"worker","source":"https://github.com/Bake-Ware/rook","title":"Worker","version":"0.5.0"}',  # noqa: E501
    '{"caps":{"env":{"args":{"default":{"doc":"What a missing name returns.","type":"str"},"name":{"type":"str"},"prefix":{"type":"str"}},"doc":"One environment variable (name), or all starting with prefix. Credential-like names (TOKEN, SECRET, PASSWORD, *_KEY, ROOK_*) read ***.","effects":["sensitive"],"idempotent":true,"risk":"read","run":"scripts/shell.py:env","scope":"local"},"exec":{"args":{"argv":{"doc":"The program and its arguments, e.g. [git, status].","items":{"type":"str"},"type":"list"},"cmd":{"doc":"A command line for a shell (pipes, redirection, globbing).","type":"str"},"cwd":{"doc":"Working directory.","type":"path"},"env":{"doc":"Variables added to the worker\'s environment (strings).","type":"map"},"shell":{"doc":"The shell for cmd; blank: sh, or PowerShell on Windows.","enum":["sh","bash","powershell","pwsh","cmd"],"type":"str"},"stdin":{"doc":"Text fed to the process\'s standard input.","type":"str"},"timeout":{"default":30,"doc":"Seconds before the process is killed.","max":3600,"min":0.1,"type":"float"}},"doc":"Run a command and wait for it: argv (a list, no shell; prefer it) or cmd (a string for a shell: sh -c, PowerShell on Windows, or the shell named). Returns ok, code, stdout, stderr; denied=true when it looks like it needed elevation (ask again with elevation; it is never re-run on its own).","elevation":"may","examples":[{"args":{"argv":["uname","-a"]}},{"args":{"cmd":"df -h / | tail -1"}}],"result":{"fields":{"code":{"type":"int"},"denied":{"type":"bool"},"ok":{"type":"bool"},"shell":{"doc":"The shell that ran cmd (left out for sh on Linux).","type":"str"},"stderr":{"type":"str"},"stdout":{"type":"str"},"truncated":{"doc":"Output past 8 MiB per stream was dropped (the process ran on).","type":"bool"}},"type":"map"},"risk":"exec","run":"scripts/shell.py:exec_","scope":"local","timeout":"none"},"open":{"args":{"argv":{"items":{"type":"str"},"type":"list"},"cmd":{"type":"str"},"cols":{"default":120,"max":1000,"min":1,"type":"int"},"cwd":{"type":"path"},"env":{"type":"map"},"pty":{"default":true,"doc":"Run on a terminal (not on Windows yet).","type":"bool"},"rows":{"default":32,"max":1000,"min":1,"type":"int"},"shell":{"enum":["sh","bash","powershell","pwsh","cmd"],"type":"str"},"stdin":{"doc":"Text fed to the process\'s input first; with pty=false its input then ends (end of file).","type":"str"},"title":{"doc":"The stream\'s title; blank: the command.","type":"str"}},"doc":"Start a command (default: a login shell) as a live stream and return {stream}. pty=true runs it on a terminal (keystrokes, Ctrl-C, resize); pty=false on pipes, output merged (what proc.start was). Read and write it with stream.read / stream.write; it closes when the process exits, with its exit code.","elevation":"may","risk":"exec","run":"scripts/shell.py:open_","scope":"local","stream":{"durable":false,"input":true,"types":["bytes"]},"timeout":"30s"},"which":{"args":{"name":{"doc":"A program name, not a path.","pattern":"^[^/\\\\\\\\\\\\x00]+$","required":true,"type":"str"}},"doc":"Full path of a program on this worker\'s PATH, or null.","idempotent":true,"risk":"read","run":"scripts/shell.py:which","scope":"local"}},"doc":"Run commands on this worker, read its environment, and open live terminals.","docs":{"manual":"docs/manual.md"},"hooks":{"stop":"scripts/shell.py:stop"},"license":"Apache-2.0","manifest":1,"package":"devices","plugin":"shell","source":"https://github.com/Bake-Ware/rook","title":"Shell","version":"0.5.0"}',  # noqa: E501
    '{"caps":{"list":{"args":{"hidden":{"default":true,"doc":"Include dot files.","type":"bool"},"limit":{"default":5000,"max":100000,"min":1,"type":"int"},"path":{"required":true,"type":"path"},"recursive":{"default":false,"type":"bool"}},"doc":"A directory\'s entries: name, type (file, dir, symlink, other), size, modified, mode. recursive adds each entry\'s relative path.","idempotent":true,"risk":"read","run":"scripts/file.py:list_","scope":"local","timeout":"60s"},"read":{"args":{"encoding":{"default":"utf-8","doc":"A Python codec (utf-8, latin-1) or base64.","pattern":"^[A-Za-z0-9_-]{1,32}$","type":"str"},"max_bytes":{"default":8388608,"max":67108864,"min":0,"type":"int"},"path":{"required":true,"type":"path"}},"doc":"A file\'s content: text in `encoding`, or base64 (encoding=base64) for bytes. Reads at most max_bytes (8 MiB); a longer file says truncated.","effects":["sensitive"],"idempotent":true,"result":{"fields":{"content":{"type":"str"},"encoding":{"type":"str"},"size":{"doc":"The whole file\'s size.","type":"int"},"truncated":{"type":"bool"}},"type":"map"},"risk":"read","run":"scripts/file.py:read","scope":"local","timeout":"60s"},"search":{"args":{"glob":{"default":"*","type":"str"},"ignore_case":{"default":false,"type":"bool"},"limit":{"default":100,"max":10000,"min":1,"type":"int"},"path":{"default":".","type":"path"},"pattern":{"required":true,"type":"str"},"recursive":{"default":true,"type":"bool"}},"doc":"Lines matching a regex in the files under path (glob narrows them; binary files are skipped): {matches: [{path, line, text}]}. Stops at limit, or after 50 s, and says truncated.","effects":["sensitive"],"idempotent":true,"risk":"read","run":"scripts/file.py:search","scope":"local","timeout":"60s"},"stat":{"args":{"path":{"required":true,"type":"path"}},"doc":"Whether a path exists, and if so its type, size, modified time and mode (a symlink is reported as itself).","idempotent":true,"result":{"fields":{"exists":{"type":"bool"},"mode":{"type":"str"},"modified":{"type":"float"},"size":{"type":"int"},"type":{"type":"str"}},"type":"map"},"risk":"read","run":"scripts/file.py:stat","scope":"local"},"write":{"args":{"append":{"default":false,"type":"bool"},"content":{"required":true,"type":"str"},"create_parents":{"default":false,"type":"bool"},"encoding":{"default":"utf-8","pattern":"^[A-Za-z0-9_-]{1,32}$","type":"str"},"path":{"required":true,"type":"path"}},"doc":"Write content to a file (replacing it, or append=true). encoding=base64 writes bytes. create_parents makes missing directories.","result":{"fields":{"path":{"type":"str"},"written":{"doc":"Bytes written.","type":"int"}},"type":"map"},"risk":"exec","run":"scripts/file.py:write","scope":"local","timeout":"60s"}},"doc":"Read, write, list, search and stat files on this worker.","docs":{"manual":"docs/manual.md"},"license":"Apache-2.0","manifest":1,"package":"devices","plugin":"file","source":"https://github.com/Bake-Ware/rook","title":"Files","version":"0.5.1"}',  # noqa: E501
]
# -- END GENERATED MANIFESTS --

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
