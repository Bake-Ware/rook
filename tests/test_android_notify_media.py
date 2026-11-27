"""The phone's notification and media caps as built-in plugins (brief AP4): notification-android and
media-android. On the Rook Android app they run under Chaquopy, whose `java` module reaches Android
(NotificationListener.kt, Cards.kt); that is proved on an emulator (the pull request). Here: off the
app they announce nothing and break nothing, on a real worker over the real carrier; with a `java`
module present they announce 0.5's cap names, each with a one-line description and a schema; what
they check before reaching Android refuses as it should; 0.5's rules for picking a media session and a
notification's action hold; and none of 0.5's bounds is kept."""

import asyncio

import pytest

from test_android_plugins import fake_java, part
from test_default_plugins import Rig

AP4 = {
    "media-android": ["media.next", "media.pause", "media.play", "media.previous", "media.seek", "media.status",
                      "media.stop"],
    "notification-android": ["notification.act", "notification.dismiss", "notification.get", "notification.list",
                             "notification.post", "notification.update"],
}


def test_off_the_app_they_announce_nothing():
    for name in AP4:
        assert part(name)["CAPS"] == {}, name


def test_on_the_app_they_announce_0_5s_caps_each_with_a_one_line_description_and_a_schema():
    for name, caps in AP4.items():
        declared = part(name, fake_java())["CAPS"]
        assert sorted(declared) == caps, name
        for cap in declared.values():
            assert cap.description and "\n" not in cap.description
            assert cap.schema["type"] == "object" and cap.schema.get("additionalProperties") is False


def test_a_linux_worker_loads_them_announces_none_of_their_caps_and_serves_the_rest(tmp_path):
    async def go():
        async with Rig(tmp_path) as rig:
            loaded = {p["name"]: p for p in rig.hub.get_worker(rig.guid)["plugins"]}
            assert all(name in loaded and "error" not in loaded[name] for name in AP4), loaded
            assert not set(rig.announced()) & {cap for caps in AP4.values() for cap in caps}
            assert (await rig.ok("worker.ping"))["pong"] is True
    asyncio.run(go())


def refusal(name, cap, args):
    """What a cap says to `args` it refuses before reaching Android (the fake `java` fails any reach)."""
    handler = part(name, fake_java())["CAPS"][cap].handler
    with pytest.raises(ValueError) as refused:
        asyncio.run(handler(args))
    return str(refused.value)


def test_what_they_refuse_before_reaching_android():
    n = "notification-android"
    assert "notification.post needs text" in refusal(n, "notification.post", {})
    assert "takes no argument colour" in refusal(n, "notification.post", {"text": "hi", "colour": "red"})
    assert "channel must be one of" in refusal(n, "notification.post", {"text": "hi", "channel": "loud"})
    assert "kind must be one of" in refusal(n, "notification.post", {"text": "q", "id": "c1", "kind": "poll"})
    assert "actions must be some of" in refusal(n, "notification.post", {"text": "q", "id": "c1", "actions": ["maybe"]})
    assert "go together" in refusal(n, "notification.post", {"text": "q", "id": "c1", "actions": ["choose"]})
    assert "go together" in refusal(n, "notification.post", {"text": "q", "id": "c1", "options": ["a", "b"]})
    assert "different, non-blank" in refusal(n, "notification.post",
                                             {"text": "q", "id": "c1", "actions": ["choose"], "options": ["a", "a"]})
    assert "non-blank string" in refusal(n, "notification.post", {"text": "q", "id": "  "})
    assert "state must be one of" in refusal(n, "notification.update", {"id": "c1", "state": "maybe"})
    assert "notification.update needs state" in refusal(n, "notification.update", {"id": "c1"})
    assert "notification.get needs key" in refusal(n, "notification.get", {})
    assert "notification.dismiss needs key" in refusal(n, "notification.dismiss", {})
    assert "limit must be a whole number" in refusal(n, "notification.list", {"limit": 0})
    assert "packages must be a list" in refusal(n, "notification.list", {"packages": "com.example"})
    assert "action must be an action's index or its title" in refusal(n, "notification.act", {"key": "k", "action": 1.5})
    m = "media-android"
    assert "media.status takes no argument package" in refusal(m, "media.status", {"package": "x"})
    assert "media.play takes no argument by" in refusal(m, "media.play", {"by": 3})
    assert "by must be a whole number" in refusal(m, "media.seek", {"by": "ten"})
    assert "package must be" in refusal(m, "media.pause", {"package": 3})


def test_a_card_is_0_5s_card():
    card = part("notification-android", fake_java())["card"]
    assert card({"id": " c1 ", "text": " Restart? "}) == {
        "id": "c1", "kind": "card", "text": "Restart?", "body": "", "explain": "", "urgency": "normal",
        "channel": "approvals", "actions": ["yes", "no", "explain"], "options": [], "source": ""}
    assert card({"id": "a", "text": "Door open", "kind": "alert"})["actions"] == ["ack"]
    assert card({"id": "a", "text": "t", "actions": []})["actions"] == ["ack"]
    assert card({"id": "a", "text": "t", "actions": ["ack", "no", "yes"]})["actions"] == ["yes", "no", "ack"]   # 0.5's order
    chosen = card({"id": "a", "text": "Which?", "actions": ["choose"], "options": [" Den ", "Porch"]})
    assert chosen["options"] == ["Den", "Porch"]
    assert card({"id": "a", "text": "t", "body": "For: x\n\n  Who: y  \n"})["body"] == "For: x\nWho: y"
    assert card({"id": "a", "text": "t", "channel": "digest"})["channel"] == "digest"


def test_picking_a_notifications_action_and_a_media_session():
    pick_action = part("notification-android", fake_java())["pick_action"]
    actions = [{"index": 0, "title": "Mark as read", "takes_text": False}, {"index": 1, "title": "Reply", "takes_text": True}]
    assert (pick_action(actions, "reply"), pick_action(actions, "1"), pick_action(actions, 0)) == (1, 1, 0)
    with pytest.raises(ValueError, match="no action 'Archive'; it has: 'Mark as read', 'Reply'"):
        pick_action(actions, "Archive")
    pick = part("media-android", fake_java())["pick"]
    sessions = [("a", {"package": "com.google.android.youtube", "app": "YouTube", "state": "paused"}),
                ("b", {"package": "com.spotify.music", "app": "Spotify", "state": "playing"}),
                ("c", {"package": "com.android.chrome", "app": "Chrome", "state": "none"})]
    assert pick(sessions, "")[0] == "b"                          # the one playing
    assert pick(sessions, "chrome")[0] == "c"                    # named by its app
    assert pick(sessions, "com.google.android.youtube")[0] == "a"
    assert pick(sessions, "Pandora") is None
    assert pick(sessions[:1] + sessions[2:], "")[0] == "a"       # none playing: the most recent
    assert pick([], "") is None


def test_where_a_session_is_now():
    position_ms = part("media-android", fake_java())["position_ms"]

    class State:
        def __init__(self, state, at, updated, speed=1.0):
            self.values = state, at, updated, speed

        def getState(self): return self.values[0]
        def getPosition(self): return self.values[1]
        def getLastPositionUpdateTime(self): return self.values[2]
        def getPlaybackSpeed(self): return self.values[3]

    assert position_ms(State(3, 10_000, 5_000), 7_000) == 12_000          # playing: moved on since it told
    assert position_ms(State(3, 10_000, 5_000, 2.0), 7_000) == 14_000
    assert position_ms(State(2, 10_000, 5_000), 7_000) == 10_000           # paused: where it told
    assert position_ms(State(3, -1, 5_000), 7_000) is None                 # it does not say


def test_no_bound_is_kept_from_0_5():
    """0.5 cut notification.list at 40 (most 200), every text at a length, a card's options at 6 and its
    actions at 5, and gave every cap a 10-15 s timeout (Bake, 17 November: none of that)."""
    caps = {cap: c for name in AP4 for cap, c in part(name, fake_java())["CAPS"].items()}

    def bounds(schema):
        found = set()
        if isinstance(schema, dict):
            found |= {k for k in schema if k in ("maximum", "maxLength", "maxItems", "timeout")}
            for value in schema.values():
                found |= bounds(value)
        elif isinstance(schema, list):
            for value in schema:
                found |= bounds(value)
        return found

    assert {cap: bounds(c.schema) for cap, c in caps.items() if bounds(c.schema)} == {}
    assert "default" not in caps["notification.list"].schema["properties"]["limit"]
    card = part("notification-android", fake_java())["card"]
    long = "x" * 50_000
    made = card({"id": "i" * 500, "text": long, "body": "\n".join(f"line {n}" for n in range(40)), "explain": long,
                 "source": long, "actions": ["choose"], "options": [f"option {n} {long}" for n in range(30)]})
    assert len(made["id"]) == 500 and made["text"] == long and made["explain"] == long and made["source"] == long
    assert len(made["body"].splitlines()) == 40 and len(made["options"]) == 30
