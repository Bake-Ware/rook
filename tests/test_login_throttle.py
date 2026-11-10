"""Failed-login throttle (rook/core/throttle.py) and every place it guards a
secret: dashboard login form and Basic auth, account sign-in, MCP bearer
tokens, the OAuth /token endpoint, /healthz and the skill download."""
import asyncio
import base64
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import aiohttp
import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from rook.core import throttle as throttle_mod
from rook.core.throttle import LoginThrottle, client_keys, from_env


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(clock=None, **kw):
    kw.setdefault("max_failures", 3)
    kw.setdefault("window", 60)
    kw.setdefault("lockout", 30)
    kw.setdefault("lockout_max", 120)
    return LoginThrottle(clock=clock or Clock(), **kw)


# -- the throttle itself ---------------------------------------------------------

def test_locks_after_n_failures_and_backs_off_exponentially():
    clock = Clock()
    th = make(clock)
    assert th.failure("192.0.2.1") == 0 and th.failure("192.0.2.1") == 0
    assert th.retry_after("192.0.2.1") == 0
    assert th.failure("192.0.2.1") == 30
    assert th.retry_after("192.0.2.1") == 30
    clock.t += 10.5
    assert th.retry_after("192.0.2.1") == 20       # rounded up
    assert th.retry_after("192.0.2.2") == 0        # other addresses unaffected
    clock.t += 20
    assert th.retry_after("192.0.2.1") == 0
    # After a lockout, one more failure locks again, twice as long, capped.
    assert th.failure("192.0.2.1") == 60
    clock.t += 61
    assert th.failure("192.0.2.1") == 120
    clock.t += 121
    assert th.failure("192.0.2.1") == 120


def test_failures_while_locked_do_not_extend_or_log_again(caplog):
    clock = Clock()
    th = make(clock)
    with caplog.at_level(logging.WARNING, logger="rook.core.throttle"):
        for _ in range(3):
            th.failure("192.0.2.1", "test login")
        for _ in range(10):
            assert th.failure("192.0.2.1") == 0
    warnings = [r for r in caplog.records if "locked out" in r.getMessage()]
    assert len(warnings) == 1 and "192.0.2.1" in warnings[0].getMessage()
    assert "test login" in warnings[0].getMessage()
    assert th.retry_after("192.0.2.1") == 30


def test_success_resets_failures_and_backoff():
    clock = Clock()
    th = make(clock)
    th.failure("192.0.2.1"); th.failure("192.0.2.1")
    th.success("192.0.2.1")
    th.failure("192.0.2.1"); th.failure("192.0.2.1")
    assert th.retry_after("192.0.2.1") == 0
    th.failure("192.0.2.1")
    clock.t += 31
    th.success("192.0.2.1")
    # A fresh start: N failures again before a lockout, and back to 30 s.
    assert th.failure("192.0.2.1") == 0 and th.failure("192.0.2.1") == 0
    assert th.failure("192.0.2.1") == 30


def test_sliding_window_and_quiet_spell():
    clock = Clock()
    th = make(clock)
    th.failure("192.0.2.1"); th.failure("192.0.2.1")
    clock.t += 61                                  # those two fell out of the window
    assert th.failure("192.0.2.1") == 0
    th.failure("192.0.2.1")
    assert th.failure("192.0.2.1") == 30
    clock.t += 30 + 121                            # quiet longer than max(window, lockout_max)
    assert th.failure("192.0.2.1") == 0            # strikes forgotten


def test_memory_is_bounded_and_idle_entries_pruned():
    clock = Clock()
    th = make(clock, max_keys=100)
    for i in range(1000):
        th.failure(f"key-{i}")
    assert len(th) == 100
    assert th.retry_after("key-999") == 0 and "key-999" in th._entries
    clock.t += 200                                 # past window, lockout_max and the prune tick
    th.failure("fresh")
    assert len(th) == 1


def test_multiple_keys_lock_together():
    th = make()
    for _ in range(3):
        th.failure(("198.51.100.7", "203.0.113.9"))
    assert th.retry_after(("203.0.113.9",)) == 30
    assert th.retry_after(("192.0.2.200", "198.51.100.7")) == 30


def test_disabled_and_env_settings(monkeypatch):
    off = make(max_failures=0)
    for _ in range(50):
        off.failure("192.0.2.1")
    assert off.retry_after("192.0.2.1") == 0
    monkeypatch.setenv("ROOK_LOGIN_MAX_FAILURES", "7")
    monkeypatch.setenv("ROOK_LOGIN_WINDOW_SECS", "120")
    monkeypatch.setenv("ROOK_LOGIN_LOCKOUT_SECS", "10")
    monkeypatch.setenv("ROOK_LOGIN_LOCKOUT_MAX_SECS", "nonsense")
    th = from_env()
    assert (th.max_failures, th.window, th.lockout, th.lockout_max) == (7, 120, 10, 900)


def test_client_keys_trust_proxy_headers_only_from_loopback():
    hdr = {"X-Forwarded-For": "198.51.100.7", "CF-Connecting-IP": "203.0.113.9"}
    # A remote peer's headers are ignored: it can neither dodge nor frame.
    assert client_keys("192.0.2.66", hdr) == ("192.0.2.66",)
    # Behind a local proxy: the addresses the proxy reported.
    assert client_keys("127.0.0.1", hdr) == ("203.0.113.9", "198.51.100.7")
    assert client_keys("::1", {"X-Forwarded-For": "198.51.100.7"}) == ("198.51.100.7",)
    assert client_keys("::ffff:127.0.0.1", {"CF-Connecting-IP": "203.0.113.9"}) == ("203.0.113.9",)
    # X-Forwarded-For: the entry the nearest proxy appended, not what the client wrote.
    assert client_keys("127.0.0.1", {"X-Forwarded-For": "192.0.2.1, 198.51.100.7"}) == ("198.51.100.7",)
    assert client_keys("127.0.0.1", {"X-Forwarded-For": "198.51.100.7, 127.0.0.1"}) == ("198.51.100.7",)
    # No usable header: the peer itself.
    assert client_keys("127.0.0.1", {}) == ("127.0.0.1",)
    assert client_keys("127.0.0.1", {"CF-Connecting-IP": "not-an-ip"}) == ("127.0.0.1",)
    assert client_keys(None) == ("unknown",)
    # IPv6 counts per /64.
    assert client_keys("2001:db8:1:2::5") == client_keys("2001:db8:1:2:ffff::1") == ("2001:db8:1:2::/64",)


def test_remote_peer_cannot_frame_or_dodge_with_forwarded_headers():
    th = make()
    for i in range(3):
        th.failure(client_keys("192.0.2.66", {"X-Forwarded-For": f"192.0.2.{i + 10}",
                                              "CF-Connecting-IP": "192.0.2.10"}))
    assert th.retry_after(client_keys("192.0.2.66")) == 30            # the attacker is locked
    assert th.retry_after(client_keys("192.0.2.10")) == 0             # the named victim is not
    assert th.retry_after(client_keys("127.0.0.1", {"X-Forwarded-For": "192.0.2.10"})) == 0


# -- dashboard -------------------------------------------------------------------

SETUP = {"band_name": "b", "band_psk": "alpha-bravo-charlie-delta-echo",
         "hub_public": "hub.example.com:443", "pyz_domain": "hub.example.com"}


def _basic(password):
    return {"Authorization": "Basic " + base64.b64encode(f"owner:{password}".encode()).decode()}


# -- MCP side --------------------------------------------------------------------


def _client(app, peer="192.0.2.10"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 1234)),
                             base_url="http://127.0.0.1:8765")


class FakeBand:
    workers: dict = {}

    async def call(self, *a, **k):
        return {"ok": True, "result": {}}


STATIC = "static-token-0123456789abcdef"
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "pytest", "version": "1"}}}


# -- merged hub: one throttle for both halves --------------------------------------

def free_port(kind=None) -> int:
    import socket
    with socket.socket(socket.AF_INET, kind or socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EchoRelay(asyncio.DatagramProtocol):
    """Stands in for the relay; ignores band traffic."""

    def connection_made(self, transport):
        self.transport = transport


