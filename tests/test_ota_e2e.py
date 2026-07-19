"""End-to-end in-band OTA: a real Worker + BandClient over a fake UDP hub relay
push a signed bundle across the band with telesthete Drop — no HTTP.

Proves the whole stack: TelestheteHubTransport fragmentation, the Drop bridge,
the worker's binary-handler routing, signature verification, and sha256 integ-
rity — stopping just short of the real swap/restart (stubbed).
"""

import asyncio
import base64
import hashlib
import os

import pytest

from nacl.signing import SigningKey

from rook.worker._build_info import BUILD
from rook.worker._update_verify import canonical_payload, verify_manifest as _real_verify
from rook.worker.core import Worker
from rook.worker.transports.telesthete_hub import TelestheteHubTransport
from rook.band_mcp.client import BandClient

PSK = "ota-e2e-band-psk"


class FakeHub(asyncio.DatagramProtocol):
    """Minimal telesthitium: relay each datagram to the other peers sharing its
    16-byte band_id prefix (learned from the packets themselves)."""

    def __init__(self):
        self.transport = None
        self.peers: dict[bytes, set] = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) < 16:
            return
        band = data[:16]
        self.peers.setdefault(band, set()).add(addr)
        for peer in self.peers[band]:
            if peer != addr:
                self.transport.sendto(data, peer)


def _signed_manifest(bundle: bytes, build: int):
    sk = SigningKey.generate()
    pub_b64 = base64.b64encode(sk.verify_key.encode()).decode()
    manifest = {
        "schema": 1, "build": build, "version": f"{build}.test",
        "filename": "band-worker.pyz",
        "sha256": hashlib.sha256(bundle).hexdigest(), "size": len(bundle),
    }
    manifest["sig"] = base64.b64encode(
        sk.sign(canonical_payload(manifest)).signature).decode()
    return manifest, pub_b64


@pytest.mark.asyncio
async def test_inband_ota_push_end_to_end(tmp_path, monkeypatch):
    loop = asyncio.get_running_loop()
    hub_transport, hub = await loop.create_datagram_endpoint(
        FakeHub, local_addr=("127.0.0.1", 0))
    hub_port = hub_transport.get_extra_info("socket").getsockname()[1]

    # A "bundle": bytes that span many Drop chunks + a partial tail.
    bundle = os.urandom(1024 * 40 + 123)
    manifest, pub_b64 = _signed_manifest(bundle, build=BUILD + 1)

    # Verify against the test key (the real baked-in key isn't ours to sign for).
    monkeypatch.setattr("rook.worker._update_verify.verify_manifest",
                        lambda m, pk=None: _real_verify(m, pub_b64))
    # Keep the received bundle out of the real home dir.
    monkeypatch.setattr("rook.worker.plugins.selfupdate._WORKER_DIR", tmp_path)

    # Worker: selfupdate only, over the hub relay.
    wt = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=hub_port)
    worker = Worker(transport=wt, enabled=["selfupdate"], name="e2e-worker",
                    announce_interval=1.0)
    su = next(p for p in worker.plugins if p.NAMESPACE == "worker")

    # Capture the staged bundle instead of swapping + restarting the test runner.
    swapped = {}

    async def _fake_swap(tmp, target, from_build):
        swapped["bytes"] = tmp.read_bytes()
        swapped["target"] = target
        return {"ok": True, "action": "updated", "to_build": target}

    monkeypatch.setattr(su, "_swap_and_restart", _fake_swap)
    # No real subprocess selftest.
    async def _ok_smoke(_p):
        return True
    monkeypatch.setattr(su, "_smoke_test", _ok_smoke)

    worker_task = asyncio.create_task(worker.run())

    client = BandClient(psk=PSK, hub_host="127.0.0.1", hub_port=hub_port)
    await client.start()

    try:
        await asyncio.sleep(0.5)  # let both register with the hub
        result = await client.push_update(
            worker.worker_id, bundle, manifest, drop_id=42,
            begin_timeout=10.0, transfer_timeout=30.0)
        assert result["ok"] is True, result
        assert result["stage"] == "transfer"
        assert result["verified"] is True

        # The worker received the exact bundle, verified it, and staged it.
        for _ in range(50):
            if "bytes" in swapped:
                break
            await asyncio.sleep(0.1)
        assert swapped.get("bytes") == bundle, "worker must stage the exact bundle"
        assert swapped["target"] == BUILD + 1
    finally:
        await client.stop()
        await worker.shutdown()
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):
            pass
        hub_transport.close()


@pytest.mark.asyncio
async def test_inband_ota_rejects_unsigned_manifest(tmp_path, monkeypatch):
    """A worker must refuse an in-band push whose manifest isn't validly signed —
    being on the band (knowing the PSK) is not enough."""
    loop = asyncio.get_running_loop()
    hub_transport, hub = await loop.create_datagram_endpoint(
        FakeHub, local_addr=("127.0.0.1", 0))
    hub_port = hub_transport.get_extra_info("socket").getsockname()[1]

    bundle = os.urandom(2048)
    manifest, _pub = _signed_manifest(bundle, build=BUILD + 1)
    manifest["sig"] = base64.b64encode(b"\x00" * 64).decode()  # bogus signature
    # Use the REAL verifier (baked-in key) — the bogus sig must fail closed.
    monkeypatch.setattr("rook.worker.plugins.selfupdate._WORKER_DIR", tmp_path)

    wt = TelestheteHubTransport(psk=PSK, hub_host="127.0.0.1", hub_port=hub_port)
    worker = Worker(transport=wt, enabled=["selfupdate"], name="e2e-worker2",
                    announce_interval=1.0)
    worker_task = asyncio.create_task(worker.run())
    client = BandClient(psk=PSK, hub_host="127.0.0.1", hub_port=hub_port)
    await client.start()
    try:
        await asyncio.sleep(0.5)
        result = await client.push_update(
            worker.worker_id, bundle, manifest, drop_id=7,
            begin_timeout=10.0, transfer_timeout=5.0)
        # begin fails closed → no transfer.
        assert result["ok"] is False
        assert result["stage"] == "begin"
    finally:
        await client.stop()
        await worker.shutdown()
        worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):
            pass
        hub_transport.close()
