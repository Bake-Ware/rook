"""rook.kit.records: storage, migration, search, history, privacy, refs, generated caps."""

import pytest

from rook.kit.ctx import Caller, Ctx, KitError
from rook.kit.records import RecordDef, RecordStore, record_caps

BAKE = Caller(person="bake", agent="claude", band="b1", why="test")
KID = Caller(person="kid", band="b1")
OTHER_BAND = Caller(person="bake", band="b2")
OWNER = Caller(person="bake", band="b1", owner=True)

HOMEWORK = {
    "plugin": "homework",
    "records": {
        "item": {
            "fields": {
                "title": {"type": "str", "required": True},
                "slug": {"type": "str", "unique": True},
                "body": {"type": "markdown"},
                "due": {"type": "time"},
                "done": {"type": "bool", "default": False},
                "tags": {"type": "list"},
                "subject": {"type": "ref", "to": "subject"},
            },
            "index": ["due"],
            "search": ["title", "body"],
            "history": True,
            "risk": {"delete": "admin"},
        },
        "subject": {"fields": {"name": {"required": True}}, "caps": ["list", "get", "create"]},
        "note": {"fields": {"text": {}}, "private": True, "retain": "30d"},
    },
}


@pytest.fixture
def events():
    return []


@pytest.fixture
def store(events):
    s = RecordStore(emit=lambda e, p, src: events.append((e, p, src)))
    s.register(HOMEWORK)
    yield s
    s.close()


def test_create_get_list(store, events):
    items = store.type("homework.item")
    a = items.create(BAKE, title="Fractions", slug="frac", tags=["math"], due="2026-11-02")
    assert a["revision"] == 1 and a["creator"] == "claude" and a["done"] is False
    assert a["tags"] == ["math"] and a["archived"] is None
    assert items.get(BAKE, a["id"])["title"] == "Fractions"
    items.create(BAKE, title="Essay", due="2026-11-01")
    assert [r["title"] for r in items.list(BAKE, order="due")] == ["Essay", "Fractions"]
    assert [r["title"] for r in items.list(BAKE, where={"slug": "frac"})] == ["Fractions"]
    assert items.list(OTHER_BAND) == []                  # per-band
    with pytest.raises(KitError):
        items.get(OTHER_BAND, a["id"])
    assert events[0][0] == "homework.item.created" and events[0][2] == "homework"
    assert events[0][1]["record"]["title"] == "Fractions"


def test_validation(store):
    items = store.type("homework.item")
    with pytest.raises(KitError, match="title is required"):
        items.create(BAKE, slug="x")
    with pytest.raises(KitError, match="expected bool"):
        items.create(BAKE, title="x", done="yes")
    with pytest.raises(KitError, match="no field"):
        items.create(BAKE, title="x", colour="red")
    with pytest.raises(KitError, match="order"):
        items.list(BAKE, order="nope")


def test_unique_per_band(store):
    items = store.type("homework.item")
    items.create(BAKE, title="a", slug="s")
    with pytest.raises(KitError, match="slug is already used"):
        items.create(BAKE, title="b", slug="s")
    items.create(OTHER_BAND, title="c", slug="s")        # other band is fine


def test_history_and_optimistic_concurrency(store, events):
    items = store.type("homework.item")
    a = items.create(BAKE, title="v1")
    with pytest.raises(KitError, match="revision"):
        items.update(BAKE, a["id"], title="v2")           # history needs the revision
    b = items.update(BAKE, a["id"], revision=1, title="v2")
    assert b["revision"] == 2
    with pytest.raises(KitError, match="Record changed") as e:
        items.update(BAKE, a["id"], revision=1, title="stale")
    assert e.value.retry
    hist = items.get(BAKE, a["id"], history=True)["history"]
    assert [(h["revision"], h["op"], h["data"]["title"]) for h in hist] == [(1, "create", "v1"), (2, "update", "v2")]
    assert events[-1][0] == "homework.item.updated"
    with pytest.raises(KitError, match="required"):
        items.update(BAKE, a["id"], revision=2, title=None)


def test_no_history_revision_optional(store):
    subjects = store.type("homework.subject")
    s = subjects.create(BAKE, name="Math")
    with pytest.raises(KitError, match="keeps no history"):
        subjects.history(BAKE, s["id"])
    notes = store.type("homework.note")
    n = notes.create(BAKE, text="a")
    assert notes.update(BAKE, n["id"], text="b")["revision"] == 2
    with pytest.raises(KitError, match="Record changed"):
        notes.update(BAKE, n["id"], revision=1, text="c")


def test_search(store):
    items = store.type("homework.item")
    a = items.create(BAKE, title="Long division", body="Divide **big** numbers")
    items.create(BAKE, title="Poems", body="Rhymes and verses")
    assert [r["id"] for r in items.search(BAKE, "divide numbers")] == [a["id"]]
    assert items.search(BAKE, 'NEAR("x" OR') == []        # FTS syntax is just words
    assert items.search(OTHER_BAND, "divide") == []
    items.update(BAKE, a["id"], revision=1, body="Split things")
    assert items.search(BAKE, "divide") == []
    assert len(items.search(BAKE, "split")) == 1
    items.archive(BAKE, a["id"], revision=2)
    assert items.search(BAKE, "split") == []              # archived are out of search
    with pytest.raises(KitError, match="query"):
        items.search(BAKE, "  ")
    with pytest.raises(KitError, match="semantic"):
        items.search(BAKE, "x", semantic=True)


def test_archive_restore_delete(store, events):
    items = store.type("homework.item")
    a = items.create(BAKE, title="x")
    arch = items.archive(BAKE, a["id"], revision=1)
    assert arch["archived"] is not None and events[-1][0] == "homework.item.archived"
    assert items.list(BAKE) == [] and len(items.list(BAKE, archived=True)) == 1
    back = items.archive(BAKE, a["id"], revision=2, restore=True)
    assert back["archived"] is None and events[-1][0] == "homework.item.updated"
    assert items.delete(BAKE, a["id"]) == {"id": a["id"], "deleted": True}
    with pytest.raises(KitError):
        items.get(BAKE, a["id"])


def test_private(store, events):
    notes = store.type("homework.note")
    n = notes.create(BAKE, text="mine")
    assert notes.list(KID) == []
    with pytest.raises(KitError):
        notes.get(KID, n["id"])
    with pytest.raises(KitError):
        notes.update(KID, n["id"], text="hacked")
    assert notes.get(Caller(person="bake", band="b1"), n["id"])["text"] == "mine"  # creator's person
    assert notes.get(OWNER, n["id"])["text"] == "mine"
    with pytest.raises(KitError):                         # same agent, other person
        notes.get(Caller(person="kid", agent="claude", band="b1"), n["id"])
    solo = notes.create(Caller(agent="janitor", band="b1"), text="agent's own")
    assert notes.get(Caller(agent="janitor", band="b1"), solo["id"])["person"] is None
    with pytest.raises(KitError):
        notes.get(BAKE, solo["id"])
    assert "record" not in events[-1][1]                  # private data stays out of events


def test_refs_and_backlinks(store):
    subjects, items, notes = (store.type(f"homework.{t}") for t in ("subject", "item", "note"))
    math = subjects.create(BAKE, name="Math")
    with pytest.raises(KitError, match="no homework.subject"):
        items.create(BAKE, title="x", subject="r_missing")
    a = items.create(BAKE, title="Fractions", subject=math["id"])
    got = subjects.get(BAKE, math["id"], backlinks=True)
    assert got["backlinks"] == [{"type": "homework.item", "id": a["id"], "field": "subject"}]
    items.update(BAKE, a["id"], revision=1, subject=None)
    assert store.backlinks(BAKE, "homework.subject", math["id"]) == []


def test_cross_plugin_refs_and_private_backlinks():
    s = RecordStore()
    s.register({"plugin": "workbench", "records": {"project": {"fields": {"title": {}}}}})
    s.register({"plugin": "diary", "records": {"entry": {
        "fields": {"text": {}, "project": {"type": "ref", "to": "workbench.project"}}, "private": True}}})
    p = s.type("workbench.project").create(BAKE, title="Castling")
    e = s.type("diary.entry").create(BAKE, text="hi", project=p["id"])
    assert s.backlinks(BAKE, "workbench.project", p["id"]) == [{"type": "diary.entry", "id": e["id"], "field": "project"}]
    assert s.backlinks(KID, "workbench.project", p["id"]) == []   # private source hidden
    assert len(s.backlinks(Caller(person="bake", band="b1"), "workbench.project", p["id"])) == 1
    assert len(s.backlinks(OWNER, "workbench.project", p["id"])) == 1


def test_retain_prune(events):
    now = [1_000_000.0]
    s = RecordStore(clock=lambda: now[0])
    s.register(HOMEWORK)
    notes = s.type("homework.note")
    old = notes.create(BAKE, text="old")
    now[0] += 31 * 86400
    fresh = notes.create(BAKE, text="fresh")
    s.type("homework.item").create(BAKE, title="kept forever")
    assert s.prune() == 1
    assert [n["id"] for n in notes.list(OWNER)] == [fresh["id"]]
    assert len(s.type("homework.item").list(BAKE)) == 1
    with pytest.raises(KitError):
        notes.get(BAKE, old["id"])


def test_additive_migration(tmp_path):
    db = str(tmp_path / "records.db")
    v1 = {"plugin": "p", "records": {"thing": {"fields": {"a": {}, "b": {}}, "search": ["a"]}}}
    s = RecordStore(db)
    s.register(v1)
    t = s.type("p.thing").create(BAKE, a="alpha", b="beta")
    s.close()

    v2 = {"plugin": "p", "records": {"thing": {
        "fields": {"a": {"unique": True}, "c": {"type": "int"}},       # b dropped, c added
        "search": ["a"], "index": [["a", "c"]]}}}
    s = RecordStore(db)
    s.register(v2)
    got = s.type("p.thing").get(BAKE, t["id"])
    assert got["a"] == "alpha" and got["c"] is None and "b" not in got
    s.type("p.thing").update(BAKE, t["id"], c=3)
    cols = [r[1] for r in s._db.execute('PRAGMA table_info("r:p.thing")')]
    assert "b" in cols                                    # data kept for a rollback
    s.close()

    s = RecordStore(db)                                   # roll back to v1: still works
    s.register(v1)
    assert s.type("p.thing").get(BAKE, t["id"])["b"] == "beta"
    s.close()

    v3 = {"plugin": "p", "records": {"thing": {"fields": {"a": {"type": "int"}}}}}
    s = RecordStore(db)
    with pytest.raises(KitError, match="not additive"):
        s.register(v3)
    ran = []
    s.register(v3, migrate=lambda db, rdef, old: ran.append(old["a"]["type"]))
    assert ran == ["str"]
    s.close()


def test_search_fields_change_rebuilds_index(tmp_path):
    db = str(tmp_path / "r.db")
    s = RecordStore(db)
    s.register({"plugin": "p", "records": {"t": {"fields": {"a": {}, "b": {}}, "search": ["a"]}}})
    s.type("p.t").create(BAKE, a="apple", b="banana")
    assert s.type("p.t").search(BAKE, "banana") == []
    s.close()
    s = RecordStore(db)
    s.register({"plugin": "p", "records": {"t": {"fields": {"a": {}, "b": {}}, "search": ["a", "b"]}}})
    assert len(s.type("p.t").search(BAKE, "banana")) == 1


def test_unique_on_duplicates_refused():
    s = RecordStore()
    s.register({"plugin": "p", "records": {"t": {"fields": {"a": {}}}}})
    s.type("p.t").create(BAKE, a="x")
    s.type("p.t").create(BAKE, a="x")
    with pytest.raises(KitError, match="unique"):
        s.register({"plugin": "p", "records": {"t": {"fields": {"a": {"unique": True}}}}})


def test_bad_definitions():
    for block in ({"fields": {}}, {"fields": {"id": {}}}, {"fields": {"a": {"type": "ref"}}},
                  {"fields": {"a": {}}, "index": ["zz"]}, {"fields": {"a": {}}, "caps": ["search"]},
                  {"fields": {"a": {}}, "caps": ["frob"]}):
        with pytest.raises(KitError):
            RecordDef.parse("p", "t", block)
    with pytest.raises(KitError):
        RecordDef.parse("p", "Bad-Name", {"fields": {"a": {}}})


class FakeSemantic:
    def __init__(self):
        self.docs = {}

    def index(self, rtype, rid, text):
        self.docs[rid] = text

    def forget(self, rtype, rid):
        self.docs.pop(rid, None)

    def search(self, rtype, query, limit):
        return [rid for rid, text in self.docs.items() if query in text][:limit]


def test_semantic_hook():
    sem = FakeSemantic()
    s = RecordStore(semantic=sem)
    s.register({"plugin": "kb", "records": {"page": {"fields": {"body": {}}, "semantic": ["body"]}}})
    pages = s.type("kb.page")
    p = pages.create(BAKE, body="the cat sat")
    assert [r["id"] for r in pages.search(BAKE, "cat", semantic=True)] == [p["id"]]
    pages.delete(BAKE, p["id"])
    assert sem.docs == {}


def test_ctx_records_binding(store):
    ctx = Ctx({"plugin": "homework"}, BAKE, records=store)
    a = ctx.records.item.create(title="via ctx")
    assert ctx.records["item"].get(a["id"])["creator"] == "claude"
    with pytest.raises(KitError):
        ctx.records.unknown
    other = Ctx({"plugin": "other"}, BAKE, records=store)
    with pytest.raises(KitError):
        other.records.item                                # only its own types


def test_generated_caps(store):
    caps = record_caps(store, "homework")
    assert set(caps) == {f"homework.item.{v}" for v in ("list", "get", "search", "create", "update", "archive", "delete")} \
        | {"homework.subject.list", "homework.subject.get", "homework.subject.create"} \
        | {f"homework.note.{v}" for v in ("list", "get", "create", "update", "archive", "delete")}
    spec = lambda n: caps[n].spec  # noqa: E731
    assert spec("homework.item.list")["risk"] == "read" and spec("homework.item.list")["idempotent"]
    assert spec("homework.item.create")["risk"] == "write"
    assert spec("homework.item.delete")["risk"] == "admin"                # override
    assert spec("homework.item.delete")["effects"] == ["destructive"]
    assert spec("homework.note.delete")["risk"] == "write"
    assert spec("homework.note.list")["scope"] == "self" and spec("homework.item.list")["scope"] == "multi-user"
    assert spec("homework.item.create")["args"]["title"]["required"] is True
    assert "required" not in spec("homework.item.update")["args"]["title"]
    assert spec("homework.item.update")["args"]["revision"]["required"] is True
    assert spec("homework.note.update")["args"]["revision"]["required"] is False
    assert spec("homework.item.create")["args"]["subject"]["to"] == "homework.subject"

    ctx = Ctx({"plugin": "homework"}, BAKE, records=store)
    made = caps["homework.item.create"].handler(ctx, title="From a cap")
    assert caps["homework.item.get"].handler(ctx, id=made["id"])["title"] == "From a cap"
    assert len(caps["homework.item.search"].handler(ctx, query="cap")) == 1
    caps["homework.item.update"].handler(ctx, id=made["id"], revision=1, done=True)
    assert caps["homework.item.list"].handler(ctx, where={"done": True})[0]["id"] == made["id"]
    caps["homework.item.archive"].handler(ctx, id=made["id"], revision=2)
    caps["homework.item.delete"].handler(ctx, id=made["id"])
    assert caps["homework.item.list"].handler(ctx, archived=True) == []


def test_unregister_keeps_data(store):
    a = store.type("homework.item").create(BAKE, title="keep")
    store.unregister("homework")
    with pytest.raises(KitError):
        store.type("homework.item")
    assert record_caps(store, "homework") == {}
    store.register(HOMEWORK)
    assert store.type("homework.item").get(BAKE, a["id"])["title"] == "keep"


def test_search_over_a_list_field():
    s = RecordStore()
    s.register({"plugin": "p", "records": {"t": {"fields": {"tags": {"type": "list"}}, "search": ["tags"]}}})
    s.type("p.t").create(BAKE, tags=["garden", "tomato"])
    assert len(s.type("p.t").search(BAKE, "tomato")) == 1


def test_threads_share_the_store(tmp_path):
    import threading
    s = RecordStore(str(tmp_path / "r.db"))
    s.register({"plugin": "p", "records": {"t": {"fields": {"n": {"type": "int"}}, "search": ["n"]}}})
    t = s.type("p.t")
    errors = []

    def work(k):
        try:
            for i in range(25):
                r = t.create(BAKE, n=k * 100 + i)
                t.update(BAKE, r["id"], revision=1, n=r["n"] + 1)
                t.list(BAKE, limit=5)
        except Exception as e:      # pragma: no cover - the assertion reports it
            errors.append(e)

    threads = [threading.Thread(target=work, args=(k,)) for k in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert errors == [] and len(t.list(BAKE, limit=500)) == 100
