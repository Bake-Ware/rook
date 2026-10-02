"""The conformance vectors (conformance/) against the Python reference.

* The committed vectors must be what ``conformance/generate.py`` produces
  from the current code (so a wire or core change shows up as a vector diff).
* The reference must pass its own vectors through the *verifying* direction
  (decrypt, reassemble, verify signatures), not just regenerate them.
* The example ports' offline vector suites run when asked:
  ``ROOK_PORTS=1`` plus node (>= 23.6) and/or cargo on PATH.

The live half (a candidate against a real hub) is in
tests/integration/test_it_conformance.py.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "conformance" / "vectors"


def load(name: str) -> dict:
    return json.loads((VECTORS / f"{name}.json").read_text(encoding="utf-8"))


def test_vectors_are_current():
    r = subprocess.run([sys.executable, str(ROOT / "conformance" / "generate.py"), "--check"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


def test_every_vector_names_the_spec_version():
    for path in VECTORS.glob("*.json"):
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["spec"] == "1.0" and doc["vector"] == path.stem


def test_reference_decrypts_and_reassembles_frames():
    from telesthete.protocol.crypto import BandCrypto
    from telesthete.protocol.fragment import Reassembler
    from telesthete.protocol.framing import unpack_packet
    for c in load("frames")["cases"]:
        crypto = BandCrypto(c["psk"])
        r, msg = Reassembler(), None
        for f in c["frames"]:
            pkt = unpack_packet(bytes.fromhex(f))
            assert pkt.band_id == crypto.band_id and pkt.channel_type == 2
            msg = r.feed(crypto.decrypt(pkt.sequence, pkt.ciphertext)) or msg
        assert msg.hex() == c["message"]
    for c in load("band_crypto")["cases"]:
        pt = BandCrypto(c["psk"]).decrypt(int(c["sequence"]), bytes.fromhex(c["ciphertext"]))
        assert pt.hex() == c["plaintext"]


def test_reference_verifies_signature_vectors():
    from rook.core import authz
    v = load("signatures")
    for c in v["grants"]:
        x = c["context"]
        ok, _ = authz.verify_grant(c["grant"], x["anchors"], band=x["band"], now=x["now"],
                                   revoked=x["revoked"], worker_id=x.get("worker_id"))
        assert ok == c["ok"], c["label"]
    for c in v["announces"]:
        x = c["context"]
        assert sorted(authz.held_roles(c["announce"], x["anchors"], band=x["band"],
                                       now=x["now"])) == c["held_roles"], c["label"]
    for c in v["tickets"]:
        e = c["envelope"]
        ok, _ = authz.verify_ticket(c["ticket"], cap=e["cap"], target=e["target"],
                                    msg_id=e["msg_id"], args=e["args"],
                                    keys=c["grants_by_kid"], now=c["now"])
        assert ok == c["ok"], c["label"]
    for c in load("canonical")["cases"]:
        assert authz.canonical(c["value"]).decode() == c["canonical"]


def test_reference_placement_and_tiers():
    from rook.core.authz import effective_tier
    from rook.core.facts import NodeFacts, evaluate_placement
    v = load("placement")
    for c in v["cases"]:
        for name, want in c["results"].items():
            n = v["nodes"][name]
            facts = NodeFacts(node_id=name, roles=frozenset(n["roles"]), hw=n["hw"])
            assert evaluate_placement(c["expr"], facts) is want, (c["expr"], name)
    for c in load("tiers")["cases"]:
        assert effective_tier(c["cap"], c["declared"], c["override"], c["lower"]) == c["tier"]


def _ports_enabled() -> bool:
    return os.environ.get("ROOK_PORTS", "") not in ("", "0")


def _node_ok() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    out = subprocess.run([node, "-p", "process.versions.node"], capture_output=True, text=True)
    major, minor = (int(x) for x in out.stdout.strip().split(".")[:2])
    return (major, minor) >= (23, 6)   # native TypeScript type stripping


@pytest.mark.skipif(not _ports_enabled(), reason="port suites are opt-in: set ROOK_PORTS=1")
def test_typescript_port_passes_the_vectors():
    if not _node_ok():
        pytest.skip("node >= 23.6 not found")
    port = ROOT / "examples" / "ports" / "typescript"
    r = subprocess.run(["node", "--test", "test/vectors.test.ts"], cwd=port,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(not _ports_enabled(), reason="port suites are opt-in: set ROOK_PORTS=1")
def test_rust_port_passes_the_vectors():
    if not shutil.which("cargo"):
        pytest.skip("cargo not found")
    port = ROOT / "examples" / "ports" / "rust"
    r = subprocess.run(["cargo", "test", "--release", "--quiet"], cwd=port,
                       capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
