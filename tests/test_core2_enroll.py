"""Enrollment (rook/core2/enroll; spec 12): a hub started in-process with a
loopback band, the device side talking to its HTTP door, local network
discovery on a loopback port, the hub's ``init`` and ``recovery-key``.

Every test runs under a hard timeout; HTTP requests run in threads with
their own socket timeouts."""
from __future__ import annotations

import asyncio
import json
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from rook.core2 import wire  # noqa: E402
from rook.core2.enroll import device, mdns  # noqa: E402
from rook.core2.enroll.hub import CODE_SECS, MAX_PENDING, Enrollment, check_options, recovery_key  # noqa: E402
from rook.core2.hub.__main__ import main as hub_main  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback, load_config  # noqa: E402
from rook.core2.transport import band_of  # noqa: E402
from rook.core2.worker import runtime as worker_runtime  # noqa: E402
from rook.core2.worker.security import TrustStore  # noqa: E402

PSK = "e2e-enroll-test-band-key-0123456789"
BAND = band_of(PSK)
OWNER = Caller(person="person:bake")


def run(coro, timeout=30):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def config(tmp_path: Path, **over) -> Config:
    return Config(**{"data": tmp_path / "data",
                     "users": {"bake": {"preset": "owner"}, "ann": {"preset": "adult"}, "kid": {"preset": "child"}},
                     "agents": {"claude": {"grants": ["*"], "max_risk": "exec"}, "root": {"grants": ["*"]}},
                     "doors": {"http": {"bind": "127.0.0.1:0"}},
                     "transport": {"bands": [{"psk": PSK, "relay": "127.0.0.1:7474"}]}, **over})


async def started(tmp_path: Path, **over) -> tuple[Hub, dict]:
    hub = Hub(config(tmp_path, **over), transport=Loopback())
    await hub.start()
    tokens = {"bake": (hub.data / "owner.token").read_text().strip()}
    for name, caller in (("ann", Caller(person="person:ann")), ("kid", Caller(person="person:kid")),
                         ("claude", Caller(agent="agent:claude")),
                         ("claude-for-bake", Caller(person="person:bake", agent="agent:claude")),
                         ("root-for-bake", Caller(person="person:bake", agent="agent:root"))):
        tokens[name] = hub.add_token(caller)
    return hub, tokens


def url(hub: Hub) -> str:
    return f"http://127.0.0.1:{hub.doors[0].port}"


async def http(hub: Hub, method: str, path: str, body=None, token: str | None = None):
    def go():
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url(hub) + path, data=data, method=method)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read())}
    return await asyncio.to_thread(go)


async def until(pred, timeout=10.0):
    for _ in range(int(timeout / 0.02)):
        got = await pred()
        if got:
            return got
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_a_qr_code_enrolls_a_device_once_and_install_writes_what_the_worker_reads(tmp_path):
    async def go():
        hub, tok = await started(tmp_path)
        try:
            got = await http(hub, "POST", "/enroll/code", {}, tok["bake"])
            assert got["ok"] and got["hub"] == hub.hub_id and got["qr"].startswith("rook://enroll?")
            target, code, pinned = device.parse_target(got["qr"])
            assert (target, code, pinned) == (url(hub), got["code"], hub.hub_id)
            bundle = await asyncio.to_thread(device.sign_in, target, "laptop", code, pinned)
            again = await http(hub, "POST", "/enroll/claim", {"code": code, "name": "laptop"})
            enrolled = json.loads((hub.data / "enrolled.json").read_text())
            entries = hub.journal.query(kind="enroll", viewer={"owner": True}, limit=5)
            workers = await http(hub, "GET", "/workers", None, tok["bake"])
            return hub, bundle, again, enrolled, entries, workers
        finally:
            await hub.stop()
    hub, bundle, again, enrolled, entries, workers = run(go())
    assert bundle["hub"] == {"hub_id": hub.hub_id, "root": wire.pub_text(hub.root), "url": None}
    assert bundle["band"] == {"id": BAND, "psk": PSK, "relay": "127.0.0.1:7474"}
    assert bundle["owner"] == "person:bake" and bundle["name"] == "laptop" and bundle["core"] == "2.0"
    assert again == {"ok": False, "error": "unknown or expired code"}         # one use
    assert enrolled[bundle["worker_id"]]["owner"] == "person:bake"
    assert entries[0]["worker"] == bundle["worker_id"] and entries[0]["person"] == "person:bake"

    cfg_path = tmp_path / "w" / "worker.yaml"
    cfg_path.parent.mkdir()
    cfg_path.write_text("plugins: [./plugins]\nlog: {format: json}\n")          # kept
    device.install(cfg_path, bundle, "http://hub")
    raw = yaml.safe_load(cfg_path.read_text())
    assert raw["plugins"] == ["./plugins"] and raw["log"] == {"format": "json"}
    assert (raw["name"], raw["worker_id"], raw["anchor"], raw["data"]) == \
        ("laptop", bundle["worker_id"], "anchor.json", "data")
    anchor = cfg_path.parent / "anchor.json"
    assert stat.S_IMODE(anchor.stat().st_mode) == 0o644
    assert stat.S_IMODE((cfg_path.parent / "data" / "bands.json").stat().st_mode) == 0o600
    store = TrustStore(anchor, None, bundle["worker_id"])
    assert (store.home.hub, store.home.band) == (hub.hub_id, BAND)
    cfg = worker_runtime.load_config(cfg_path)
    assert cfg.worker_id == bundle["worker_id"] and cfg.name == "laptop"
    w = worker_runtime.Worker(cfg)
    assert w._joined() == {BAND: {"psk": PSK, "relay": "127.0.0.1:7474"}}
    assert json.loads((cfg_path.parent / "data" / "enrollment.json").read_text())["url"] == "http://hub"

    with pytest.raises(device.EnrollError, match="already hub"):
        device.install(cfg_path, bundle, "http://hub")
    (cfg_path.parent / "data" / "memberships.json").write_text("{}")
    device.install(cfg_path, {**bundle, "worker_id": "a" * 32}, "http://hub", force=True)
    assert not (cfg_path.parent / "data" / "memberships.json").exists()     # re-anchoring drops them (9.5)
    assert yaml.safe_load(cfg_path.read_text())["worker_id"] == "a" * 32


def test_a_device_code_waits_for_the_person_and_can_be_refused(tmp_path):
    said = []

    async def go():
        hub, tok = await started(tmp_path)
        try:
            task = asyncio.create_task(asyncio.to_thread(
                device.sign_in, url(hub), "tablet", say=said.append, sleep=lambda s: __import__("time").sleep(0.05)))
            async def listed():   # the device prints the code only once the hub's answer is back
                return said and await _pending(hub, tok["ann"])
            pending = await until(listed)
            code = pending[0]["user_code"]
            assert pending[0]["name"] == "tablet" and said and code in said[0] and hub.hub_id in said[0]
            kid = await http(hub, "POST", "/enroll/approve", {"code": code}, tok["kid"])
            ok = await http(hub, "POST", "/enroll/approve", {"code": code.lower().replace("-", " ")}, tok["ann"])
            bundle = await task

            refused = asyncio.create_task(asyncio.to_thread(
                device.sign_in, url(hub), "other", say=said.append, sleep=lambda s: __import__("time").sleep(0.05)))
            code2 = (await until(lambda: _pending(hub, tok["ann"])))[0]["user_code"]
            no = await http(hub, "POST", "/enroll/approve", {"code": code2, "decision": "refuse"}, tok["bake"])
            with pytest.raises(device.EnrollError, match="refused by the person"):
                await refused
            unknown = await http(hub, "POST", "/enroll/approve", {"code": "BCDF-GHJK"}, tok["bake"])
            return kid, ok, bundle, no, unknown
        finally:
            await hub.stop()
    kid, ok, bundle, no, unknown = run(go())
    assert kid == {"ok": False, "error": "denied: person may not set up machines"}
    assert ok == {"ok": True, "name": "tablet", "decision": "approve"}
    assert bundle["owner"] == "person:ann" and bundle["name"] == "tablet"    # the approver owns it
    assert no["ok"] and unknown == {"ok": False, "error": "unknown or expired code"}


async def _pending(hub, token):
    got = await http(hub, "GET", "/enroll/pending", None, token)
    return got.get("devices")


def test_who_may_enroll_see_the_recovery_key_and_invite(tmp_path):
    async def go():
        hub, tok = await started(tmp_path)
        try:
            out = {}
            for who in ("bake", "ann", "kid", "claude", "claude-for-bake", "root-for-bake"):
                out[who] = [(await http(hub, m, p, b, tok[who])).get("error")
                            for m, p, b in (("POST", "/enroll/code", {}), ("GET", "/recovery-key", None),
                                            ("POST", "/bands/invite", {"ceiling": {"grants": ["e2e.*"]}}))]
            key = await http(hub, "GET", "/recovery-key", None, tok["bake"])
            open_ = await http(hub, "POST", "/enroll/device", {"name": "rook"})
            noauth = await http(hub, "POST", "/enroll/code", {})
            return out, key, open_, noauth, hub
        finally:
            await hub.stop()
    out, key, open_, noauth, hub = run(go())
    alone = "denied: only an owner, signed in as themselves"
    assert out["bake"] == [None, None, None]
    assert out["ann"] == [None, alone, alone]
    assert out["kid"] == ["denied: person may not set up machines", alone, alone]
    assert out["claude"] == ["denied: setting up a machine needs a person", alone, alone]
    # an agent never sets up a machine, even for an owner with full rights: the
    # code it gets would claim a bundle holding the band key
    person = "denied: setting up a machine is for the person, not an agent"
    assert out["claude-for-bake"] == [person, alone, alone]
    assert out["root-for-bake"] == [person, alone, alone]
    assert key["recovery_key"] == recovery_key(hub.data) == \
        (hub.data / "keys" / "recovery.key").read_text().strip()
    assert len(key["recovery_key"]) == 39 and key["recovery_key"].count("-") == 7
    assert open_["ok"] is False and "name" in open_["error"]
    assert noauth["status"] == 401


def test_codes_expire_and_waiting_devices_are_bounded(tmp_path):
    now = [1_800_000_000.0]
    hub = Hub(config(tmp_path), transport=Loopback(), clock=lambda: now[0])
    hub.data, hub.hub_id = Path(tmp_path / "data"), "0" * 16
    hub.data.mkdir()
    e = Enrollment(hub, {})
    hub.identities = __import__("rook.core2.hub.identity", fromlist=["x"]).Identities()
    hub.identities.add_user("bake", "owner")

    async def go():
        code = (await e.new_code(OWNER))["code"]
        dev = await e.device_start({"name": "box"})
        now[0] += CODE_SECS + 1
        late = await e.claim({"code": code, "name": "box"})
        poll = await e.device_poll({"device_code": dev["device_code"]})
        for _ in range(MAX_PENDING):
            assert (await e.device_start({"name": "box"}))["ok"]
        busy = await e.device_start({"name": "box"})
        return late, poll, busy
    late, poll, busy = run(go())
    assert late["error"] == "unknown or expired code"
    assert poll["error"] == "unknown or expired device code"
    assert busy["error"].startswith("busy:")


def test_membership_requests_are_checked_and_band_keys_resolve_for_owners_only(tmp_path):
    async def go():
        hub, tok = await started(tmp_path)
        other, otok = await started(tmp_path / "other", transport={"bands": [{"psk": "the-other-band-key-abcdef",
                                                                             "relay": "10.0.0.2:7474"}]})
        try:
            invite = (await http(other, "POST", "/bands/invite", {"ceiling": {"grants": ["e2e.ping"]}},
                                 otok["bake"]))["invite"]
            own = (await http(hub, "POST", "/bands/invite", {"role": "co-owner"}, tok["bake"]))["invite"]
            out = {
                "kid": await http(hub, "POST", "/memberships", {"worker": "x", "invite": invite}, tok["kid"]),
                "unknown": await http(hub, "POST", "/memberships", {"worker": "nope", "invite": invite}, tok["bake"]),
            }
            hub.dispatcher.workers["w" * 32] = __import__("rook.core2.hub.dispatch", fromlist=["x"]).Worker(
                "w" * 32, "kaiju", BAND, {}, {"grants": ["*"]})
            for name, body in (("own", {"invite": own}), ("forged", {"invite": {**invite, "hub": "0" * 16}}),
                               ("badpsk", {"invite": {**invite, "connect": {**invite["connect"], "psk": "x"}}}),
                               ("noband", {"band": "1" * 32, "role": "guest", "ceiling": {"grants": []}}),
                               ("homerole", {"role": "guest"}), ("noceiling", {"invite": {**invite, "ceiling": None}})):
                out[name] = await http(hub, "POST", "/memberships", {"worker": "kaiju", "why": "test", **body},
                                       tok["bake"])
            ref = "{{secret:band-psk-%s}}" % invite["band"]
            resolved = hub.enroll.resolve({"connect": {"psk": ref}, "x": [ref, "keep"]}, OWNER, "core.trust.set")
            with pytest.raises(ValueError, match="only an owner"):
                hub.enroll.resolve({"psk": ref}, Caller(person="person:ann"), "core.trust.set")
            # an agent acting for the owner, or any other cap, never gets a band key
            with pytest.raises(ValueError, match="only an owner"):
                hub.enroll.resolve({"psk": ref}, Caller(person=OWNER.person, agent="agent:claude"), "core.trust.set")
            with pytest.raises(ValueError, match="only in core.trust.set"):
                hub.enroll.resolve({"argv": ["echo", ref]}, OWNER, "shell.exec")
            return invite, out, resolved
        finally:
            await other.stop()
            await hub.stop()
    invite, out, resolved = run(go())
    assert invite["role"] == "guest" and invite["connect"] == {"psk": "the-other-band-key-abcdef",
                                                               "relay": "10.0.0.2:7474"}
    assert out["kid"]["error"].startswith("denied")
    assert out["unknown"]["error"] == "unknown worker: nope"
    assert out["own"]["error"] == "bad invite: it is for this hub's own band"
    assert out["forged"]["error"] == "bad invite: its hub is not key_id(root)"
    assert out["badpsk"]["error"] == "bad invite: its PSK is for another band"
    assert out["noband"]["error"].startswith("unknown band")
    assert out["homerole"]["error"].endswith("the role is home")
    assert "ceiling" in out["noceiling"]["error"]
    # the bad-ceiling invite was still a valid invite: its band is known now, its key resolves
    assert resolved == {"connect": {"psk": "the-other-band-key-abcdef"}, "x": ["the-other-band-key-abcdef", "keep"]}


def test_local_network_discovery_finds_the_hub(tmp_path):
    async def go():
        r = mdns.Responder("https://rook.example:8443", "1a2b3c4d5e6f7a8b", port=0, bind="127.0.0.1", group=False)
        await r.start()
        try:
            return await asyncio.to_thread(mdns.discover, 2.0, ("127.0.0.1", r.port))
        finally:
            await r.stop()
    found = run(go())
    assert found == [{"url": "https://rook.example:8443", "hub": "1a2b3c4d5e6f7a8b", "name": "Rook hub 1a2b3c4d"}]
    assert device.find_hub(lambda: found) == ("https://rook.example:8443", "1a2b3c4d5e6f7a8b")
    with pytest.raises(device.EnrollError, match="several"):
        device.find_hub(lambda: found * 2)
    with pytest.raises(device.EnrollError, match="no Rook hub"):
        device.find_hub(list)
    # a query that isn't for us, and a response, get nothing
    assert mdns.asks_for_us(mdns.query()) == (True, True)
    assert mdns.asks_for_us(mdns.answer(0, "https://x", "0" * 16, False)) == (False, False)
    assert mdns.parse(mdns.query()) == []


def test_bundles_travel_over_tls_and_must_hang_together():
    for ok in ("https://rook.example", "http://127.0.0.1:8740", "http://localhost:1", "http://[::1]:2"):
        device.check_url(ok)
    with pytest.raises(device.EnrollError, match="plain http"):
        device.check_url("http://192.168.1.5:8740")
    device.check_url("http://192.168.1.5:8740", insecure=True)
    assert device.parse_target("rook.example")[0] == "https://rook.example"
    from nacl.signing import SigningKey
    root = SigningKey.generate()
    good = {"hub": {"hub_id": wire.key_id(bytes(root.verify_key)), "root": wire.pub_text(root)},
            "band": {"id": BAND, "psk": PSK, "relay": "r:1"}, "worker_id": "a" * 32}
    assert device.check_bundle(good, None) is good
    with pytest.raises(device.EnrollError, match="not 0000"):
        device.check_bundle(good, "0" * 16)
    with pytest.raises(device.EnrollError, match="another band"):
        device.check_bundle({**good, "band": {**good["band"], "psk": "other"}}, None)
    with pytest.raises(device.EnrollError, match="malformed"):
        device.check_bundle({"hub": {}}, None)


def test_init_sets_up_an_owner_hub_once(tmp_path, capsys):
    path = tmp_path / "hub" / "hub.yaml"
    assert hub_main(["--config", str(path), "init", "--owner", "bake"]) == 2         # no band
    assert hub_main(["--config", str(path), "init", "--owner", "bake", "--relay", "127.0.0.1:7474",
                     "--url", "https://rook.example"]) == 0
    out = capsys.readouterr().out
    cfg = load_config(path)
    assert cfg.users == {"bake": {"preset": "owner"}} and cfg.enroll == {"url": "https://rook.example"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    key = recovery_key(cfg.data)
    assert key in out and "Sign-in token for bake" in out
    token = out.split("Sign-in token for bake, shown once (only its hash is kept):\n  ")[1].split()[0]
    assert hub_main(["--config", str(path), "init", "--owner", "bake", "--relay", "x:1"]) == 2   # once
    assert hub_main(["--config", str(path), "recovery-key"]) == 0
    assert capsys.readouterr().out.strip() == key

    async def go():
        hub = Hub(cfg, transport=Loopback())
        await hub.start()
        try:
            assert not (cfg.data / "owner.token").exists()      # init handed the token over already
            return await http(hub, "POST", "/enroll/code", {}, token)
        finally:
            await hub.stop()
    got = run(go())
    assert got["ok"] and got["url"] == "https://rook.example"


def test_enroll_options_are_checked():
    check_options({"url": "https://x", "discover": True, "services": {"voice": {"url": "https://v"}}})
    for bad in ({"discover": True}, {"nope": 1}, {"services": {"voice": {}}}, {"url": 3}):
        with pytest.raises(ValueError):
            check_options(bad)
