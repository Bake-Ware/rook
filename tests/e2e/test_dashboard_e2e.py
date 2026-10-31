"""The hub site against a real hub process (see harness.py): a person signs
in on the HTTP door, opens the workers page, and its sandboxed calls reach
only what the page declares, under that person's rights; with the agents
package the house agent builds and archives a page from the site's dock."""
from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request

from harness import HTTP_SECS, REPO, until


class Browser:
    def __init__(self, url: str):
        self.url, self.cookie, self.csrf = url, "", ""

    def request(self, method: str, path: str, body: dict | None = None):
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.csrf:
            headers["X-Rook-CSRF"] = self.csrf
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=HTTP_SECS) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def post(self, path: str, body: dict) -> dict:
        return json.loads(self.request("POST", path, body)[2])

    def sign_in(self, token: str) -> None:
        status, headers, _ = self.request("POST", "/login", {"token": token})
        assert status == 200
        self.cookie = headers["Set-Cookie"].split(";")[0]
        self.csrf = json.loads(self.request("GET", "/web/api/me")[2])["csrf"]


def test_a_person_uses_the_workers_page(band):
    b = Browser(band.url)
    status, _, page = b.request("GET", "/")           # signed out: on to /login (urllib follows the 303)
    assert status == 200 and b'action="/login"' in page
    b.sign_in(band.owner_token)
    status, _, shell = b.request("GET", "/")
    assert status == 200 and b"/web/static/shell.js" in shell

    pages = b.post("/web/api/call", {"cap": "page.list"})["result"]["pages"]
    assert "hub/workers" in [p["page"] for p in pages]
    opened = b.post("/web/api/open", {"page": "hub/workers"})
    assert opened["ok"], opened
    status, headers, doc = b.request("GET", opened["frame"])
    assert status == 200 and headers["Content-Security-Policy"].startswith("sandbox allow-scripts;")
    assert b"window.rook" in doc and b"hub.workers" in doc

    workers = until(lambda: (r := b.post("/web/api/page", {"token": opened["token"], "cap": "hub.workers"}))["ok"]
                    and {w["name"]: w for w in r["result"]["workers"]}.get("e2e-worker"), 20, "the worker on the page")
    assert workers["online"] and workers["id"] == band.worker_id
    # a cap the page doesn't declare is refused, even for an owner, and nothing reaches the worker
    before = band.worker_calls("e2e.ping")
    out = b.post("/web/api/page", {"token": opened["token"], "cap": "e2e.ping", "worker": "e2e-worker",
                                   "args": {"text": "hi"}})
    assert out["error"] == "denied: page: grants"
    assert band.worker_calls("e2e.ping") == before
    entries = band.owner.get("/journal?cap=hub.workers&limit=5")["entries"]
    assert entries and entries[0]["person"] == "person:bake" and entries[0]["why"] == "Shown on the hub/workers page."


def test_a_child_opens_pages_its_preset_grants_and_nothing_else(band):
    kid = Browser(band.url)
    kid.sign_in(band.kid_token)
    out = kid.post("/web/api/open", {"page": "hub/workers"})     # child: page.view, no first-use queue
    assert not out.get("held"), out
    assert kid.post("/web/api/call", {"cap": "page.list"})["error"].startswith("denied: person")


def test_the_house_agent_makes_and_archives_a_page_from_the_dock(fresh):
    """Agents + workbench in a real hub process, with a fake agent.backend
    (tests/fixtures/castling/house-brain): from the site's dock the person
    asks the house agent for a page; it arranges components, the build is a
    task linked to the page, the page is their start page; then it archives
    it."""
    for folder in ("agents", "tasks", "projects", "knowledge", "concepts", "whiteboard"):
        shutil.copytree(REPO / "rook" / "plugins" / folder, fresh.library / folder)
    shutil.copytree(REPO / "tests" / "fixtures" / "castling" / "house-brain", fresh.library / "agents-local")
    fresh.start_hub()
    b = Browser(fresh.url)
    b.sign_in(fresh.owner_token)

    def shell(cap, **args):
        out = b.post("/web/api/call", {"cap": cap, "args": args})
        assert out.get("ok"), out
        return out["result"]

    assert shell("page.site") == {"person": "person:bake", "start": None, "dock": "house", "workbench": True}
    assert shell("agent.chat.read", agent="house")["events"] == []         # first run
    assert "How do you want to use the site" in shell(
        "agent.chat.send", agent="house", text="[site] First visit to the hub site. Ask me.")["text"]
    reply = shell("agent.chat.send", agent="house",
                  text="[site] The person is on the page hub/workers (Workers).\nmake a page uptime")["text"]
    assert reply.startswith("Made page/uptime (task r_"), reply
    assert shell("page.site")["start"] == "page/uptime"
    page = shell("page.get", page="page/uptime")
    assert page["made_by"]["task"] and page["note"].startswith("# Uptime")
    task = fresh.owner.call("task.get", {"id": page["made_by"]["task"]}, worker="rook")["result"]
    assert task["links"][0]["ref"] == "page/uptime" and task["state"] == "in_progress"

    opened = b.post("/web/api/open", {"page": "page/uptime"})
    status, headers, doc = b.request("GET", opened["frame"])
    assert status == 200 and headers["Content-Security-Policy"].startswith("sandbox allow-scripts;")
    assert b'rook._as("hub/workers")' in doc and b"rook._mount(" in doc
    assert b.post("/web/api/page", {"token": opened["token"], "cap": "hub.workers"})["ok"]
    assert b.post("/web/api/page", {"token": opened["token"], "cap": "task.create",
                                    "args": {"title": "x"}})["error"] == "denied: page: grants"

    assert "Archived page/uptime" in shell("agent.chat.send", agent="house",
                                            text="[site] The person is on the page page/uptime.\narchive page/uptime")["text"]
    assert [p["page"] for p in shell("page.list", archived=True)["pages"]] == ["page/uptime"]
    assert shell("page.site")["start"] is None
    assert b.post("/web/api/page", {"token": opened["token"], "cap": "hub.workers"})["error"].startswith("denied: page")
    entries = fresh.owner.get("/journal?cap=page.archive&limit=5")["entries"]
    assert entries and entries[0]["person"] == "person:bake" and entries[0]["ok"]
