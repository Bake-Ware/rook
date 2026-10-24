"""attention.* (Android APK plugin), notify.post channels and device.update channel,
against fake Kotlin bridges; no device needed."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

PY = Path(__file__).parents[1] / "android/app/src/main/python"


def _load(monkeypatch, name, ctx=None, jclass=None):
    monkeypatch.setitem(sys.modules, "rook_android.androidctx",
                        SimpleNamespace(app_context=lambda: ctx, jclass=jclass, cast=None))
    spec = importlib.util.spec_from_file_location(f"{name}_test", PY / f"rook_android/plugins/{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def mod(monkeypatch):
    return _load(monkeypatch, "attention_android")


class FakeStore:
    """Mimics AttentionStore's JSON-string API."""

    def __init__(self):
        self.calls = []

    def post(self, ctx, payload):
        self.calls.append(("post", json.loads(payload)))
        return json.dumps({"ok": True, "id": json.loads(payload)["id"], "new": True})

    def update(self, ctx, id, state, message):
        self.calls.append(("update", id, state, message))
        return json.dumps({"ok": True, "id": id})

    def poll(self, ctx, since):
        self.calls.append(("poll", since))
        return json.dumps({"ok": True, "events": [{"seq": since + 1, "ts": 1.0, "type": "unlocked"}], "last": since + 1})

    def feed(self, ctx, limit):
        self.calls.append(("feed", limit))
        return json.dumps({"ok": True, "entries": [], "ignored_7d": 3})

    def rulesGet(self, ctx):
        return json.dumps({"ok": True, "source": "default", "rules": {"rules": []}})

    def rulesSet(self, ctx, payload):
        self.calls.append(("rulesSet", payload))
        return json.dumps({"ok": True, "source": "override" if payload else "default"})


def plugin(mod, monkeypatch, store=None):
    store = store or FakeStore()
    monkeypatch.setattr(mod, "_Store", store)
    monkeypatch.setattr(mod, "app_context", lambda: object())
    return mod.AndroidAttentionPlugin(), store


def test_clean_post_defaults_and_clips(mod):
    c = mod.clean_post("hold-1", line="Restart the hub?  \n now", explain="x" * 700)
    assert c["kind"] == "card" and c["channel"] == "approvals" and c["urgency"] == "normal"
    assert c["actions"] == ["yes", "no", "explain"]
    assert c["line"] == "Restart the hub? now"
    assert len(c["explain"]) == 600 and c["explain"].endswith("…")
    assert len(mod.clean_post("a", line="y" * 300)["line"]) == 140
    assert mod.clean_post("a", kind="alert", line="Fire drill")["actions"] == ["ack"]
    # Order follows the contract, duplicates and case collapse.
    assert mod.clean_post("a", line="l", actions="no, YES,no")["actions"] == ["yes", "no"]
    assert mod.clean_post("a", line="l", actions=[])["actions"] == ["ack"]


def test_a_decision_card_from_the_hub_never_expires_on_the_phone(mod, monkeypatch):
    """The hub sends decision cards with expires_at 0 (Bake: no expiry of any
    kind); the app keeps 0 as "never" (the Kotlin side: AttentionModel.expired
    and AttentionChannels only act on expires_at > 0, AttentionModelTest)."""
    from rook.hub.plugins.attention.service import card_for
    card = card_for({"id": "c_1", "kind": "card", "who": "qa", "what": "rerun", "created": 1.0})
    assert card["expires_at"] == 0
    assert mod.clean_post(**card)["expires_at"] == 0.0
    p, store = plugin(mod, monkeypatch)
    assert p._post(**card)["ok"] is True and store.calls[-1][1]["expires_at"] == 0.0


def test_clean_post_body_keeps_lines(mod):
    # Old hubs send no body: the card is unchanged apart from an empty body.
    assert mod.clean_post("a", line="Read hub approvals database")["body"] == ""
    body = ("For: Approval cards plain (task)\n"
            "Who:   Claude web\n\n"
            "  I want to: run a read-only sqlite query on the hub server bakenetcanada\n"
            "Risk: Low: read-only, but the hub is guarded so changes there ask you\n")
    c = mod.clean_post("a", line="Read hub approvals database", body=body, explain="sqlite3 -readonly ...")
    assert c["body"].split("\n") == [
        "For: Approval cards plain (task)",
        "Who: Claude web",
        "I want to: run a read-only sqlite query on the hub server bakenetcanada",
        "Risk: Low: read-only, but the hub is guarded so changes there ask you",
    ]
    assert c["line"] == "Read hub approvals database" and c["explain"] == "sqlite3 -readonly ..."
    many = mod.clean_post("a", line="l", body="\n".join(f"line {i}" for i in range(9)))["body"]
    assert many.split("\n") == [f"line {i}" for i in range(5)] + ["line 5…"]
    long = mod.clean_post("a", line="l", body="w " * 400 + "\nnext")["body"]
    assert len(long) == mod.BODY_MAX and long.endswith("…") and "\n" not in long
    assert mod.clean_post("a", line="l", body=None)["body"] == ""


@pytest.mark.parametrize("kwargs, msg", [
    ({"id": "", "line": "l"}, "id"),
    ({"id": "x" * 65, "line": "l"}, "id"),
    ({"id": "a", "line": "  "}, "line"),
    ({"id": "a", "line": "l", "kind": "poll"}, "kind"),
    ({"id": "a", "line": "l", "urgency": "high"}, "urgency"),
    ({"id": "a", "line": "l", "channel": "sms"}, "channel"),
    ({"id": "a", "line": "l", "actions": ["yes", "maybe"]}, "actions"),
    ({"id": "a", "line": "l", "expires_at": "soon"}, "expires_at"),
    ({"id": "a", "line": "l", "expires_at": -1}, "expires_at"),
])
def test_clean_post_rejects(mod, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        mod.clean_post(**kwargs)


def test_off_device_every_cap_says_so(mod):
    p = mod.AndroidAttentionPlugin()
    assert p.available() is False
    assert p._post(id="a", line="l") == {"ok": False, "error": "not an Android host"}
    assert p._poll()["ok"] is False and p._feed()["ok"] is False and p._rules_get()["ok"] is False


def test_post_and_update_reach_the_store(mod, monkeypatch):
    p, store = plugin(mod, monkeypatch)
    assert p.available() is True
    r = p._post(id="hold-7", kind="hold", line="Deploy 857?", explain="It ships the voice fix.",
                urgency="urgent", source="architect", expires_at=1900000000)
    assert r == {"ok": True, "id": "hold-7", "new": True}
    sent = store.calls[-1][1]
    assert sent["urgency"] == "urgent" and sent["kind"] == "hold" and sent["expires_at"] == 1900000000.0
    assert sent["body"] == ""
    p._post(id="hold-8", line="Read hub approvals database", body="For: Plain cards\nRisk: Low")
    assert store.calls[-1][1]["body"] == "For: Plain cards\nRisk: Low"
    assert p._post(id="x", line="l", channel="nope")["ok"] is False
    assert p._update(id="hold-7", state="DONE")["ok"] is True
    assert store.calls[-1] == ("update", "hold-7", "done", "")
    assert "state must be" in p._update(id="hold-7", state="approved")["error"]


def test_poll_and_feed_args(mod, monkeypatch):
    p, store = plugin(mod, monkeypatch)
    r = p._poll(since="41")
    assert r["last"] == 42 and store.calls[-1] == ("poll", 41)
    assert p._poll(since=-5)["last"] == 1
    assert p._poll(since="x")["ok"] is False
    assert p._feed(limit=10_000)["ignored_7d"] == 3 and store.calls[-1] == ("feed", 300)
    assert p._feed(limit=0) and store.calls[-1] == ("feed", 1)


def test_store_errors_become_replies(mod, monkeypatch):
    class Broken(FakeStore):
        def poll(self, ctx, since):
            raise RuntimeError("boom")
    p, _ = plugin(mod, monkeypatch, Broken())
    assert p._poll() == {"ok": False, "error": "RuntimeError: boom"}


class FakeRemind:
    """Mimics SpokenReminder.start: returns at once, the work happens on the phone."""

    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    def start(self, ctx, id, text, sample_ms, speak_unchecked):
        self.calls.append((id, text, sample_ms, speak_unchecked))
        return json.dumps(self.reply or {"ok": True, "id": id, "accepted": True})


def test_clean_remind_defaults_and_collapses(mod):
    r = mod.clean_remind("rem-1", "  Need your input:\n deploy   hub update?  ")
    assert r == {"id": "rem-1", "text": "Need your input: deploy hub update?", "sample_ms": 3000,
                 "speak_unchecked": False}
    assert mod.clean_remind(" a ", "x" * 300)["text"] == "x" * 300
    assert mod.clean_remind("a", "t", sample_ms=10)["sample_ms"] == 1000
    assert mod.clean_remind("a", "t", sample_ms="99999")["sample_ms"] == 8000
    assert mod.clean_remind("a", "t", sample_ms=2500.7)["sample_ms"] == 2500
    assert mod.clean_remind("a", "t", sample_ms=None)["sample_ms"] == 3000
    assert mod.clean_remind("x" * 64, "t")["id"] == "x" * 64


@pytest.mark.parametrize("value, want", [
    (False, False), (True, True), ("false", False), ("False", False), ("0", False), ("no", False),
    ("", False), (None, False), (0, False), ("true", True), ("YES", True), ("1", True), (1, True),
])
def test_clean_remind_speak_unchecked_is_boolean_ish(mod, value, want):
    assert mod.clean_remind("a", "t", speak_unchecked=value)["speak_unchecked"] is want


@pytest.mark.parametrize("kwargs, msg", [
    ({"id": "", "text": "t"}, "id"),
    ({"id": "   ", "text": "t"}, "id"),
    ({"id": None, "text": "t"}, "id"),
    ({"id": "x" * 65, "text": "t"}, "id"),
    ({"id": "a", "text": ""}, "text"),
    ({"id": "a", "text": " \n\t "}, "text"),
    ({"id": "a", "text": None}, "text"),
    ({"id": "a", "text": "x" * 301}, "300"),
    ({"id": "a", "text": "t", "sample_ms": "soon"}, "sample_ms"),
    ({"id": "a", "text": "t", "sample_ms": float("nan")}, "sample_ms"),
    ({"id": "a", "text": "t", "sample_ms": True}, "sample_ms"),
    ({"id": "a", "text": "t", "speak_unchecked": "maybe"}, "speak_unchecked"),
    ({"id": "a", "text": "t", "speak_unchecked": [1]}, "speak_unchecked"),
])
def test_clean_remind_rejects(mod, kwargs, msg):
    with pytest.raises(ValueError, match=msg):
        mod.clean_remind(**kwargs)


def test_remind_reaches_the_phone_and_returns_at_once(mod, monkeypatch):
    p, _ = plugin(mod, monkeypatch)
    fake = FakeRemind()
    monkeypatch.setattr(mod, "_Remind", fake)
    r = p._remind(id="cardq-1", text="Hey boss,  you have 6 decisions pending.", sample_ms="4000",
                  speak_unchecked="false")
    assert r == {"ok": True, "id": "cardq-1", "accepted": True}
    assert fake.calls == [("cardq-1", "Hey boss, you have 6 decisions pending.", 4000, False)]


def test_remind_bad_args_do_nothing(mod, monkeypatch):
    p, _ = plugin(mod, monkeypatch)
    fake = FakeRemind()
    monkeypatch.setattr(mod, "_Remind", fake)
    assert p._remind(id="a", text="")["ok"] is False
    assert p._remind(id="a", text="t", speak_unchecked="perhaps")["ok"] is False
    assert fake.calls == []


@pytest.mark.parametrize("reply", [
    {"ok": True, "id": "a", "accepted": False, "busy": "another reminder is running"},
    {"ok": True, "id": "a", "accepted": False, "reason": "off"},
])
def test_remind_passes_the_phones_answer_through(mod, monkeypatch, reply):
    p, _ = plugin(mod, monkeypatch)
    monkeypatch.setattr(mod, "_Remind", FakeRemind(reply))
    assert p._remind(id="a", text="t") == reply


def test_remind_off_device_and_bridge_errors(mod, monkeypatch):
    assert mod.AndroidAttentionPlugin()._remind(id="a", text="t") == {"ok": False, "error": "not an Android host"}
    p, _ = plugin(mod, monkeypatch)

    class Broken:
        def start(self, *a):
            raise RuntimeError("boom")
    monkeypatch.setattr(mod, "_Remind", Broken())
    assert p._remind(id="a", text="t") == {"ok": False, "error": "RuntimeError: boom"}


def test_remind_is_admin_and_physical_like_post(mod):
    meta = mod.AndroidAttentionPlugin._remind._rook_cap_meta
    post = mod.AndroidAttentionPlugin._post._rook_cap_meta
    assert meta.risk == post.risk == "admin"
    assert "physical" in meta.tags
    assert "reminded" in mod.AndroidAttentionPlugin._poll.__doc__


def test_clean_rules(mod):
    assert mod.clean_rules(None) == "" and mod.clean_rules([]) == ""
    rule = {"priority": "family", "category": "trusted", "match": {"senders": ["Coach Dana"]}, "note": "trust map"}
    assert json.loads(mod.clean_rules([rule])) == [rule]
    doc = {"rules": [rule], "family_names": ["Mom"], "extend_default": True}
    assert json.loads(mod.clean_rules(json.dumps(doc))) == doc
    for bad, msg in [("{nope", "JSON"), ({"family_names": []}, "rules"), ([1], "object"),
                     ([{"priority": "loud", "match": {"words": ["x"]}}], "priority"),
                     ([{"priority": "urgent", "match": {}}], "match"),
                     ([{"priority": "urgent", "match": {"words": "fire"}}], "list of strings"),
                     ([{"priority": "urgent", "match": {"emoji": ["x"]}}], "unknown"),
                     ([{"priority": "urgent", "match": {"words": ["x"]}, "except": []}], "except"),
                     ([{"priority": "ignore", "match": {"words": ["x" * 70000]}}], "larger")]:
        with pytest.raises(ValueError, match=msg):
            mod.clean_rules(bad)


def test_rules_set_passes_json_and_reset(mod, monkeypatch):
    p, store = plugin(mod, monkeypatch)
    assert p._rules_set([{"priority": "ignore", "match": {"packages": ["com.game"]}}])["source"] == "override"
    assert json.loads(store.calls[-1][1])[0]["priority"] == "ignore"
    assert p._rules_set()["source"] == "default" and store.calls[-1] == ("rulesSet", "")
    assert p._rules_set("[1]")["ok"] is False


def test_default_rules_asset_is_valid(mod):
    """The shipped asset passes the same validation the hub's rules_set does."""
    doc = json.loads((Path(__file__).parents[1] / "android/app/src/main/assets/attention_rules.json").read_text())
    assert mod.clean_rules(doc)
    priorities = [r["priority"] for r in doc["rules"]]
    assert {"urgent", "family", "ignore"} <= set(priorities)


def test_notify_post_channels(monkeypatch):
    m = _load(monkeypatch, "notify_android")
    assert m.channel_id("") is None and m.channel_id(None) is None
    assert m.channel_id("Urgent") == "rook_urgent" and m.channel_id("jobs") == "rook_jobs"
    with pytest.raises(ValueError):
        m.channel_id("loud")
    assert "channel must be" in m.AndroidNotifyPlugin()._post(title="t", channel="loud")["error"]


def test_device_update_channel(monkeypatch):
    class Updater:
        channel = "stable"

        @classmethod
        def setChannel(cls, ctx, value):
            cls.channel = value
            return True

        @classmethod
        def request(cls, ctx, automatic):
            return json.dumps({"ok": True, "queued": True, "channel": cls.channel})

    m = _load(monkeypatch, "device_android", ctx=object(), jclass=lambda name: Updater)
    p = m.AndroidDevicePlugin()
    assert p._update()["channel"] == "stable"
    assert p._update(channel="Beta")["channel"] == "beta" and Updater.channel == "beta"
    assert p._update(channel="nightly") == {"ok": False, "error": "channel must be stable or beta"}
    assert Updater.channel == "beta"
