"""Huddle role presets by category (docs/design/huddles.md 4.2): the
categories and presets are data (rook/hub/plugins/roles/presets/), each preset
is a deep, valid, least-privilege role; ``role.presets`` groups them; every
preset's tools render into each harness's config in its home; media roles
can never post, publish or send without a person approving the item; and the
Roles page picks a preset from one dropdown grouped under category headings."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from rook.core.authz import effective_tier
from rook.hub.plugins.roles import model
from rook.worker import rolehome
from tests.test_roles import URL, _call, hub  # noqa: F401 - the hub fixture

ROOT = Path(__file__).resolve().parents[1]

#: The roster as asked for, by category, in order.
ROSTER = {
    "planning": ["pm", "product", "analyst", "style"],
    "engineering": ["lead", "qa", "architect", "security"],
    "operations": ["release", "sre"],
    "hardware": ["cad", "pcb", "firmware", "fab"],
    "writing": ["writer", "editor"],
    "research": ["research", "ml"],
    "media": ["social", "launch", "community", "content", "comms"],
    "critique": ["skeptic"],
    "home": ["household"],
}
#: Caps that would post, publish or send something somewhere (real ones and
#: likely names for plugins a band might add).
PUBLISHING = ["social.post", "notify.send", "telegram.send", "discord.send", "huddle.post",
              "huddle.write", "chat.write", "blog.publish", "x.tweet", "forum.reply",
              "github.comment", "video.upload", "mail.send", "shell.exec", "proc.start",
              "hid.type", "ui.click", "browser.open", "task.write", "knowledge.write",
              "calendar.share", "social.schedule_post", "community.invite"]


def _copy_presets(tmp_path) -> Path:
    root = tmp_path / "presets"
    shutil.copytree(model.PRESETS_DIR, root)
    return root


# -- the data --------------------------------------------------------------------------

def test_categories_are_data_in_sort_order_and_every_preset_is_in_one():
    cats = model.categories()
    assert [c["id"] for c in cats] == list(ROSTER)
    assert [c["order"] for c in cats] == sorted(c["order"] for c in cats)
    assert all(c["name"] and c["description"] for c in cats)
    assert {c["name"] for c in cats} >= {"Product and planning", "Software engineering",
                                          "Media and promotion", "Home and family"}
    docs = model.presets()
    assert [p["id"] for p in docs] == [i for ids in ROSTER.values() for i in ids]
    assert len({p["id"] for p in docs}) == len(docs) == 25
    # role.presets' grouping: same order, empty categories left out.
    assert {c["id"]: [p["id"] for p in c["presets"]] for c in model.grouped()} == ROSTER
    assert set(model.grouped()[0]["presets"][0]) == {"id", "name", "description", "category",
                                                     "autonomy"}
    assert model.grouped(full=True)[0]["presets"][0]["charter"]


@pytest.mark.parametrize("doc", model.presets(), ids=lambda d: d["id"])
def test_every_preset_is_a_deep_valid_role(doc):
    assert model.validate(doc) == doc                          # saves as it is
    assert doc["description"] and "\n" not in doc["description"]
    assert len(doc["description"]) <= 160
    charter = doc["charter"]
    for part in ("Owns:", "Never:", "How it decides:", "Asks a person when:"):
        assert part in charter, f"{doc['id']}: charter has no {part!r}"
    assert len(charter) >= 600
    assert len(doc["concerns"]) >= 4 and len(doc["checklist"]) >= 4
    assert 2 <= len(doc["examples"]) <= 3 and all(e.endswith(("?", ".")) for e in doc["examples"])
    assert doc["seed_memory"] and doc["memory"] == "write"
    assert doc["enabled"] and not doc["host"] and not doc["backend"] and not doc["model"]
    # Least privilege: named read caps only (one family wildcard: info.*), no
    # harness file tools, and every source is within the role's own tools.
    assert doc["autonomy"] == "propose" and doc["builtin"] == []
    assert "knowledge.read" in doc["tools"]["allow"]
    for pat in doc["tools"]["allow"]:
        assert pat == "info.*" or "*" not in pat, f"{doc['id']}: {pat} is too broad"
        caps = ["info.host", "info.ping", "info.uptime"] if pat == "info.*" else [pat]
        for cap in caps:
            assert effective_tier(cap, None) == "read", f"{doc['id']}: {cap} is not a read cap"
    for src in doc["sources"]:
        assert model.in_scope(doc, src["cap"]), f"{doc['id']}: source {src['cap']} not in tools"
        assert effective_tier(src["cap"], None) == "read"
    # Generic: nothing personal, private or secret in the public repo.
    text = json.dumps(doc)
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b|@[a-z0-9-]+\.[a-z]|\{\{secret:|"
                         r"\.(lan|local|home\.arpa|ts\.net)\b|\bsk-[A-Za-z0-9]", text), doc["id"]


def test_the_shipped_files_are_only_data():
    for path in model.PRESETS_DIR.glob("*.toml"):
        tomllib.loads(path.read_text(encoding="utf-8"))
    assert {p.stem for p in model.PRESETS_DIR.glob("*.toml")} - {"categories"} == \
        {i for ids in ROSTER.values() for i in ids}


def test_the_manual_lists_every_preset_under_its_category():
    text = (ROOT / "docs/manual/huddles.md").read_text(encoding="utf-8")
    table = text[text.index("## Presets by category"):text.index("## Huddles from jobs")]
    for c in model.grouped():
        assert f"| {c['name']} |" in table
        for p in c["presets"]:
            assert f"| {p['name']} (`@{p['id']}`) | {p['description']} |" in table


def test_a_new_category_and_preset_are_just_data(tmp_path):
    root = _copy_presets(tmp_path)
    (root / "categories.toml").write_text(
        (root / "categories.toml").read_text() + '\n[[category]]\nid = "garden"\n'
        'name = "Garden"\norder = 5\ndescription = "Plants."\n\n[category.guard]\n'
        'autonomy = "propose"\ndeny = ["valve.*"]\n')
    (root / "gardener.toml").write_text(
        'id = "gardener"\nname = "Gardener"\ncategory = "garden"\norder = 1\n'
        'description = "Waters things."\ncharter = "Owns: the beds."\n'
        '[tools]\nallow = ["knowledge.read"]\ndeny = ["pump.*"]\n')
    cats, docs = model.load_presets(root)
    assert cats[0]["id"] == "garden" and docs[0]["id"] == "gardener"
    assert docs[0]["tools"]["deny"] == ["pump.*", "valve.*"]   # the guard's deny is added


@pytest.mark.parametrize("change, msg", [
    (("pm.toml", 'category = "planning"', 'category = "nope"'), "not in categories"),
    (("pm.toml", 'id = "pm"', 'id = "boss"'), "file's name"),
    (("pm.toml", 'description = "', 'summary = "'), "unknown field|description"),
    (("social.toml", 'autonomy = "propose"', 'autonomy = "act"'), "needs autonomy propose"),
    (("skeptic.toml", 'autonomy = "propose"', 'autonomy = "act"'), "needs autonomy propose"),
    (("pm.toml", "order = 10", 'order = "ten"'), "whole number"),
    (("categories.toml", 'id = "writing"', 'id = "planning"'), "share an id"),
    (("categories.toml", 'order = 50', 'order = 50\ncolour = "red"'), "a category is"),
])
def test_bad_preset_data_fails_to_load_naming_the_file(tmp_path, change, msg):
    root = _copy_presets(tmp_path)
    name, old, new = change
    text = (root / name).read_text()
    assert old in text
    (root / name).write_text(text.replace(old, new, 1))
    with pytest.raises(model.RoleError, match=msg):
        model.load_presets(root)


# -- role.presets and saving one -------------------------------------------------------

@pytest.mark.asyncio
async def test_role_presets_groups_by_category_and_a_preset_saves_whole(hub):  # noqa: F811
    got = await _call(hub, "role.presets", {})
    assert [c["id"] for c in got["categories"]] == list(ROSTER)
    media = next(c for c in got["categories"] if c["id"] == "media")
    assert media["name"] == "Media and promotion" and media["guard"]["autonomy"] == "propose"
    assert [p["id"] for p in media["presets"]] == ROSTER["media"]
    assert all(p["saved"] is False and "charter" not in p for p in media["presets"])
    full = await _call(hub, "role.presets", {"category": "hardware", "full": True})
    assert [c["id"] for c in full["categories"]] == ["hardware"]
    assert full["categories"][0]["presets"][0]["charter"].startswith("Owns:")
    one = await _call(hub, "role.presets", {"id": "analyst"})
    doc = one["preset"]
    assert doc["category"] == "planning" and doc["checklist"] and doc["seed_memory"]
    assert (await _call(hub, "role.set", {"role": doc}))["rev"] == 1
    saved = (await _call(hub, "role.get", {"id": "analyst"}))["role"]
    assert {k: v for k, v in saved.items() if k != "rev"} == doc   # every field kept
    again = await _call(hub, "role.presets", {"category": "planning"})
    assert [p["saved"] for p in again["categories"][0]["presets"]] == [False, False, True, False]
    for bad in ({"id": "nope"}, {"category": "nope"}):
        res = await hub.node.dispatch("role.presets", bad, "human:op")
        assert not res["ok"] and "no preset" in res["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pid", [i for ids in ROSTER.values() for i in ids])
async def test_every_presets_tools_render_into_each_harness(hub, tmp_path, pid):  # noqa: F811
    """A saved preset's turn offers only the hub tools its scope allows, and
    its home declares exactly those for Claude Code, Codex and OpenCode, with
    shell, editing and the web off; on the band it sees only in-scope read caps."""
    doc = (await _call(hub, "role.presets", {"id": pid}))["preset"]
    await _call(hub, "role.set", {"role": doc})
    role = hub.roles.role(pid)
    names = sorted((t.get("function") or t)["name"] for t in hub.roles.tools_for(role))
    assert "rook_call" in names and set(names) <= {"knowledge_search", "rook_workers",
                                                    "rook_call"}
    home = rolehome.ensure(tmp_path, pid)
    rolehome.render(home, role=pid, name=role["name"], rev=role["rev"], mcp_url=URL,
                    tools=names, builtin=role["builtin"], memory=role["memory"])
    mem = list(rolehome.MEMORY_TOOLS[role["memory"]])
    claude = json.loads((home / ".claude/settings.json").read_text())["permissions"]
    assert claude["allow"] == [f"mcp__rook__{n}" for n in names] + \
        [f"mcp__memory__{t}" for t in mem]
    assert set(rolehome.CLAUDE_DENY) <= set(claude["deny"]) and claude["defaultMode"] == "dontAsk"
    codex = tomllib.loads((home / ".codex/config.toml").read_text())
    assert codex["mcp_servers"]["rook"]["enabled_tools"] == names
    assert codex["mcp_servers"]["memory"]["enabled_tools"] == mem
    assert codex["sandbox_mode"] == "read-only" and codex["features"]["shell_tool"] is False
    oc = json.loads((home / rolehome.OPENCODE_CONFIG).read_text())
    assert oc["tools"] == {"*": False, **{f"rook_{n}": True for n in names},
                           **{f"memory_{t}": True for t in mem}}
    assert oc["permission"] == {"edit": "deny", "bash": "deny", "webfetch": "deny"}
    assert json.loads((home / "role.json").read_text())["tools"] == names
    # What rook_workers shows it: in-scope read caps only, never shell.
    seen = {c for w in (await hub.roles.role_workers(role))["workers"] for c in w["caps"]}
    assert "shell.exec" not in seen
    assert ("info.ping" in seen) == model.in_scope(role, "info.ping")


# -- media roles never publish on their own --------------------------------------------

MEDIA = [p for p in model.presets() if p["category"] == "media"]


@pytest.mark.parametrize("doc", MEDIA, ids=lambda d: d["id"])
def test_media_presets_draft_and_propose_only(doc):
    assert doc["autonomy"] == "propose"
    assert "never" in doc["charter"].lower() and "approv" in doc["charter"].lower()
    assert any("approv" in s.lower() for s in doc["seed_memory"] + doc["checklist"])
    for cap in PUBLISHING:
        assert not model.in_scope(doc, cap), f"{doc['id']} may call {cap}"
    # Even widened to every cap and switched to act, publishing stays denied.
    wide = model.validate({**doc, "autonomy": "act",
                           "tools": {"allow": ["*"], "deny": doc["tools"]["deny"]}})
    for cap in PUBLISHING:
        assert not model.in_scope(wide, cap), f"{doc['id']} (act, allow *) may call {cap}"


@pytest.mark.asyncio
@pytest.mark.parametrize("pid", [d["id"] for d in MEDIA])
async def test_a_media_role_cannot_post_even_when_it_acts(hub, pid):  # noqa: F811
    doc = (await _call(hub, "role.presets", {"id": pid}))["preset"]
    doc = {**doc, "autonomy": "act", "tools": {"allow": ["*"], "deny": doc["tools"]["deny"]}}
    await _call(hub, "role.set", {"role": doc})
    role = hub.roles.role(pid)
    before = len(hub.band.calls)
    for cap in ("shell.exec", "notify.send", "huddle.post"):
        why = hub.roles.scope_refusal(role, cap, None)
        assert why and "outside" in why
        reply = await hub.roles.role_call(role, "box", cap, {"text": "hello world"})
        assert not reply.get("ok", True) and reply["error"]
    assert len(hub.band.calls) == before                     # nothing reached a worker
    prompt = hub.roles.system_prompt(role, hub.roles.config_for(role))
    assert "Never call these yourself" in prompt and "*.publish*" in prompt


def test_the_home_role_never_acts_on_family_devices():
    doc = model.preset("household")
    assert doc["autonomy"] == "propose"
    for cap in ("shell.exec", "hid.type", "ui.click", "voice.speak", "notify.send",
                "agent.wake", "camera.snap", "screenshot.capture", "file.write"):
        assert not model.in_scope(model.validate({**doc, "tools": {
            "allow": ["*"], "deny": doc["tools"]["deny"]}}), cap), cap


# -- the turn: checklist, seed memory --------------------------------------------------

@pytest.mark.asyncio
async def test_a_presets_checklist_and_seed_memory_reach_its_turns(hub):  # noqa: F811
    doc = (await _call(hub, "role.presets", {"id": "release"}))["preset"]
    await _call(hub, "role.set", {"role": doc})
    role = hub.roles.role("release")
    prompt = hub.roles.system_prompt(role, hub.roles.config_for(role))
    assert "Your checklist" in prompt and doc["checklist"][0] in prompt
    assert "What you know to start with" in prompt and doc["seed_memory"][0] in prompt
    await _call(hub, "role.ask", {"id": "release", "question": "Ready to ship?"})
    seeded = [c for c in hub.band.calls if c.cap == "agenthost.role_memory_edit"]
    assert [c.args["text"] for c in seeded] == doc["seed_memory"]
    assert all(c.args["action"] == "remember" and c.args["by"] == "system:roles" and
               c.target == "w-hosta" for c in seeded)
    role = hub.roles.role("release")
    assert role["host"] == "hosta"
    assert "What you know to start with" not in hub.roles.system_prompt(
        role, hub.roles.config_for(role))                   # in its memory now
    await _call(hub, "role.ask", {"id": "release", "question": "And now?"})
    assert len([c for c in hub.band.calls if c.cap == "agenthost.role_memory_edit"]) == \
        len(doc["seed_memory"])                              # seeded once


# -- the Roles page's dropdown ---------------------------------------------------------

@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_roles_page_picks_a_preset_from_one_dropdown_grouped_by_category(tmp_path):
    src = (ROOT / "rook/web/roles.js").read_text(encoding="utf-8")
    src = re.sub(r"^const \{[^}]*\}=await import\([^;]*;$", "", src, flags=re.M)
    # A tiny h(): elements as plain objects, so the structure can be checked.
    (tmp_path / "roles.mjs").write_text(
        "const h=(tag,attrs,...kids)=>({tag,attrs:attrs||{},kids:kids.flat(9).filter(k=>k!=null&&k!==false)}),"
        "ago=0,useCss=0,btn=0,table=0,two=0,hapi=0;\n" + src, encoding="utf-8")
    groups = model.grouped()
    groups[1]["presets"][1]["saved"] = True                   # qa is saved already
    (tmp_path / "t.mjs").write_text(
        "import {presetSelect,presetDescription} from './roles.mjs';\n"
        f"const g={json.dumps(groups)};\n"
        "const s=presetSelect(g,'analyst');\n"
        "console.log(JSON.stringify({tag:s.tag,label:s.attrs['aria-label'],"
        "first:s.kids[0].attrs,groups:s.kids.slice(1).map(o=>({tag:o.tag,label:o.attrs.label,"
        "options:o.kids.map(x=>({tag:x.tag,...x.attrs}))})),"
        "about:presetDescription(g,'analyst'),none:presetDescription(g,'')}));\n",
        encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(tmp_path / "t.mjs")], capture_output=True,
                                    text=True, check=True, timeout=30).stdout)
    assert out["tag"] == "select" and out["label"] == "Add from presets"
    assert out["first"]["value"] == "" and out["first"]["selected"] is False
    assert [g["tag"] for g in out["groups"]] == ["optgroup"] * len(ROSTER)
    assert [g["label"] for g in out["groups"]] == [c["name"] for c in model.categories()]
    assert {g["label"]: [o["value"] for o in g["options"]] for g in out["groups"]} == \
        {c["name"]: ROSTER[c["id"]] for c in model.categories()}
    eng = out["groups"][1]["options"]
    assert eng[1]["disabled"] is True and eng[1]["text"] == "QA engineer (added)"
    assert eng[0]["disabled"] is False and eng[0]["title"] == model.preset("lead")["description"]
    picked = [o for g in out["groups"] for o in g["options"] if o.get("selected")]
    assert [o["value"] for o in picked] == ["analyst"]
    assert out["about"] == model.preset("analyst")["description"] and out["none"] == ""
