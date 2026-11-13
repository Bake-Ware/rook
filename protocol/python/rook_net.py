"""The dumb network primitives (D3): one HTTP request, one TCP send, one UDP send.

They know nothing of any device. Every worker serves them as the caps net.http,
net.tcp and net.udp (rook_worker.py), and the hub runs the same functions itself
for a device on a direct route (D4), so both answer alike. Standard library only.
Each function blocks; callers run it in a thread.

Bytes go out as `body` / `data` (text, sent as UTF-8) or `body_base64` /
`data_base64`, and come back the same way: as text when they are UTF-8, else base64.
How long to wait for an answer is an argument, `wait`, in seconds.
"""

import base64
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


class _HTTPRedirectsOnly(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to http or https. A redirect anywhere else is not
    followed: the 3xx response itself is the answer (D3)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme.lower() not in ("http", "https"):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Straight to the address (no proxy), and redirects followed only to http and https.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _HTTPRedirectsOnly)


def _outgoing(args, name):
    """The bytes to send, from `<name>` (text) or `<name>_base64`; None when neither is given."""
    if args.get(name) is not None:
        return str(args[name]).encode("utf-8")
    if args.get(name + "_base64") is not None:
        return base64.b64decode(args[name + "_base64"], validate=True)
    return None


def _incoming(data, name):
    try:
        return {name: data.decode("utf-8")}
    except UnicodeDecodeError:
        return {name + "_base64": base64.b64encode(data).decode("ascii")}


def http(args):
    """One HTTP request: {status, headers, body or body_base64}. An HTTP error status is an answer, not a failure.
    Only http and https, for the request and for any redirect it follows: this is an HTTP
    request, never a way to read files or reach other protocols (D3)."""
    scheme = urllib.parse.urlsplit(args["url"]).scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"net.http makes HTTP requests only: the url must start with http:// or https://, "
                         f"not {scheme or 'no scheme'}")
    request = urllib.request.Request(args["url"], data=_outgoing(args, "body"), method=args.get("method", "GET"),
                                     headers=args.get("headers") or {})
    try:
        response = _OPENER.open(request, timeout=args.get("wait"))
    except urllib.error.HTTPError as e:
        response = e
    with response:
        body = response.read()
        return {"status": response.status, "headers": dict(response.headers), **_incoming(body, "body")}


def tcp(args):
    """Connect, send, and return what comes back until the other end closes or `wait` seconds pass.
    With `wait` 0 it sends and returns at once, reading nothing: {data: null}."""
    deadline = time.monotonic() + args["wait"]
    received = b""
    with socket.create_connection((args["host"], args["port"]), timeout=args["wait"] or None) as sock:
        sock.sendall(_outgoing(args, "data") or b"")
        if args["wait"] <= 0:
            return {"data": None}
        while (left := deadline - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            received += chunk
    return _incoming(received, "data")


def udp(args):
    """Send one datagram; return the first one that comes back within `wait` seconds, or data null."""
    family = socket.getaddrinfo(args["host"], args["port"], type=socket.SOCK_DGRAM)[0][0]
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        sock.sendto(_outgoing(args, "data") or b"", (args["host"], args["port"]))
        if args["wait"] <= 0:
            return {"data": None}
        sock.settimeout(args["wait"])
        try:
            data, _ = sock.recvfrom(65536)
        except TimeoutError:
            return {"data": None}
    return _incoming(data, "data")


_TEXT = {"type": "string", "description": "What to send, as text (sent as UTF-8)."}
_BYTES = {"type": "string", "description": "What to send, as base64, for bytes that are not text."}
_WAIT = {"type": "number", "minimum": 0, "description": "Seconds to wait for the answer."}
_HOST = {"type": "string", "description": "Host name or IP address."}
_PORT = {"type": "integer", "minimum": 1, "maximum": 65535}

CAPS = {
    "net.http": (http, "Make one HTTP or HTTPS request and return its status, headers and body.", {
        "type": "object", "required": ["url"], "additionalProperties": False, "properties": {
            "url": {"type": "string"}, "method": {"type": "string", "description": "GET unless given."},
            "headers": {"type": "object", "additionalProperties": {"type": "string"}},
            "body": _TEXT, "body_base64": _BYTES,
            "wait": dict(_WAIT, description="Seconds to wait for the server; no limit unless given.")}}),
    "net.tcp": (tcp, "Open a TCP connection, send data, and return what comes back within wait seconds (0: send only).", {
        "type": "object", "required": ["host", "port", "wait"], "additionalProperties": False, "properties": {
            "host": _HOST, "port": _PORT, "data": _TEXT, "data_base64": _BYTES, "wait": _WAIT}}),
    "net.udp": (udp, "Send one UDP datagram and return the first reply within wait seconds (or none).", {
        "type": "object", "required": ["host", "port", "wait"], "additionalProperties": False, "properties": {
            "host": _HOST, "port": _PORT, "data": _TEXT, "data_base64": _BYTES, "wait": _WAIT}}),
}
