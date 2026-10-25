"""Merges without votes, the house agent's playbook and the plain wording of
shell asks with advanced security off (the default; knowledge
change-management-design-20261025). The old behaviour, with the plugin on, is
what test_table_votes, test_front_door and test_plain_asks keep checking."""
from __future__ import annotations

import pytest

from tests.test_trust import seeded


# -- merges without votes ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_merge_without_votes_needs_the_self_review_then_goes_on_the_gates(table):
    from rook.hub.plugins.huddle.table import TableError
    from tests.test_table_votes import _huddle, _merges, _settle, _texts
    room = await _huddle(table)
    pinned = await table.t.pin(room, "agent:home", {"kind": "pr", "repo": "Bake-Ware/rook",
                                                    "number": 7})
    with pytest.raises(TableError, match="self_review"):
        await table.t.call(room, "token:claude", {"kind": "merge",
                                                  "artifact": pinned["artifact"]})
    review = ("Read the whole diff: one docs line, no code, nothing loads it; CI green; "
              "worst case a typo in docs/x.md, reverted with one commit.")
    out = await table.t.call(room, "token:claude", {"kind": "merge", "artifact":
                                                    pinned["artifact"], "self_review": review})
    await _settle(table)
    m = table.t.motion(out["motion"])
    assert m["state"] == "executed" and not out["seats"], m
    assert _merges(table)[0][3]["sha"] == pinned["version"]
    assert any(t.startswith("Self-review for merge") and "one docs line" in t
               for _, t in _texts(table, room))


@pytest.fixture
def table(hub):  # noqa: F811
    from tests.test_table_votes import TOKEN, FakeGitHub
    from rook.hub.plugins.huddle import table as TB
    hub.node.settings.set("home.front_door", True, actor="human:op")
    hub.node.plugin("trust")._store = seeded()
    hub.node._vault.data["hub-github"] = TOKEN
    hub.node.settings.set("huddle.vote_github_secret", "hub-github", actor="human:op")
    hub.node.settings.set("huddle.vote_gate_wait_minutes", 0, actor="human:op")
    gh = FakeGitHub()
    t = hub.svc.table
    t.github = TB.GitHub(t._token, gh.http)
    hub.gh, hub.t = gh, t
    return hub


from tests.test_huddle_chair import hub  # noqa: E402,F401


# -- wording and the playbook --------------------------------------------------------------------

def test_shell_asks_say_change_or_destroy_never_read_only():
    from rook.hub import plain
    s = plain.shell_summary({"argv": ["cat", "/etc/hostname"]})
    assert (s["kind"], s["title"]) == ("command", "Read hostname")
    assert plain.risk_text("command", "exec") == "Medium: it runs a command."
    assert plain.shell_summary({"cmd": "rm -rf /home/bake"})["kind"] == "destroy"
    assert plain.risk_text("change", "exec", hold_kind="default",
                           reason="hard stop: it reveals a secret's value").endswith(
        "it reveals a secret's value, which always asks you.")


def test_retired_playbook_defaults_no_longer_ask(tmp_path):
    from rook.hub.plugins.home.playbook import Playbook
    path = tmp_path / "playbook.md"
    path.write_text("## Ask me first\n"
                    "- [pb-aaaa] Anything that deletes data, spends money, or sends a message to "
                    "someone other than me. -- default\n"
                    "- [pb-bbbb] Anything on a family device. -- default\n"
                    "- [pb-cccc] Anything on a family device. -- Bake\n")
    texts = [(r.id, r.text) for r in Playbook(str(path)).rules()]
    assert texts == [("pb-aaaa", "Anything that spends money."),
                     ("pb-cccc", "Anything on a family device.")]
