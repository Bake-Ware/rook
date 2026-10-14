"""The user manual: bundle parsing, the release check, the knowledge copy
(seed, update prompt, local edits) and the Help API."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import manual_check  # noqa: E402
from rook.hub.plugins.knowledge.service import KnowledgeService  # noqa: E402
from rook.hub.plugins.manual import bundle as mb  # noqa: E402
from rook.hub.plugins.manual.sync import ManualSync  # noqa: E402


def page(d: Path, pid: str, title: str, body: str, features=("f/x",), deprecated=False,
         since="0.1.0", order=1) -> Path:
    feats = "\n".join(f"  - {f}" for f in features)
    p = d / f"{pid}.md"
    p.write_text(f"---\nid: {pid}\ntitle: {title}\norder: {order}\nfeatures:\n{feats}\n"
                 f"since: {since}\ndeprecated: {str(deprecated).lower()}\n---\n# {title}\n\n{body}\n",
                 encoding="utf-8")
    return p


# -- the bundled manual -------------------------------------------------------------

def test_the_repository_manual_pages_parse_and_link():
    """What blocks a pull request: every page parses and its links resolve.
    Coverage and the CHANGELOG are advisory in CI and block only a release
    (tools/release_prep.py runs manual_check --release)."""
    problems = manual_check.check(pages_only=True)
    assert problems == [], "\n".join(problems)


def test_the_bundle_is_found_and_every_page_parses():
    b = mb.load()
    assert b.errors == [] and len(b.pages) >= 20
    assert b.directory == mb.find_dir()
    ids = [p.id for p in b.pages]
    assert ids[0] == "welcome" and "help" in ids and "README" not in ids
    for p in b.pages:
        assert p.slug == "manual-" + p.id and len(p.body) <= mb.MAX_BODY and (p.features or p.preview)


def test_front_matter_subset():
    meta, body = mb.parse_front_matter("---\nid: a\ntitle: Work: tasks\nlist: [x, y]\n"
                                       "features:\n  - f/one\n  - f/two\ndeprecated: false\n"
                                       "order: 3\n---\n\n# Body\n")
    assert meta == {"id": "a", "title": "Work: tasks", "list": ["x", "y"],
                    "features": ["f/one", "f/two"], "deprecated": False, "order": 3}
    assert body == "# Body\n"
    for bad in ("no front matter", "---\nid: a\n", "---\n  - stray\n---\n", "---\nNot a key\n---\n"):
        with pytest.raises(mb.ManualError):
            mb.parse_front_matter(bad)


def test_page_validation(tmp_path):
    page(tmp_path, "ok", "Fine", "Text.")
    (tmp_path / "named-wrong.md").write_text(
        "---\nid: other\ntitle: T\nfeatures: []\nsince: 0.1.0\ndeprecated: false\n---\nx\n")
    (tmp_path / "no-since.md").write_text("---\nid: no-since\ntitle: T\nfeatures: []\ndeprecated: false\n---\nx\n")
    (tmp_path / "README.md").write_text("contributor notes, skipped")
    b = mb.load(tmp_path)
    assert [p.id for p in b.pages] == ["ok"]
    assert any("named-wrong.md" in e and "other.md" in e for e in b.errors)
    assert any("no-since.md" in e and "since" in e for e in b.errors)


def test_content_hash_ignores_front_matter_only_edits(tmp_path):
    a = page(tmp_path, "a", "A", "Same text.", features=("f/x",))
    h1 = mb.load(tmp_path).pages[0].hash
    page(tmp_path, "a", "A", "Same text.", features=("f/x", "f/y"), order=9)
    assert mb.load(tmp_path).pages[0].hash == h1
    page(tmp_path, "a", "A", "Different text.")
    assert mb.load(tmp_path).pages[0].hash != h1 and a.exists()


# -- the checker ------------------------------------------------------------------------

def checked(d: Path, features: dict, changelog: str = "## Unreleased\n", **kw) -> list:
    return manual_check.check(d, features=features, changelog=changelog, **kw)


def test_check_coverage_both_ways(tmp_path):
    page(tmp_path, "a", "A", "See [[manual-b]].", features=("features/one", "plugin/hub/gone"))
    page(tmp_path, "b", "B", "Text.", features=("features/two",), order=2)
    feats = {"features/one": "x", "features/two": "x", "cli/new": "rook new"}
    assert checked(tmp_path, feats, pages_only=True) == []
    problems = checked(tmp_path, feats)
    assert len(problems) == 2
    assert any(p.startswith("cli/new (rook new) has no manual page") for p in problems)
    assert any(p.startswith("plugin/hub/gone (on a.md) is not a current feature") for p in problems)
    # A removed feature may stay documented on a deprecated page.
    page(tmp_path, "old", "Old", "Gone soon.", features=("plugin/hub/gone",), deprecated=True, order=3)
    page(tmp_path, "a", "A", "See [[manual-b]].", features=("features/one", "cli/new"))
    assert checked(tmp_path, feats) == []
    # A deprecated feature that still exists is covered by its deprecated page.
    assert checked(tmp_path, {**feats, "plugin/hub/gone": "x"}) == []


def test_check_links(tmp_path):
    page(tmp_path, "a", "A", "[[manual-nope]] [[rook-manual]] [[hosts-nas]] `[[in-code]]`")
    problems = checked(tmp_path, {"f/x": "x"}, pages_only=True)
    assert len(problems) == 2
    assert "[[manual-nope]] links to a page that does not exist" in problems[0]
    assert "[[hosts-nas]] is not a manual page link" in problems[1]


CHANGES = """# Changelog

## Unreleased

### Widgets
- Added the widget page, with no manual marker: not under an Added heading.
- Fixed a typo.

#### Added
- The widget page. (manual: a)
- The gadget view.

### Plumbing
#### Removed
Manual: n/a
- An unused internal module.

### Added
- A **frobnicator** on the Workers page.

## 0.0.9
- Added something long ago.
"""


def test_check_changelog_markers(tmp_path):
    page(tmp_path, "a", "A", "Text.")
    problems = checked(tmp_path, {"f/x": "x"}, CHANGES)
    assert len(problems) == 2, problems
    assert "CHANGELOG (Added): 'The gadget view.' adds or removes a feature but names no manual page" in problems[0]
    assert "(Added): 'A **frobnicator** on the Workers page.'" in problems[1]
    fixed = CHANGES.replace("- The gadget view.", "- The gadget view (manual: manual-a).")
    fixed = fixed.replace("### Added\n", "### Added\nManual: missing-page\n")
    problems = checked(tmp_path, {"f/x": "x"}, fixed)
    assert problems == ["CHANGELOG (Added): 'A **frobnicator** on the Workers page.' names manual "
                        "page 'missing-page', which does not exist"]
    bullets = manual_check.changelog_bullets(manual_check.changelog_section(CHANGES))
    # Only bullets under Added/Removed/Deprecated headings count, whatever their verb.
    assert [b["feature"] for b in bullets] == [False, False, True, True, True, True]
    assert bullets[4]["refs"] == ["n/a"] and bullets[2]["refs"] == ["a"]


def test_check_against_a_base_and_for_a_release(tmp_path):
    repo = tmp_path / "repo"
    d = repo / "docs" / "manual"
    d.mkdir(parents=True)
    git = lambda *a: subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *a],
                                    cwd=repo, check=True, capture_output=True)
    git("init", "-q")
    page(d, "a", "A", "Text.")
    page(d, "b", "B", "Text.", order=2)
    git("add", ".")
    git("commit", "-qm", "base")
    git("tag", "v0.1.0")
    page(d, "b", "B", "New text.", order=2)
    log = "## Unreleased\n### Added\nManual: a, b\n- The thing.\n"
    assert manual_check.changed_pages("v0.1.0", repo) == {"b"}
    problems = manual_check.check(d, features={"f/x": "x"}, changelog=log, base="v0.1.0", root=repo)
    assert problems == ["CHANGELOG names manual page 'a', but docs/manual/a.md did not change since v0.1.0"]
    page(d, "a", "A", "Text.", since="0.3.0")
    git("add", ".")
    git("commit", "-qm", "next")
    problems = manual_check.check(d, features={"f/x": "x"}, changelog=log, release="v0.2.0", root=repo)
    assert problems == ["a.md: since 0.3.0 is newer than the release 0.2.0"]


def test_cli_exit_codes(tmp_path, capsys):
    assert manual_check.main(["--pages"]) == 0
    assert "manual pages ok" in capsys.readouterr().out
    page(tmp_path, "a", "A", "See [[manual-gone]].")
    assert manual_check.main(["--dir", str(tmp_path), "--pages"]) == 1
    assert "The user manual is out of date" in capsys.readouterr().err
    assert manual_check.main(["--dir", str(tmp_path), "--pages", "--write"]) == 1   # written, still broken
    assert (tmp_path / "manifest.json").exists()


def test_release_prep_runs_the_manual_gate(monkeypatch, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("release_prep_m", ROOT / "tools" / "release_prep.py")
    rp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rp)
    seen = {}
    monkeypatch.setattr(manual_check, "check", lambda **kw: seen.update(kw) or ["cli/x has no manual page"])
    with pytest.raises(SystemExit) as stop:
        rp.check_manual("v0.2.0")
    assert seen == {"release": "v0.2.0"} and "cli/x has no manual page" in str(stop.value)
    b = rp.check_manual("v0.2.0", skip=True)          # the emergency override still builds the manifest
    assert "skipped" in capsys.readouterr().err
    assert b.manifest()["features"]["plugin/hub/manual"] == ["help"]


# -- the knowledge copy ----------------------------------------------------------------

def service(tmp_path) -> KnowledgeService:
    return KnowledgeService(tmp_path / "knowledge.db", lambda: {"identity": "agent:x", "kind": "agent"})


def manual_dir(tmp_path) -> Path:
    d = tmp_path / "manual"
    d.mkdir()
    page(d, "start", "Start", "Begin here. See [[manual-chat]].")
    page(d, "chat", "Chat", "Talk.", order=2)
    page(d, "old", "Old", "Going away.", order=3)
    return d


def make_sync(tmp_path, d, version="0.1.0") -> ManualSync:
    return ManualSync(lambda: mb.load(d), tmp_path / "state" / "state.json", version, "1.test.build")


HUMAN = {"id": "human:op", "kind": "human", "label": "Op"}


@pytest.mark.asyncio
async def test_seed_on_a_fresh_knowledge_base(tmp_path):
    s, d = service(tmp_path), manual_dir(tmp_path)
    sync = make_sync(tmp_path, d)
    assert sync.status(s)["prompt"] is True and sync.status(s)["fresh"]
    assert await sync.seed_if_fresh(s, auto_seed=False) is None
    res = await sync.seed_if_fresh(s)
    assert res["written"] == ["start", "chat", "old"] and not res["errors"]
    root = s.store.get("default", "rook-manual")
    assert root["title"] == "Rook manual" and "[[manual-start]] Start" in root["body"]
    assert root["attrs"]["verification"] == "verified"
    assert any(l["ref"] == "docs/manual/manifest.json" for l in root["links"])
    got = s.store.get("default", "manual-start")
    assert got["parent"] == root["id"] and got["body"].startswith("# Start")
    assert got["attrs"]["tags"] == ["rook-manual", "rook-0.1.0"]
    assert got["attrs"]["manual"]["page"] == "start" and got["attrs"]["verification"] == "verified"
    assert any(l["kind"] == "file" and l["ref"] == "docs/manual/start.md" for l in got["links"])
    st = sync.status(s)
    assert st["prompt"] is False and st["imported"]["version"] == "0.1.0"
    assert st["diff"]["unchanged"] == 3 and st["history"][-1]["event"] == "seed"
    assert await sync.seed_if_fresh(s) is None      # once


@pytest.mark.asyncio
async def test_a_knowledge_base_with_pages_is_asked_first(tmp_path):
    s, d = service(tmp_path), manual_dir(tmp_path)
    await s.dispatch("create", None, "knowledge", None, "", {"title": "Hosts", "body": "x"}, "r1")
    await s.dispatch("create", None, "knowledge", None, "", {"title": "Mine", "body": "y",
                                                             "slug": "manual-chat"}, "r2")
    sync = make_sync(tmp_path, d)
    assert await sync.seed_if_fresh(s) is None
    st = sync.status(s)
    assert st["prompt"] and [a["id"] for a in st["diff"]["added"]] == ["start", "chat", "old"]
    assert [a["local_edit"] for a in st["diff"]["added"]] == [False, True, False]
    res = await sync.apply(s, who="human:op")
    assert res["kept"] == [{"id": "chat", "why": "a page with this name already exists"}]
    assert s.store.get("default", "manual-chat")["body"] == "y"
    assert sync.status(s)["diff"]["added"][0]["id"] == "chat"      # still on offer
    await sync.apply(s, overwrite=["chat"])
    assert s.store.get("default", "manual-chat")["body"].startswith("# Chat")


@pytest.mark.asyncio
async def test_an_update_shows_the_diff_and_keeps_local_edits(tmp_path):
    s, d = service(tmp_path), manual_dir(tmp_path)
    await make_sync(tmp_path, d).seed_if_fresh(s)
    # Someone edits the Chat page in the knowledge base.
    chat = s.store.get("default", "manual-chat")
    await s.dispatch("update", None, None, "manual-chat", "",
                     {"revision": chat["revision"], "patch": {"body": chat["body"] + "\nOur notes."}},
                     "edit-1", actor=HUMAN)
    edited = s.store.get("default", "manual-chat")["attrs"]
    assert edited["verification"] == "unverified"               # no longer what the manual says
    assert edited["review_note"].startswith("Edited after it was imported from the Rook manual")
    # The next release changes Start and Chat, removes Old and adds Jobs.
    page(d, "start", "Start", "Begin here, now better.")
    page(d, "chat", "Chat", "Talk, now better.", order=2)
    (d / "old.md").unlink()
    page(d, "jobs", "Jobs", "Schedules.", order=4)
    sync = make_sync(tmp_path, d, "0.2.0")
    st = sync.status(s)
    assert st["prompt"] and st["imported"]["version"] == "0.1.0" and st["version"] == "0.2.0"
    diff = st["diff"]
    assert [(c["id"], c["local_edit"]) for c in diff["changed"]] == [("start", False), ("chat", True),
                                                                      ("_root", False)]
    assert [(c["id"], c["local_edit"]) for c in diff["removed"]] == [("old", False)]
    assert [a["id"] for a in diff["added"]] == ["jobs"] and diff["unchanged"] == 0
    assert sync.status(s, prompt_enabled=False)["prompt"] is False

    res = await sync.apply(s, who="human:op")
    assert sorted(res["written"]) == ["jobs", "start"] and res["archived"] == ["old"]
    assert res["kept"] == [{"id": "chat", "why": "edited in the knowledge base"}]
    assert s.store.get("default", "manual-start")["body"].endswith("now better.")
    assert s.store.get("default", "manual-start")["attrs"]["tags"] == ["rook-manual", "rook-0.2.0"]
    assert "Our notes." in s.store.get("default", "manual-chat")["body"]
    assert s.store.get("default", "manual-old")["state"] == "archived"
    assert "[[manual-jobs]] Jobs" in s.store.get("default", "rook-manual")["body"]
    start = s.store.get("default", "manual-start")
    assert start["attrs"]["verification"] == "verified"
    assert sum(l["kind"] == "file" for l in start["links"]) == 1      # evidence linked once
    st = sync.status(s)
    assert not st["pending"] and not st["prompt"] and st["imported"]["version"] == "0.2.0"
    # Help shows the edited copy; the bundle stays reachable.
    assert sync.page("chat", s)["edited"] and sync.page("chat", s)["source"] == "knowledge"
    assert sync.page("chat", s, source="bundle")["body"].startswith("# Chat\n\nTalk, now better.")
    toc = sync.pages(s)["pages"]
    assert [p["id"] for p in toc] == ["start", "chat", "jobs"] and toc[1]["edited"]
    # A later change to the edited page is offered again, still marked.
    page(d, "chat", "Chat", "Talk, better still.", order=2)
    diff = sync.status(s)["diff"]
    assert [(c["id"], c["local_edit"]) for c in diff["changed"]] == [("chat", True)]
    res = await sync.apply(s, overwrite=["chat"])
    chat = s.store.get("default", "manual-chat")
    assert res["written"] == ["chat"] and "Our notes." not in chat["body"]
    assert chat["attrs"]["verification"] == "verified" and "review_note" not in chat["attrs"]
    assert not sync.diff(s)["changed"]


@pytest.mark.asyncio
async def test_someone_elses_rook_manual_page_is_never_adopted(tmp_path):
    s, d = service(tmp_path), manual_dir(tmp_path)
    await s.dispatch("create", None, "knowledge", None, "",
                     {"title": "My Rook notes", "body": "mine", "slug": "rook-manual"}, "r1")
    sync = make_sync(tmp_path, d)
    res = await sync.apply(s, who="human:op")
    assert res["root"] == "rook-manual-2" and not res["kept"]
    assert s.store.get("default", "rook-manual")["body"] == "mine"
    root = s.store.get("default", "rook-manual-2")
    assert root["title"] == "Rook manual" and s.store.get("default", "manual-start")["parent"] == root["id"]
    assert sync.status(s)["imported"]["slug"] == "rook-manual-2"


@pytest.mark.asyncio
async def test_the_contents_page_is_only_rewritten_with_consent(tmp_path):
    s, d = service(tmp_path), manual_dir(tmp_path)
    sync = make_sync(tmp_path, d)
    await sync.seed_if_fresh(s)
    root = s.store.get("default", "rook-manual")
    await s.dispatch("update", None, None, "rook-manual", "",
                     {"revision": root["revision"], "patch": {"body": root["body"] + "\nOperator notes."}},
                     "edit-root", actor=HUMAN)
    page(d, "jobs", "Jobs", "Schedules.", order=4)           # the contents change
    diff = sync.diff(s)
    assert [(c["id"], c["local_edit"]) for c in diff["changed"]] == [("_root", True)]
    res = await sync.apply(s, who="human:op")
    assert res["written"] == ["jobs"] and res["kept"] == [{"id": "_root", "why": "edited in the knowledge base"}]
    assert "Operator notes." in s.store.get("default", "rook-manual")["body"]
    assert s.store.get("default", "manual-jobs")["parent"] == root["id"]
    assert not sync.diff(s)["changed"]                           # asked once, not every start
    page(d, "old", "Old", "Still here, reworded.", order=3)
    page(d, "jobs", "Jobs and schedules", "Schedules.", order=4)  # a new title: contents change again
    assert ("_root", True) in [(c["id"], c["local_edit"]) for c in sync.diff(s)["changed"]]
    await sync.apply(s, overwrite=["_root"])
    body = s.store.get("default", "rook-manual")["body"]
    assert "Operator notes." not in body and "Jobs and schedules" in body


@pytest.mark.asyncio
async def test_tag_edits_count_and_user_tags_are_kept(tmp_path):
    s, d = service(tmp_path), manual_dir(tmp_path)
    sync = make_sync(tmp_path, d)
    await sync.seed_if_fresh(s)
    start = s.store.get("default", "manual-start")
    await s.dispatch("update", None, None, "manual-start", "",
                     {"revision": start["revision"],
                      "patch": {"attrs": {"tags": start["attrs"]["tags"] + ["onboarding"]}}},
                     "tag-1", actor=HUMAN)
    page(d, "start", "Start", "Begin here, reworded.")
    assert [(c["id"], c["local_edit"]) for c in sync.diff(s)["changed"]] == [("start", True)]
    assert (await sync.apply(s))["kept"] == [{"id": "start", "why": "edited in the knowledge base"}]
    sync2 = make_sync(tmp_path, d, "0.2.0")
    page(d, "start", "Start", "Begin here, reworded again.")
    await sync2.apply(s, overwrite=["start"])
    assert s.store.get("default", "manual-start")["attrs"]["tags"] == ["rook-manual", "rook-0.2.0", "onboarding"]


@pytest.mark.asyncio
async def test_not_now_and_never_for_this_version(tmp_path, monkeypatch):
    s, d = service(tmp_path), manual_dir(tmp_path)
    sync = make_sync(tmp_path, d)
    await sync.seed_if_fresh(s)
    page(d, "chat", "Chat", "Changed.", order=2)
    assert sync.status(s)["prompt"]
    sync.snooze(24, who="human:op")
    assert not sync.status(s)["prompt"] and sync.status(s)["pending"]
    import rook.hub.plugins.manual.sync as sync_mod
    real = sync_mod._now
    monkeypatch.setattr(sync_mod, "_now", lambda: real() + 25 * 3600)
    assert sync.status(s)["prompt"]
    sync.decline(who="human:op")
    assert not sync.status(s)["prompt"] and sync.status(s)["declined"]
    page(d, "chat", "Chat", "Changed again.", order=2)     # the next manual change asks again
    assert sync.status(s)["prompt"]


def test_without_the_knowledge_base_help_reads_the_bundle(tmp_path):
    d = manual_dir(tmp_path)
    sync = make_sync(tmp_path, d)
    st = sync.status(None)
    assert st["knowledge"] == "off" and not st["prompt"] and not st["pending"]
    assert sync.page("start")["source"] == "bundle"
    assert [p["source"] for p in sync.pages()["pages"]] == ["bundle"] * 3
    with pytest.raises(ValueError):
        sync.page("nope")


# -- the hub plugin and its API --------------------------------------------------------

@pytest.fixture
def hub(tmp_path, monkeypatch):
    from tests.plugins_fakes import make_node
    monkeypatch.setenv("ROOK_KNOWLEDGE", "1")
    monkeypatch.setenv("ROOK_MANUAL_DIR", str(manual_dir(tmp_path)))
    return make_node(tmp_path)


@pytest.mark.asyncio
async def test_plugin_seeds_and_serves_the_api(hub):
    await hub.start()
    try:
        plugin = hub.plugin("manual")
        assert plugin is not None and plugin.knowledge() is not None
        assert (await plugin.seed_once())["written"] == ["start", "chat", "old"]
        assert (await hub.invoke("manual.get", {"id": "manual-start"}))["source"] == "knowledge"
        assert (await hub.invoke("manual.status", {}))["imported"]["slug"] == "rook-manual"
        # The agent hint rides the rook_knowledge tool's description.
        assert "rook-manual" in hub.guidance_defaults()["tool:rook_knowledge"]

        from rook.hub.plugins.manual.web import routes
        accounts = SimpleNamespace(session=lambda c: {"id": c, "username": c, "csrf": "k",
                                                      "admin": c == "op"} if c else None)
        app = Starlette(routes=routes(lambda: hub.plugin("manual"), accounts))
        api = "/manual/account-api"
        op, member = {"Cookie": "rook_account=op"}, {"Cookie": "rook_account=m1"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get(api)).status_code == 401
            mine = (await c.get(api, headers=member)).json()
            assert mine["prompt"] is False and "diff" not in mine and mine["admin"] is False
            toc = (await c.get(api, headers=member, params={"view": "pages", "full": "1"})).json()
            assert [p["id"] for p in toc["pages"]] == ["start", "chat", "old"]
            assert toc["pages"][0]["body"].startswith("# Start")
            # Members read the manual as Rook ships it; the knowledge copy (which
            # may hold the operator's notes) is the operator's.
            assert {p["source"] for p in toc["pages"]} == {"bundle"}
            one = (await c.get(api, headers=member, params={"view": "page", "id": "chat",
                                                            "source": "auto"})).json()
            assert one["title"] == "Chat" and one["source"] == "bundle"
            mine = (await c.get(api, headers=op, params={"view": "page", "id": "chat"})).json()
            assert mine["source"] == "knowledge"
            ops = (await c.get(api, headers=op, params={"view": "pages", "full": "1"})).json()
            assert {p["source"] for p in ops["pages"]} == {"knowledge"}
            assert (await c.get(api, headers=member, params={"view": "page", "id": "x"})).status_code == 400
            assert (await c.post(api, headers=member, json={"csrf": "k", "action": "snooze"})).status_code == 403
            assert (await c.post(api, headers=op, json={"action": "snooze"})).status_code == 403  # csrf
            r = await c.post(api, headers=op, json={"csrf": "k", "action": "decline"})
            assert r.status_code == 200 and r.json()["result"]["declined"]["version"]
            r = await c.post(api, headers=op, json={"csrf": "k", "action": "update"})
            assert r.json()["ok"] and r.json()["result"]["status"]["imported"]
            assert (await c.post(api, headers=op, json={"csrf": "k", "action": "nope"})).status_code == 400
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_the_seed_loop_stops_on_a_knowledge_base_with_pages(hub):
    await hub.start()
    try:
        plugin = hub.plugin("manual")
        await plugin.stop()                                   # drive the loop by hand
        await plugin.knowledge().dispatch("create", None, "knowledge", None, "",
                                          {"title": "Hosts", "body": "x"}, "r1")
        await asyncio.wait_for(plugin._seed_loop(), 10)     # one try, then it leaves it to the prompt
        assert not plugin._sync().state().get("imported") and plugin.status_view()["prompt"]
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_dashboard_proxies_the_manual_api():
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from rook.remote.knowledge_web import ManualWeb

    seen = {}

    async def upstream(request):
        seen["query"] = dict(request.query)
        return web.json_response({"pages": [], "csrf": "k"})

    up = web.Application()
    up.router.add_get("/manual/account-api", upstream)
    async with TestClient(TestServer(up)) as upc:
        account = SimpleNamespace(require=lambda r: {"id": "m1", "admin": False}, csrf=lambda *a: None)
        proxy = ManualWeb(account)
        proxy.url = str(upc.make_url("/manual/account-api"))
        app = web.Application()
        proxy.install(app)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/account/manual/api?view=pages")
            assert r.status == 200 and seen["query"] == {"view": "pages"}   # members may read
            assert (await c.get("/account/manual/assets/manual.js")).status == 200
            assert (await c.get("/account/manual/assets/other.js")).status == 404


def test_a_dashboard_login_carries_over_to_the_manual_api():
    from rook.remote.bootstrap import CombinedServer
    assert "/account/manual/api" in CombinedServer._BRIDGE_PATHS


def test_guidance_defaults_prefer_text_over_an_empty_slot():
    from rook.hub.node import HubNode
    a = SimpleNamespace(GUIDANCE={"tool:t": "", "x.": "cap tip"})
    b = SimpleNamespace(GUIDANCE={"tool:t": "the hint", "x.": "other"})
    fake = SimpleNamespace(host=SimpleNamespace(plugins=[a, b]))
    assert HubNode.guidance_defaults(fake) == {"tool:t": "the hint", "cap:x.": "cap tip"}


def test_the_manifest_is_built_not_committed():
    """Generated by manual_check --write and release_prep, so pull requests
    that touch docs/manual don't conflict on it."""
    tracked = subprocess.run(["git", "ls-files", "docs/manual/manifest.json"], cwd=ROOT,
                             capture_output=True, text=True).stdout
    assert tracked == "" and "docs/manual/manifest.json" in (ROOT / ".gitignore").read_text()
    m = mb.load().manifest()
    assert m["format"] == "rook-manual/1" and json.loads(json.dumps(m)) == m
