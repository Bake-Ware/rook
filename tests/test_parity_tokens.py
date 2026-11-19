"""Parity rows 30, 32, 33 and 50 (docs/parity.md; brief 60), each through POST /mcp with an agent
token against a real hub and worker:

- an agent mints, lists and revokes tokens (token.create, token.list, token.revoke: 0.5's names),
  owned by the person its own token belongs to, and minted by its token (T5, T6);
- a webhook token minted through the door reaches POST /webhook and fires the job its reply's
  match names (J2); the Auth page mints one too;
- `python -m rook_worker URL --code TOKEN` with a join token minted through the door joins with no
  person approving (R3), owned by the agent's person; the code works once;
- users.list lists the users that the refusal "List the users" points at (row 50)."""

import asyncio
import json

import httpx2
import pytest

from helpers import eventually
from memory_link import connector
from rook_hub import Hub
from rook_worker import Cap, Worker
from test_door import mcp_client, refusal, tool
from test_first_run import Started
from test_jobs import runs
from test_site import ask, browser, login
from test_worker_start import WorkerStarted, set_up


async def echo(args):
    return args


async def started(tmp_path):
    """An in-process hub with Alice, Bob and Alice's agent token, and one real worker, linked."""
    hub = Hub(tmp_path / "hub")
    host, port = await hub.serve("127.0.0.1", 0)
    base = f"http://{host}:{port}"
    alice = hub.auth.create_user("Alice", "a password")
    bob = hub.auth.create_user("Bob", "another password")
    worker = Worker(tmp_path / "box", base, connector(hub), name="box", caps={"demo.echo": Cap("Echo.", {}, echo)},
                    macs=[], knock_every=0.02)
    running = asyncio.create_task(worker.run())
    await eventually(hub.list_pending)
    box = hub.approve(worker.key, user=alice["guid"])["moniker"]
    await eventually(lambda: hub.get_worker(box)["online"])
    return hub, base, alice, bob, hub.auth.mint("agent", user=alice["guid"]), worker, running, box


async def stop(hub, worker, running):
    worker.stop()
    await running
    await hub.stop_serving()


async def hub_cap(door, cap, **args):
    return await tool(door, "call", worker="hub", cap=cap, args=args, why=f"brief 60: {cap}")


def test_agent_mints_lists_and_revokes_tokens(tmp_path):
    async def go():
        hub, base, alice, bob, agent, worker, running, box = await started(tmp_path)
        mine = hub.auth.verify(agent)["moniker"]
        try:
            async with mcp_client(f"{base}/mcp", agent) as door:
                # token.create: an agent token by default, owned by Alice, minted by this agent's token (T5).
                failed, made = await hub_cap(door, "token.create")
                assert not failed, made
                assert made["purpose"] == "agent" and made["scopes"] == ["client"] and made["expires_at"] is None
                assert "POST /mcp" in made["use"]
                record = hub.auth.verify(made["token"])
                assert record["owner"] == alice["guid"] and record["minted_by"] == hub.auth.verify(agent)["guid"]

                # The new token works at the door, as Alice.
                async with mcp_client(f"{base}/mcp", made["token"]) as second:
                    failed, reply = await tool(second, "call", worker=box, cap="demo.echo", args={"hi": 1},
                                               why="the minted token works")
                    assert not failed and reply == {"hi": 1}

                # token.list: every token, never the token itself, with owner and who minted it (T6).
                failed, listed = await hub_cap(door, "token.list")
                assert not failed
                by_moniker = {t["moniker"]: t for t in listed["tokens"]}
                assert made["token"] not in json.dumps(listed)
                assert by_moniker[made["moniker"]]["minted_by"] == mine
                assert by_moniker[made["moniker"]]["owner"] == {"moniker": alice["moniker"], "name": "Alice"}
                assert by_moniker[made["moniker"]]["last_used"] is not None and by_moniker[mine]["minted_by"] is None

                # The minted token is not kept in the journal (the reply's token is withheld).
                assert made["token"] not in json.dumps([dict(r) for r in hub._db.execute("SELECT * FROM journal")])

                # token.revoke by moniker: it stops at once; one the hub does not know is refused with a next step.
                failed, gone = await hub_cap(door, "token.revoke", id=made["moniker"])
                assert not failed and gone == {"revoked": made["moniker"]}
                with pytest.raises(BaseException) as e:
                    async with mcp_client(f"{base}/mcp", made["token"]):
                        pass
                assert "was revoked" in refusal(e.value)
                failed, text = await hub_cap(door, "token.revoke", id="no-such-token")
                assert failed and text.startswith("unknown_token:") and "List the tokens" in text

                # A purpose the cap does not mint is refused, saying which it does.
                failed, text = await hub_cap(door, "token.create", purpose="session")
                assert failed and "agent" in text and "webhook" in text and "join" in text, text
        finally:
            await stop(hub, worker, running)
    asyncio.run(go())


def test_webhook_token_fires_its_job(tmp_path):
    async def go():
        hub, base, alice, bob, agent, worker, running, box = await started(tmp_path)
        try:
            async with mcp_client(f"{base}/mcp", agent) as door:
                # A webhook token through the door, with 0.5's job.webhook shape: path and header; and the
                # match a job uses for it.
                failed, hook = await hub_cap(door, "token.create", purpose="webhook")
                assert not failed, hook
                assert hook["path"] == "/webhook" and hook["header"] == "Authorization: Bearer <token>"
                assert hook["match"] == {"event": "webhook", "payload": {"token_moniker": hook["moniker"]}}
                assert hook["scopes"] == ["webhook"] and hook["expires_at"] is None

                failed, made = await hub_cap(door, "job.create", title="on the hook", match=hook["match"],
                                             call={"worker": box, "cap": "demo.echo", "args": {"via": "hook"}})
                assert not failed, made
                name, value = hook["header"].replace("<token>", hook["token"]).split(": ", 1)
                async with httpx2.AsyncClient() as http:
                    sent = await http.post(base + hook["path"], json={"build": "green"}, headers={name: value})
                assert sent.status_code == 202, sent.text
                [run] = await eventually(lambda: runs(hub, made["job"]))
                assert json.loads(run["result"])["result"] == {"via": "hook"}

            # The Auth page mints one too, as the person, with the same reply.
            async with browser() as client:
                await login(client, base, "Alice", "a password")
                status, page_hook = await ask(client, "POST", base + "/api/tokens", {"purpose": "webhook"})
                assert status == 200 and page_hook["purpose"] == "webhook" and page_hook["path"] == "/webhook"
                assert hub.auth.verify(page_hook["token"])["minted_by"] is None      # the person, not the session
                status, plain = await ask(client, "POST", base + "/api/tokens")
                assert status == 200 and plain["purpose"] == "agent"
        finally:
            await stop(hub, worker, running)
    asyncio.run(go())


def test_users_listed(tmp_path):
    async def go():
        hub, base, alice, bob, agent, worker, running, box = await started(tmp_path)
        try:
            async with mcp_client(f"{base}/mcp", agent) as door:
                failed, text = await hub_cap(door, "worker.reassign", worker=box, owner="Robert")
                assert failed and "List the users" in text
                failed, listed = await hub_cap(door, "users.list")
                assert not failed
                assert [(u["name"], u["moniker"], u["guid"], u["revoked_at"]) for u in listed["users"]] == [
                    ("Alice", alice["moniker"], alice["guid"], None), ("Bob", bob["moniker"], bob["guid"], None)]
                assert all("password" not in u for u in listed["users"])
                # And what it lists is what the refusal wanted.
                failed, moved = await hub_cap(door, "worker.reassign", worker=box, owner=listed["users"][1]["moniker"])
                assert not failed and moved["owner"]["name"] == "Bob"
        finally:
            await stop(hub, worker, running)
    asyncio.run(go())


def test_start_command_joins_with_a_join_token(tmp_path):
    async def go():
        async with Started(tmp_path / "data", tmp_path / "vault.key") as hub:
            client = await set_up(hub)            # the first user, Alice, signed in on the site
            workers = []
            try:
                status, minted = await ask(client, "POST", hub.base + "/api/tokens")
                assert status == 200
                async with mcp_client(hub.base + "/mcp", minted["token"]) as door:
                    failed, join = await hub_cap(door, "token.create", purpose="join")
                    assert not failed and join["scopes"] == ["join"] and join["expires_at"]
                    assert "--code" in join["use"]

                    # Started with the code: approved at once, with no one approving, and linked.
                    worker = await WorkerStarted(tmp_path / "home", hub.base, "--code", join["token"]).start()
                    workers.append(worker)
                    approved = await worker.said("Approved as ")
                    moniker = approved.removeprefix("Approved as ").rstrip(".")
                    await worker.said(f"Linked to {hub.base} as {moniker}.")
                    assert worker.lines[1].endswith(" and a join token.")
                    assert not any(line.startswith("Waiting for approval") for line in worker.lines)

                    # Owned by the person whose agent minted the code; nothing is left knocking.
                    failed, listed = await tool(door, "workers")
                    [row] = [w for w in listed["workers"] if w["moniker"] == moniker]
                    assert row["owner"]["name"] == "Alice" and listed["knocking"] == []

                    # The code works once: a second machine with it is refused in one plain line.
                    second = await WorkerStarted(tmp_path / "other", hub.base, "--code", join["token"]).start()
                    workers.append(second)
                    code, errors = await second.ended(timeout=20)
                    assert code == 1 and "Traceback" not in errors and errors.strip() == (
                        f"rook worker: the hub refused the join token. Token {join['moniker']} was already used, and "
                        "it works once. Mint a new join token on the hub (worker.join-token), then start again with it.")
            finally:
                await client.close()
                for worker in workers:
                    worker.kill()
    asyncio.run(go())
