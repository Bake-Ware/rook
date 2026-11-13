"""The site's Jobs, Secrets and Auth pages (P4, P3, P6, P8, J1, J3, J4, J5, J7, X3, T3, T5, T6),
driven through real HTTP on localhost, as the browser drives them.

A worker on an in-memory link serves demo.echo (answers) and demo.fail (an error),
so job runs succeed and fail for real."""

import asyncio
import json
import os

import yarl
from nacl.signing import SigningKey

from helpers import Clock, eventually
from memory_link import link_pair
from rook_hub import Hub, journal
from rook_protocol import decode, encode
from test_door import mcp_client, tool
from test_site import COOKIE, ask, browser, login

CAPS = [{"name": "demo.echo", "description": "Answer with the arguments.", "returns": "result",
         "schema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        {"name": "demo.fail", "description": "Always fail.", "returns": "result", "schema": {}}]
VALUE = "hunter2-correct-horse-battery"


class Box:
    """An approved worker that answers demo.echo with its arguments and demo.fail with an error."""

    def __init__(self, hub, name, owner):
        self.hub, self.name = hub, name
        self.key = SigningKey.generate().verify_key.encode().hex()
        hub.knock({"type": "knock", "protocol": 1, "name": name, "key": self.key, "macs": [], "caps": []})
        record = hub.approve(self.key, user=owner)
        self.guid, self.moniker = record["guid"], record["moniker"]

    async def connect(self):
        hub_end, self.link = link_pair(self.key)
        self.serving = asyncio.create_task(self.hub.serve_link(hub_end))
        self.answering = asyncio.create_task(self._answer())
        await self.link.send(encode({
            "type": "heartbeat", "protocol": 1, "identity": {"name": self.name, "key": self.key},
            "facts": {"os": "linux", "arch": "x86_64", "hostname": self.name, "form": "python", "version": "0.1.0",
                      "macs": []}, "caps": CAPS, "band": {"gateway_mac": None}, "health": {}}))
        await eventually(lambda: self.hub.get_worker(self.guid)["online"])

    async def _answer(self):
        while True:
            try:
                message = decode(await self.link.recv())
            except EOFError:
                return
            if message["type"] == "call" and message["cap"] == "demo.echo":
                await self.link.send(encode({"type": "result", "id": message["id"], "result": message["args"]}))
            elif message["type"] == "call":
                await self.link.send(encode({"type": "error", "id": message["id"], "code": "it_failed",
                                             "message": "demo.fail always fails.", "next": "Nothing to try."}))

    async def stop(self):
        self.link.close()
        await self.serving
        await self.answering


def job(title, event, payload, worker, cap, args=None):
    return {"title": title, "description": f"The {title} job.", "match": {"event": event, "payload": payload},
            "call": {"worker": worker, "cap": cap, "args": args or {}}}


def calls(hub, cap):
    """Journal entries of site calls to a hub cap, oldest first."""
    return journal.read(hub, kind="call", cap=cap, limit=100)[::-1]


def test_site_pages(tmp_path):
    asyncio.run(scenario(tmp_path))


async def scenario(tmp_path):
    hub = Hub(tmp_path / "hub", clock=Clock(), vault_key=os.urandom(32))
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "correct horse")
    bob = hub.auth.create_user("Bob", "battery staple")
    box = Box(hub, "desk", alice["guid"])
    await box.connect()
    locked = Hub(tmp_path / "locked", clock=Clock())        # started without its vault key (X4)
    locked_host, locked_port = await locked.serve("127.0.0.1", 0)
    locked.auth.create_user("Alice", "correct horse")
    try:
        async with browser() as client, browser() as bobs:
            # 0. Without a session every request of these pages is refused, saying what to do (T4).
            for method, path in (("GET", "/api/jobs"), ("GET", "/api/jobs/runs"), ("GET", "/api/jobs/targets"),
                                 ("POST", "/api/jobs"), ("POST", "/api/jobs/x/pause"), ("POST", "/api/jobs/x/unpause"),
                                 ("POST", "/api/jobs/x/delete"), ("GET", "/api/secrets"), ("POST", "/api/secrets"),
                                 ("POST", "/api/secrets/x/reveal"), ("GET", "/api/auth"), ("POST", "/api/tokens"),
                                 ("POST", "/api/tokens/x/revoke"), ("POST", "/api/users/x/revoke"),
                                 ("POST", "/api/password")):
                status, reply = await ask(client, method, base + path, {} if method == "POST" else None)
                assert status == 401 and reply["code"] == "no_session" and reply["next"], (method, path)
            for page in ("/jobs", "/secrets", "/auth"):
                async with client.get(base + page) as response:
                    assert response.status == 200 and "/site/shell.js" in await response.text()
            for script in ("jobs.js", "secrets.js", "auth.js", "parts.js"):
                async with client.get(f"{base}/site/pages/{script}") as response:
                    assert response.status == 200, script

            assert (await login(client, base, "Alice", "correct horse"))[0] == 200
            session = client.cookie_jar.filter_cookies(yarl.URL(base))[COOKIE].value
            session_moniker = hub.auth.verify(session)["moniker"]
            status, reply = await ask(client, "GET", base + "/api/session")
            assert [p["id"] for p in reply["nav"][0]["pages"]] == ["workers", "auth", "secrets", "jobs", "data"]

            # ------------------------------------------------------------ Jobs
            # 1. What a new job can call: the hub, and each worker, with its caps' schemas.
            status, reply = await ask(client, "GET", base + "/api/jobs/targets")
            targets = {t["worker"]: t for t in reply["targets"]}
            assert targets["hub"]["kind"] == "hub" and "job.create" in [c["name"] for c in targets["hub"]["caps"]]
            assert targets[box.moniker]["name"] == "desk" and targets[box.moniker]["caps"] == [
                {"name": c["name"], "description": c["description"], "schema": c["schema"]} for c in CAPS]

            # 2. New job, through job.create as a call by the user (J1, J7, L1). A chain of three:
            #    a starts on event "go"; b after a succeeds; c after b fails (J3).
            created = {}
            for title, event, payload, cap in (("a", "go", {}, "demo.echo"), ("b", "job.success", "a", "demo.fail"),
                                               ("c", "job.fail", "b", "demo.echo")):
                if isinstance(payload, str):
                    payload = {"job_moniker": created[payload]["moniker"]}
                status, reply = await ask(client, "POST", base + "/api/jobs",
                                          job(title, event, payload, box.moniker, cap, {"text": title}))
                assert status == 200 and reply["warnings"] == [], reply
                created[title] = reply["job"]
            [made] = calls(hub, "job.create")[:1]
            assert (made["user"], made["token"], made["why"]) == (alice["moniker"], session_moniker,
                                                                  "Created on the Jobs page.")
            status, reply = await ask(client, "POST", base + "/api/jobs", {"title": "no call"})
            assert status == 400 and reply["code"] == "bad_message" and reply["next"]

            # A loop of two (J4). The hub has no way to edit a job (not in this slice), and a
            # job's moniker exists only once it is made, so the test points x at y directly.
            for title, other in (("x", "placeholder"), ("y", None)):
                payload = {"job_moniker": other or created["x"]["moniker"]}
                status, reply = await ask(client, "POST", base + "/api/jobs",
                                          job(title, "job.success", payload, box.moniker, "demo.echo"))
                created[title] = reply["job"]
            with hub._lock, hub._db:
                hub._db.execute("UPDATE jobs SET payload = ? WHERE guid = ?",
                                (json.dumps({"job_moniker": created["y"]["moniker"]}), created["x"]["guid"]))

            # 3. The list and the chains as trees, with the hub's loop warning (P4, J3, J4).
            m = {title: created[title]["moniker"] for title in created}
            status, view = await ask(client, "GET", base + "/api/jobs")
            assert status == 200
            listed = {j["title"]: j for j in view["jobs"]}
            assert list(listed) == ["a", "b", "c", "x", "y"]
            assert listed["b"]["match"] == {"event": "job.success", "payload": {"job_moniker": m["a"]}}
            assert listed["b"]["call"] == {"worker": box.moniker, "name": "desk", "cap": "demo.fail", "args": {"text": "b"}}
            assert listed["b"]["owner"] == {"moniker": alice["moniker"], "name": "Alice"}
            assert listed["b"]["description"] == "The b job." and listed["b"]["paused"] is False
            assert "warning" not in listed["a"] and m["x"] in listed["x"]["warning"] and m["y"] in listed["y"]["warning"]
            assert view["chains"] == [
                {"job": m["a"], "on": "go", "loop": False, "children": [
                    {"job": m["b"], "on": "job.success", "loop": False, "children": [
                        {"job": m["c"], "on": "job.fail", "loop": False, "children": []}]}]},
                {"job": m["x"], "on": "job.success", "loop": False, "children": [
                    {"job": m["y"], "on": "job.success", "loop": False, "children": [
                        {"job": m["x"], "on": "job.success", "loop": True, "children": []}]}]}]

            # Saving a job that closes a loop shows the hub's warning in the reply (J4).
            status, reply = await ask(client, "POST", base + "/api/jobs",
                                      job("echo-every-success", "job.success", {}, box.moniker, "demo.echo"))
            assert status == 200 and len(reply["warnings"]) == 1 and "loop" in reply["warnings"][0]
            # Delete it, through job.delete as a call (J7).
            status, reply = await ask(client, "POST", f"{base}/api/jobs/{reply['job']['moniker']}/delete")
            assert status == 200 and reply == {"deleted": reply["deleted"]}
            assert [e["user"] for e in calls(hub, "job.delete")] == [alice["moniker"]]

            # 4. Pause and un-pause, as calls by the user (J7, L1).
            status, reply = await ask(client, "POST", f"{base}/api/jobs/{m['y']}/pause")
            assert status == 200 and reply == {"job": m["y"], "paused": True}
            status, view = await ask(client, "GET", base + "/api/jobs")
            assert {j["title"]: j["paused"] for j in view["jobs"]}["y"] is True
            status, reply = await ask(client, "POST", f"{base}/api/jobs/{m['y']}/unpause")
            assert status == 200 and reply == {"job": m["y"], "paused": False}
            assert [(e["user"], e["token"]) for e in calls(hub, "job.pause") + calls(hub, "job.unpause")] == \
                [(alice["moniker"], session_moniker)] * 2
            status, reply = await ask(client, "POST", f"{base}/api/jobs/nobody-has-this/pause")
            assert status == 400 and reply["code"] == "unknown_job"

            # 5. Runs, grouped by run ID (J5), newest first, each step with its call's reply (P8).
            for steps in (3, 6):                     # one chain run at a time: a ok, b fails, c ok
                hub.events.emit("go", {})
                await eventually(lambda: len(journal.read(hub, kind="job", limit=100)) == steps)
            status, reply = await ask(client, "GET", base + "/api/jobs/runs")
            assert status == 200 and len(reply["runs"]) == 2
            newest, oldest = reply["runs"]
            assert newest["run"] != oldest["run"] and newest["at"] >= oldest["at"]
            for run in reply["runs"]:
                assert [(s["title"], s["cap"], s["ok"]) for s in run["steps"]] == [
                    ("a", "demo.echo", True), ("b", "demo.fail", False), ("c", "demo.echo", True)]
                assert run["steps"][0]["result"] == {"type": "result", "id": run["steps"][0]["result"]["id"],
                                                     "result": {"text": "a"}}
                assert run["steps"][1]["result"]["code"] == "it_failed"
                assert (run["steps"][0]["worker"], run["steps"][0]["name"]) == (box.moniker, "desk")
                assert run["steps"][0]["why"].startswith("Job ")
            status, mine = await ask(client, "GET", f"{base}/api/jobs/runs?job={m['c']}")
            assert mine == reply                        # c took part in both chain runs, shown whole
            status, none = await ask(client, "GET", f"{base}/api/jobs/runs?job={m['x']}")
            assert none == {"runs": []}

            # ------------------------------------------------------------ Secrets
            # 6. Add a secret through secret.create as a call; the value is never listed or journaled (X2, X3).
            status, reply = await ask(client, "POST", base + "/api/secrets",
                                      {"scope": "hub", "name": "deploy-key", "value": VALUE})
            assert status == 200 and reply["created"]["name"] == "deploy-key"
            status, reply = await ask(client, "POST", base + "/api/secrets",
                                      {"scope": f"worker:{box.moniker}", "name": "root-password", "value": "pw-on-desk"})
            assert status == 200 and reply["created"]["scope"] == f"worker:{box.moniker}"
            assert [(e["user"], e["token"], e["why"]) for e in calls(hub, "secret.create")] == \
                [(alice["moniker"], session_moniker, "Added on the Secrets page.")] * 2
            status, reply = await ask(client, "POST", base + "/api/secrets", {"scope": "hub", "name": "deploy-key",
                                                                               "value": "again"})
            assert status == 400 and reply["code"] == "secret_exists" and reply["next"]
            async with client.get(base + "/api/secrets") as response:
                text = await response.text()
                assert response.status == 200 and VALUE not in text and "pw-on-desk" not in text
                view = json.loads(text)
            secret = {s["name"]: s for s in view["secrets"]}["deploy-key"]
            assert (secret["scope"], secret["reference"]) == ("hub", "secret:deploy-key")
            assert secret["created_by"] == {"moniker": alice["moniker"], "name": "Alice"}
            assert secret["token"] == session_moniker and secret["created_at"]
            assert view["scopes"]["workers"] == [{"moniker": box.moniker, "name": "desk"}]
            assert view["scopes"]["bands"] == []

            # 7. Reveal: the value comes back to this person, once per request, never cached;
            #    the journal holds the reveal under the user and the session, without the value (X3).
            async with client.post(f"{base}/api/secrets/{secret['moniker']}/reveal") as response:
                assert response.status == 200 and await response.json() == {"value": VALUE}
                assert response.headers["Cache-Control"] == "no-store"
            [revealed] = [e for e in journal.read(hub, kind="auth", limit=100) if "revealed" in e["result"]]
            assert (revealed["user"], revealed["token"]) == (alice["moniker"], session_moniker)
            assert revealed["result"] == {"revealed": secret["guid"], "name": "deploy-key", "scope": "hub"}
            assert journal.read(hub, text=VALUE) == []
            status, reply = await ask(client, "POST", f"{base}/api/secrets/nothing-here/reveal")
            assert status == 400 and reply["code"] == "unknown_secret"
            agent = hub.auth.mint("agent", user=alice["guid"])
            agent_m = hub.auth.verify(agent)["moniker"]
            async with browser() as stolen:              # an agent's token as the cookie is refused
                stolen.cookie_jar.update_cookies({COOKIE: agent})
                status, reply = await ask(stolen, "POST", f"{base}/api/secrets/{secret['moniker']}/reveal")
                assert status == 401 and reply["code"] == "token_scope"
            assert len([e for e in journal.read(hub, kind="auth", limit=100) if "revealed" in e["result"]]) == 1

            # 8. A locked vault: the page is told so, and what to do.
            async with browser() as other:
                locked_base = f"http://{locked_host}:{locked_port}"
                assert (await login(other, locked_base, "Alice", "correct horse"))[0] == 200
                status, reply = await ask(other, "GET", locked_base + "/api/secrets")
                assert status == 400 and reply["code"] == "vault_locked" and "vault key" in reply["next"]
                status, reply = await ask(other, "POST", locked_base + "/api/secrets",
                                          {"scope": "hub", "name": "k", "value": "v"})
                assert status == 400 and reply["code"] == "vault_locked"

            # ------------------------------------------------------------ Auth
            # 9. Users, and tokens with scope, owner, times and last use (T6). Sessions are tokens.
            await ask(client, "POST", f"{base}/api/tokens/{agent_m}/revoke")
            used = hub.auth.mint("agent", user=alice["guid"])
            async with mcp_client(f"{base}/mcp", used) as mcp:
                failed, _ = await tool(mcp, "workers")
                assert not failed
            unused = hub.auth.mint("agent", user=alice["guid"])
            assert (await login(bobs, base, "Bob", "battery staple"))[0] == 200
            status, view = await ask(client, "GET", base + "/api/auth")
            assert status == 200 and view["me"] == alice["moniker"]
            assert [(u["name"], u["moniker"], u["revoked_at"]) for u in view["users"]] == [
                ("Alice", alice["moniker"], None), ("Bob", bob["moniker"], None)]
            tokens = {t["moniker"]: t for t in view["tokens"]}
            mine = tokens[session_moniker]
            assert (mine["purpose"], mine["scopes"], mine["owner"]["name"], mine["this_session"]) == \
                ("session", ["session"], "Alice", True)
            assert mine["last_used"] and mine["minted_at"] and mine["expires_at"] is None
            used_m, unused_m = (hub.auth.verify(t)["moniker"] for t in (used, unused))
            assert tokens[used_m]["last_used"] and tokens[used_m]["scopes"] == ["client"]
            assert tokens[unused_m]["last_used"] is None and tokens[unused_m]["revoked_at"] is None
            assert tokens[agent_m]["revoked_at"] and not tokens[agent_m]["this_session"]
            assert sum(t["purpose"] == "session" and t["owner"]["name"] == "Bob" for t in view["tokens"]) == 1
            assert all(jwt not in json.dumps(view) for jwt in (session, used, unused, agent))
            guids = {t["moniker"]: t["guid"] for t in hub.auth.list_tokens()}
            assert [e["token"] for e in journal.read(hub, kind="auth", user=alice["guid"], limit=100)
                    if e["result"] == {"revoked_token": guids[agent_m]}] == [session_moniker]

            # 10. New token: an agent token for this user, shown once, that works at the MCP door (T3, T5).
            async with client.post(base + "/api/tokens") as response:
                assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
                minted = await response.json()
            record = hub.auth.verify(minted["token"])
            assert (record["moniker"], record["purpose"], record["owner"]) == (minted["moniker"], "agent", alice["guid"])
            assert journal.read(hub, text=minted["token"]) == []
            status, view = await ask(client, "GET", base + "/api/auth")
            assert minted["token"] not in json.dumps(view)
            assert {t["moniker"]: t for t in view["tokens"]}[minted["moniker"]]["minted_by"] == session_moniker
            async with mcp_client(f"{base}/mcp", minted["token"]) as mcp:
                failed, workers = await tool(mcp, "workers")
                assert not failed and box.moniker in json.dumps(workers)

            # 11. Revoke a token; revoke a user and every token in their chain (T5). Both journaled.
            status, reply = await ask(client, "POST", f"{base}/api/tokens/{used_m}/revoke")
            assert status == 200
            [entry] = [e for e in journal.read(hub, kind="auth", user=alice["guid"], limit=100)
                       if e["result"] == {"revoked_token": guids[used_m]}]
            assert entry["token"] == session_moniker
            status, bob_minted = await ask(bobs, "POST", base + "/api/tokens")     # Bob's own agent token
            status, reply = await ask(client, "POST", f"{base}/api/users/{bob['moniker']}/revoke")
            assert status == 200
            [entry] = [e for e in journal.read(hub, kind="auth", user=alice["guid"], limit=100)
                       if "revoked_user" in e["result"]]
            assert entry["result"]["revoked_user"] == bob["guid"] and len(entry["result"]["revoked_tokens"]) == 2
            status, reply = await ask(bobs, "GET", base + "/api/auth")
            assert status == 401 and reply["code"] == "token_revoked"
            assert {t["moniker"]: t for t in hub.auth.list_tokens()}[bob_minted["moniker"]]["revoked_at"]  # chain
            status, reply, _ = await login(bobs, base, "Bob", "battery staple")
            assert status == 401 and reply["code"] == "user_revoked"
            status, view = await ask(client, "GET", base + "/api/auth")
            assert {u["name"]: u["revoked_at"] for u in view["users"]}["Bob"] is not None

            # 12. Change my own password, then log in with the new one.
            status, reply = await ask(client, "POST", base + "/api/password", {"password": ""})
            assert status == 400 and reply["code"] == "bad_message"
            status, reply = await ask(client, "POST", base + "/api/password", {"password": "a new password"})
            assert status == 200 and reply == {"changed": True}
            assert "a new password" not in json.dumps(journal.read(hub, limit=1000))
            await ask(client, "POST", base + "/api/logout")
            assert (await login(client, base, "Alice", "correct horse"))[0] == 401
            assert (await login(client, base, "Alice", "a new password"))[0] == 200
    finally:
        await box.stop()
        await hub.stop_serving()
        await locked.stop_serving()
        hub.close()
        locked.close()
