"""The app and the worker plugin, over a local socket (paths.socket_path()):
JSON lines both ways. The plugin listens (it runs as long as the worker does);
the app connects, and reconnects when the worker restarts.

app -> plugin:   {"t": "hello", "version"}            once connected
                 {"t": "event", "event", "payload"}   an event to emit (remote.button, remote.voice...)
                 {"t": "state", "state": {...}}       what the app shows (for remote.state), on change
plugin -> app:   {"t": "press", "button"}             remote.press
                 {"t": "chat", "text"?, "who"?, "status"?, "clear"?}   remote.chat
                 {"t": "display", "text", "seconds"}  remote.display
                 {"t": "profiles"}                    the profiles changed (remote.ir): read them again
                 {"t": "hub", "ok": bool}             whether the worker is in touch with its hub
"""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from .paths import socket_path

MAX_LINE = 256 * 1024


def _lines(sock: socket.socket, on_msg: Callable[[dict], None], stop: threading.Event) -> None:
    buf = b""
    while not stop.is_set():
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        if len(buf) > MAX_LINE * 4:
            return
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                on_msg(msg)


class Client:
    """The app's end: connects, reconnects, sends without blocking the UI (a queue)."""

    def __init__(self, on_msg: Callable[[dict], None], path: Path | None = None, hello: dict | None = None):
        self.path = Path(path) if path else socket_path()
        self.on_msg = on_msg
        self.hello = hello or {}
        self.connected = False
        self.out: "queue.Queue[dict]" = queue.Queue(maxsize=200)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="rookmote-bridge", daemon=True)
        self.thread.start()

    def send(self, msg: dict) -> bool:
        """Queued for the plugin (False: not connected, or the queue is full)."""
        if not self.connected:
            return False
        try:
            self.out.put_nowait(msg)
            return True
        except queue.Full:
            return False

    def close(self) -> None:
        self.stop.set()

    def _run(self) -> None:
        while not self.stop.is_set():
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(str(self.path))
            except OSError:
                s.close()
                self.stop.wait(2.0)
                continue
            s.settimeout(0.2)
            self.connected = True
            reader = threading.Thread(target=_lines, args=(s, self.on_msg, self.stop), daemon=True)
            reader.start()
            try:
                s.sendall((json.dumps({"t": "hello", **self.hello}) + "\n").encode())
                while not self.stop.is_set() and reader.is_alive():
                    try:
                        msg = self.out.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    s.sendall((json.dumps(msg) + "\n").encode())
            except OSError:
                pass
            self.connected = False
            self.on_msg({"t": "hub", "ok": False, "app": "disconnected"})
            try:
                s.close()
            except OSError:
                pass
            self.stop.wait(1.0)


class Server:
    """The plugin's end: one app at a time (a newer one replaces it)."""

    def __init__(self, on_msg: Callable[[dict], None], path: Path | None = None):
        self.path = Path(path) if path else socket_path()
        self.on_msg = on_msg
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.conn: socket.socket | None = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        os.chmod(self.path, 0o600)                 # the user's own: the app runs as the same user
        self.sock.listen(2)
        self.sock.settimeout(0.5)
        self.thread = threading.Thread(target=self._accept, name="rookmote-plugin", daemon=True)
        self.thread.start()

    @property
    def connected(self) -> bool:
        return self.conn is not None

    def send(self, msg: dict) -> bool:
        with self.lock:
            c = self.conn
            if c is None:
                return False
            try:
                c.sendall((json.dumps(msg) + "\n").encode())
                return True
            except OSError:
                self.conn = None
                return False

    def close(self) -> None:
        self.stop.set()
        try:
            self.sock.close()
        finally:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def _accept(self) -> None:
        while not self.stop.is_set():
            try:
                c, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            c.settimeout(0.5)
            with self.lock:
                old, self.conn = self.conn, c
            if old is not None:
                try:
                    old.close()
                except OSError:
                    pass
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c: socket.socket) -> None:
        _lines(c, self.on_msg, self.stop)
        with self.lock:
            if self.conn is c:
                self.conn = None
        self.on_msg({"t": "gone"})


def wait_for(pred: Callable[[], bool], seconds: float = 3.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()
