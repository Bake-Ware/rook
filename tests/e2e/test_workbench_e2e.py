"""The workbench package in a real hub process: an external agent (a door
token, as an MCP client holds) works a task, and it all survives a restart."""
from __future__ import annotations

import shutil

from harness import REPO, Door

WORKBENCH = ("concepts", "knowledge", "projects", "tasks", "whiteboard")
WHY = "Work a task through the workbench."


def test_an_agent_works_a_task_and_it_survives_a_hub_restart(fresh):
    for folder in WORKBENCH:
        shutil.copytree(REPO / "rook" / "plugins" / folder, fresh.library / folder)
    agent_token = fresh.hub_cli("token", "--person", "bake", "--agent", "claude-code")
    fresh.start_hub()
    agent = Door(fresh.url, agent_token)

    def call(cap, **args):
        out = agent.call(cap, args, worker="rook", why=WHY)
        assert out.get("ok"), out
        return out["result"]

    workers = fresh.owner.get("/workers")["workers"]
    assert sorted(next(w for w in workers if w.get("hub"))["plugins"]) == \
        ["concept", "knowledge", "project", "task", "whiteboard"]
    p = call("project.create", title="Castling", concepts=["software"])
    t = call("task.create", title="Workbench in a real hub", project=p["id"])
    assert call("task.claim", id=t["id"])["task"]["claims"][0]["actor"] == "agent:claude-code"
    call("whiteboard.write", task=t["id"], text="Plan: restart the hub, read it back.")
    call("knowledge.create", title="Workbench e2e", body="It ran.[^t]",
         sources=[{"key": "t", "kind": "task", "ref": t["id"]}])

    fresh.hub.stop()
    fresh.start_hub()
    agent = Door(fresh.url, agent_token)
    assert call("whiteboard.read", task=t["id"])["text"] == "Plan: restart the hub, read it back."
    assert call("task.get", id=t["id"])["state"] == "in_progress"
    page = call("knowledge.get", id="workbench-e2e")
    assert page["sources"][0]["by"] == "agent:claude-code"
    journal = fresh.owner.get("/journal?limit=50")["entries"]
    assert {"task.create", "task.claim", "whiteboard.write", "knowledge.create"} <= {e.get("cap") for e in journal}
