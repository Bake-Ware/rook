"""The heartbeat history behind the Workers page's graphs (P4, P7, H1, H4, H5), through
real HTTP as the browser asks for it.

The sample hub: on band Home, a desktop worker (cpu, ram, disk, temperature) and a GPU
box (gpu, vram, npu, battery), with two devices; on band Office, an ESP32 (free heap and
uptime) and a worker that reports no health at all; and a worker that never connected.
Each worker heartbeats every five minutes for an hour, on a clock the test moves; then
the clock moves on two hours with no heartbeats."""

import asyncio
import datetime
import pathlib

from nacl.signing import SigningKey

from helpers import Clock, link_fields
from memory_link import link_pair
from rook_hub import Hub, bands
from rook_hub.journal import iso
from rook_protocol import decode, encode
from test_site import COOKIE, ask, browser, login

HOME, OFFICE = "02:aa:00:00:00:01", "02:bb:00:00:00:02"
GB = 1 << 30
PLUGINS = pathlib.Path(__file__).parent / "plugins"


def health(name, i):
    """What each worker reports in its i-th heartbeat (H4); the quiet one reports nothing."""
    return {"desk": {"cpu": 10 * (i % 4), "ram": {"total": 8 * GB, "free": (8 - i % 4) * GB},
                     "disk": {"total": 100 * GB, "free": 75 * GB}, "temperature": 40 + i},
            "gpubox": {"gpu": 90, "vram": {"total": 24 * GB, "free": 6 * GB}, "npu": 5, "battery": 100 - i},
            "esp": {"heap_free": 180000 - 1000 * i, "uptime": 300 * (i + 1)},
            "quiet": {}}[name]


class Beater:
    """An approved worker on an in-memory link that sends the heartbeats it is told to."""

    def __init__(self, hub, name, owner, gateway):
        self.hub, self.name, self.gateway = hub, name, gateway
        signing_key = SigningKey.generate()
        self.key = signing_key.verify_key.encode().hex()
        hub.knock({"type": "knock", "protocol": 1, "name": name, "key": self.key, "macs": [], "caps": [],
                   **link_fields(signing_key)})
        self.moniker = hub.approve(self.key, user=owner)["moniker"]

    async def beat(self, health):
        if not hasattr(self, "link"):
            hub_end, self.link = link_pair(self.key)
            self.serving = asyncio.create_task(self.hub.serve_link(hub_end))
        await self.link.send(encode({
            "type": "heartbeat", "protocol": 1, "identity": {"name": self.name, "key": self.key},
            "facts": {"os": "linux", "arch": "x86_64", "hostname": self.name, "form": "python", "version": "0.1.0",
                      "macs": []}, "caps": [], "band": {"gateway_mac": self.gateway}, "health": health}))
        assert decode(await self.link.recv())["type"] == "heartbeat_reply"

    async def stop(self):
        if hasattr(self, "link"):
            self.link.close()
            await self.serving


def test_site_history(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    clock = Clock()
    hub = Hub(tmp_path / "hub", clock=clock)
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    a = {"user": alice["guid"], "token": None}
    home = bands.create(hub, "Home", HOME, **a)
    bands.create(hub, "Office", OFFICE, **a)
    beaters = {name: Beater(hub, name, alice["guid"], gw) for name, gw in
               (("desk", HOME), ("gpubox", HOME), ("esp", OFFICE), ("quiet", OFFICE))}
    spare = Beater(hub, "spare", alice["guid"], None)                 # approved, never heartbeats
    hub.plugins.install(PLUGINS / "lamp", a)
    lamps = [(await hub.call("hub", "device.add", {"name": f"lamp {n}", "driver": "lamp", "route": "direct",
                                                   "band": home["moniker"],
                                                   "config": {"url": "http://127.0.0.1:9"}}, "add a lamp", **a)
              )["result"]["added"]["moniker"] for n in (1, 2)]
    try:
        for i in range(12):                        # a heartbeat every five minutes for an hour
            clock.advance(300)
            for name, beater in beaters.items():
                await beater.beat(health(name, i))
        hour_end = clock()

        async with browser() as client:
            # 1. Without a session the history is refused (T4).
            for path in ("/api/heartbeats", f"/api/heartbeats/{beaters['desk'].moniker}"):
                status, reply = await ask(client, "GET", base + path)
                assert status == 401 and reply["code"] == "no_session", path
            async with browser() as stranger:
                stranger.cookie_jar.update_cookies({COOKIE: hub.auth.mint("agent", user=alice["guid"])})
                status, reply = await ask(stranger, "GET", base + "/api/heartbeats")
                assert status == 401 and reply["code"] == "token_scope"
            assert (await login(client, base, "Alice", "correct horse"))[0] == 200

            async def series(name, query):
                status, reply = await ask(client, "GET", f"{base}/api/heartbeats/{beaters[name].moniker}?{query}")
                assert status == 200, reply
                return reply

            # 2. One worker's health over the hour, in ten-minute buckets the database made (H4, H5).
            desk = await series("desk", "span=3600&step=600")
            assert desk["worker"] == beaters["desk"].moniker and desk["step"] == 600
            assert desk["fields"] == ["cpu", "ram", "disk", "temperature"]
            assert desk["totals"] == {"ram": 8 * GB, "disk": 100 * GB}
            assert sum(b["count"] for b in desk["buckets"]) == 12
            # Heartbeat i came 3600 - 300 (i + 1) seconds before now; buckets count back from now,
            # ten minutes each, oldest first: each bucket's averages are checked against its heartbeats.
            expected = {}
            for i in range(12):
                expected.setdefault((3600 - 300 * (i + 1)) // 600, []).append(i)
            assert len(desk["buckets"]) == 6
            for bucket in desk["buckets"]:
                k = round((hour_end - datetime.datetime.fromisoformat(bucket["at"]).timestamp()) / 600) - 1
                beats = expected[k]
                assert bucket["count"] == len(beats)
                assert bucket["cpu"] == round(sum(10 * (i % 4) for i in beats) / len(beats), 2)
                assert bucket["ram"] == round(sum(100 * (i % 4) / 8 for i in beats) / len(beats), 2)
                assert bucket["disk"] == 25.0
                assert bucket["temperature"] == round(sum(40 + i for i in beats) / len(beats), 2)
                assert "gpu" not in bucket and "heap_free" not in bucket

            gpubox = await series("gpubox", "span=3600")              # the step defaults to span / 120
            assert gpubox["fields"] == ["gpu", "vram", "npu", "battery"] and gpubox["step"] == 30
            assert gpubox["totals"] == {"vram": 24 * GB} and len(gpubox["buckets"]) == 12
            assert gpubox["buckets"][-1] == {"at": gpubox["buckets"][-1]["at"], "count": 1, "gpu": 90.0, "vram": 75.0,
                                             "npu": 5.0, "battery": 89.0}

            # An ESP32: free heap and uptime only. A step of an hour or more is read from the hourly
            # roll-up, in clock hours (H5): one two-hour bucket holds the whole hour of heartbeats.
            esp = await series("esp", "span=7200&step=7200")
            assert esp["fields"] == ["heap_free", "uptime"] and esp["totals"] == {} and esp["source"] == "hourly"
            [whole] = esp["buckets"]
            assert whole["count"] == 12 and whole["uptime"] == 3600
            assert whole["heap_free"] == sum(180000 - 1000 * i for i in range(12)) / 12

            quiet = await series("quiet", "span=3600&step=600")      # reports nothing: counts, no fields
            assert quiet["fields"] == [] and sum(b["count"] for b in quiet["buckets"]) == 12
            assert all(set(b) == {"at", "count"} for b in quiet["buckets"])
            nothing = await ask(client, "GET", f"{base}/api/heartbeats/{spare.moniker}?span=3600")
            assert nothing[1]["buckets"] == [] and nothing[1]["fields"] == []

            # 3. Every worker's heartbeat counts over time: the heartbeats graph (P7, H1).
            status, counts = await ask(client, "GET", f"{base}/api/heartbeats?span=3600&step=1800")
            assert status == 200 and counts["step"] == 1800
            rows = {w["name"]: w for w in counts["workers"]}
            assert list(rows) == ["desk", "gpubox", "esp", "quiet", "spare"]      # workers only: no devices (D6)
            assert not set(lamps) & {w["moniker"] for w in counts["workers"]}
            for name in beaters:
                assert [c["count"] for c in rows[name]["counts"]] == [6, 6], name
                assert rows[name]["last_heartbeat"] == counts["now"] and rows[name]["interval"] == 15
            assert rows["spare"]["counts"] == [] and rows["spare"]["last_heartbeat"] is None
            status, latest = await ask(client, "GET", f"{base}/api/heartbeats")        # the last minute, by default
            assert {w["name"]: sum(c["count"] for c in w["counts"]) for w in latest["workers"]} == {
                "desk": 1, "gpubox": 1, "esp": 1, "quiet": 1, "spare": 0}

            # 4. A span with no heartbeats: two hours on, the last hour is empty.
            clock.advance(7200)
            assert (await series("desk", "span=3600"))["buckets"] == []
            status, counts = await ask(client, "GET", f"{base}/api/heartbeats?span=3600")
            assert all(w["counts"] == [] for w in counts["workers"])
            assert {w["last_heartbeat"] for w in counts["workers"] if w["name"] != "spare"} == {iso(hour_end)}
            assert sum(b["count"] for b in (await series("desk", "span=10800"))["buckets"]) == 12

            # Bad requests say what to do; a device has no heartbeat history.
            for path in ("/api/heartbeats?span=0", "/api/heartbeats?span=x", "/api/heartbeats?span=60&step=-1",
                         f"/api/heartbeats/{beaters['desk'].moniker}?step=0", f"/api/heartbeats/{lamps[0]}",
                         "/api/heartbeats/nobody-at-all"):
                status, reply = await ask(client, "GET", base + path)
                assert status == 400 and reply["next"], path
    finally:
        for beater in beaters.values():
            await beater.stop()
        await hub.stop_serving()
        hub.close()
