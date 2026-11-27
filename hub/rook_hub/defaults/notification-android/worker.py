"""notification-android: the notifications of the phone the Rook Android app runs on (brief AP4),
shipped as a plugin installed by default. The cap names, arguments and replies are 0.5's
(origin/castling rook/plugins/notification-android):

- notification.list, notification.get, notification.dismiss and notification.act read other apps'
  notifications, run their own actions (a reply typed into a notification's reply field among them)
  and dismiss them, through the app's one notification listener (NotificationListener.kt). That needs
  Rook's notification access, a switch on Android's own settings page: the first call that needs it
  opens that page through the app's bridge (or a notification that opens it, when the app is not in
  front), waits for the person, then answers. Refused, the error says where to turn it on.
- notification.post and notification.update post the phone's own notifications (Cards.kt), on
  channels of their own, apart from the worker's ongoing notification. With `id` a post is a card the
  person answers with its buttons; what they do comes back to the hub as the event
  notification-android.card (Cards.kt says how it travels). A No can carry the person's typed reason
  (`reason`). A card's expires_at, the caller's own request, takes it from the shade at that time.
- Every other app's notification is told to the hub as it is posted, as the event
  notification-android.received {key, package, title, text, ts} (NotificationListener.kt, brief AP7).

Nothing here bounds a call or filters an app (Bake, 17 November): 0.5's lengths (text 4000, title
200, body 2000 and its six lines, a card's line cut at 140, explain 2000, source 80, options 6 of 200,
actions 5, a reply's text 2000, message 300), notification.list's default 40 and most 200, its ring
of the last 200 and big_text cut at 1000, the 10-15 s timeouts, and notification.act's owner switch
in the app are all left out. Nothing of ours expires a card: only the caller's own expires_at does.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio
import json

from rook_worker import Cap

try:
    from java import jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

APP = "systems.bake.rook.core.worker."
CHANNELS = ["", "urgent", "family", "approvals", "digest", "jobs"]
KINDS = ["card", "hold", "job", "ask", "alert"]
ACTIONS = ["yes", "no", "choose", "explain", "ack"]
STATES = ["done", "failed", "gone"]
NOTIFY = "android.permission.POST_NOTIFICATIONS"


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _text(args, name, default="", blank=True):
    value = args.get(name, default)
    if not isinstance(value, str) or (not blank and not value.strip()):
        raise ValueError(f"{name} must be {'a' if blank else 'a non-blank'} string")
    return value


def _choice(args, name, choices, default):
    value = args.get(name, default)
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(repr(c) if c == '' else c for c in choices)}")
    return value


def _strings(args, name):
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{name} must be a list of strings")
    return value


def _bridge():
    return jclass(APP + "Bridge")


def _answer(text):
    """A JSON reply from the app: its value, or its error as the call's."""
    got = json.loads(str(text))
    if isinstance(got, dict) and "error" in got:
        raise ValueError(got["error"])
    return got


def _listener(cap):
    """The app's notification listener, once Rook has notification access and Android has bound it."""
    Bridge = _bridge()
    refused = Bridge.ask(Bridge.NOTIFICATION_ACCESS, cap)
    if refused is not None:
        raise ValueError(str(refused))
    return jclass(APP + "NotificationListener").bound()


def _may_notify(cap):
    if int(jclass("android.os.Build$VERSION").SDK_INT) >= 33:      # a runtime permission from Android 13
        refused = _bridge().ask(NOTIFY, cap)
        if refused is not None:
            raise ValueError(str(refused))


def card(args):
    """notification.post's card, checked, as Cards.kt takes it (0.5's rules, none of its lengths)."""
    kind = _choice(args, "kind", KINDS, "card")
    given = _strings(args, "actions")
    actions = [a for a in ACTIONS if a in (given if given is not None else ["ack"] if kind == "alert"
                                           else ["yes", "no", "explain"])]
    if given is not None and not set(given) <= set(ACTIONS):
        raise ValueError(f"actions must be some of {', '.join(ACTIONS)}")
    options = [o.strip() for o in _strings(args, "options") or ()]
    if ("choose" in actions) != bool(options):
        raise ValueError("the choose action and options go together: give both, or neither")
    if not all(options) or len(set(options)) != len(options):
        raise ValueError("options must be different, non-blank answers")
    expires = args.get("expires_at", 0)
    if not isinstance(expires, (int, float)) or isinstance(expires, bool) or expires < 0:
        raise ValueError("expires_at must be a number of epoch seconds, or 0")
    return {"id": _text(args, "id", blank=False).strip(), "kind": kind, "text": _text(args, "text").strip(),
            "body": "\n".join(line.strip() for line in _text(args, "body").splitlines() if line.strip()),
            "explain": _text(args, "explain").strip(), "urgency": _choice(args, "urgency", ["normal", "urgent"], "normal"),
            "channel": _choice(args, "channel", CHANNELS, "") or "approvals", "actions": actions or ["ack"],
            "options": options, "source": _text(args, "source").strip(), "expires_at": expires}


def _post(args):
    _may_notify("notification.post")
    Cards = jclass(APP + "Cards")
    if args.get("id") is None:
        return _answer(Cards.post(_text(args, "title", "Rook"), args["text"], _choice(args, "channel", CHANNELS, "")))
    return _answer(Cards.card(json.dumps(card(args), ensure_ascii=False)))


async def post(args):
    _check(args, "notification.post", ("text", "title", "channel", "id", "kind", "body", "explain", "urgency",
                                       "actions", "options", "source", "expires_at"), ("text",))
    _text(args, "text", blank=False)
    _text(args, "title", "Rook")
    _choice(args, "channel", CHANNELS, "")
    if args.get("id") is not None:
        card(args)                  # refused here, before Android
    return await asyncio.to_thread(_post, args)


async def update(args):
    _check(args, "notification.update", ("id", "state", "message"), ("id", "state"))
    card_id, state = _text(args, "id", blank=False).strip(), _choice(args, "state", STATES, None)
    message = _text(args, "message")
    return await asyncio.to_thread(lambda: _answer(jclass(APP + "Cards").update(card_id, state, message)))


def _list(limit, packages):
    items = json.loads(str(_listener("notification.list").list()))
    if packages:
        items = [i for i in items if i.get("package") in packages]
    if limit is not None:
        items = items[:limit]
    return {"notifications": items, "connected": True}


async def list_notifications(args):
    _check(args, "notification.list", ("limit", "packages"))
    limit = args.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be a whole number, 1 or more")
    packages = _strings(args, "packages")
    return await asyncio.to_thread(_list, limit, set(packages or ()))


async def get(args):
    _check(args, "notification.get", ("key",), ("key",))
    key = _text(args, "key", blank=False)
    return await asyncio.to_thread(lambda: _answer(_listener("notification.get").get(key)))


async def dismiss(args):
    _check(args, "notification.dismiss", ("key",), ("key",))
    key = _text(args, "key", blank=False)
    return await asyncio.to_thread(lambda: _answer(_listener("notification.dismiss").dismiss(key)))


def pick_action(actions, action):
    """The index of `action` (an index, as a number or its digits, or a title, any case) among `actions`."""
    if isinstance(action, int) and not isinstance(action, bool):
        return action
    wanted = str(action).strip()
    if wanted.lstrip("-").isdigit():
        return int(wanted)
    match = [a for a in actions if str(a.get("title") or "").strip().lower() == wanted.lower()]
    if not match:
        raise ValueError(f"that notification has no action {action!r}; it has: "
                         f"{', '.join(repr(a.get('title')) for a in actions) or 'none'}")
    return match[0]["index"]


def _act(key, action, text):
    listener = _listener("notification.act")
    index = pick_action(_answer(listener.get(key)).get("actions") or [], action)
    return _answer(listener.act(_bridge().context(), key, index, text or None))


async def act(args):
    _check(args, "notification.act", ("key", "action", "text"), ("key",))
    key = _text(args, "key", blank=False)
    action = args.get("action", "0")
    if not isinstance(action, (str, int)) or isinstance(action, bool):
        raise ValueError("action must be an action's index or its title (notification.get lists them)")
    text = _text(args, "text")
    return await asyncio.to_thread(_act, key, action, text)


_KEY = {"type": "string", "description": "The notification's key, from notification.list."}

CAPS = {} if jclass is None else {
    "notification.post": Cap(
        "Show a notification (title, text, channel); with id a card the person answers with its buttons (actions: yes, "
        "no, choose with options, explain, ack), its answer coming back as the event notification-android.card.",
        {"type": "object", "additionalProperties": False, "required": ["text"], "properties": {
            "text": {"type": "string", "description": "The notification's text; a card's one line."},
            "title": {"type": "string", "default": "Rook"},
            "channel": {"type": "string", "enum": CHANNELS, "default": "",
                        "description": "How it sounds. Blank: Rook messages; a card's blank is approvals. urgent rings "
                                       "like an alarm; digest is quiet, jobs silent."},
            "id": {"type": "string", "description": "Makes it a card: its handle, for notification.update and its answer. "
                                                    "Posting the same id again updates it without a sound."},
            "kind": {"type": "string", "enum": KINDS, "default": "card"},
            "body": {"type": "string", "default": "", "description": "Short lines under a card's line (For:, Who:, I want to:, Risk:)."},
            "explain": {"type": "string", "default": "", "description": "What a card's More details shows."},
            "urgency": {"type": "string", "enum": ["normal", "urgent"], "default": "normal"},
            "actions": {"type": "array", "items": {"type": "string", "enum": ACTIONS},
                        "description": "A card's buttons; default yes, no, explain (ack for kind alert)."},
            "options": {"type": "array", "items": {"type": "string"}, "description": "A question's choices, with the choose action."},
            "source": {"type": "string", "default": "", "description": "Who asks, shown as the card's title."},
            "expires_at": {"type": "number", "default": 0, "description": "Epoch seconds: at that time the card goes from the shade by itself. "
                                                                  "0 or left out: it never does."}}},
        post),
    "notification.update": Cap(
        "Show what came of a card: done (a confirmation, its buttons gone), failed (shows message) or gone (taken away, no sound).",
        {"type": "object", "additionalProperties": False, "required": ["id", "state"], "properties": {
            "id": {"type": "string"}, "state": {"type": "string", "enum": STATES},
            "message": {"type": "string", "default": ""}}},
        update),
    "notification.list": Cap(
        "Every notification in the shade, every app's, newest first: {notifications: [{key, package, title, text, ts, "
        "posted_ms, clearable, sub_text?, big_text?}]}; packages keeps only those apps, limit the newest that many.",
        {"type": "object", "additionalProperties": False, "properties": {
            "limit": {"type": "integer", "minimum": 1},
            "packages": {"type": "array", "items": {"type": "string"}}}},
        list_notifications),
    "notification.get": Cap(
        "One notification by key with the actions it offers: {notification, actions: [{index, title, takes_text}]}.",
        {"type": "object", "additionalProperties": False, "required": ["key"], "properties": {"key": _KEY}},
        get),
    "notification.dismiss": Cap(
        "Dismiss a notification by key (from notification.list).",
        {"type": "object", "additionalProperties": False, "required": ["key"], "properties": {"key": _KEY}},
        dismiss),
    "notification.act": Cap(
        "Run one of a notification's own actions (action: its index or title, from notification.get); text fills a "
        "reply action, which sends a message as the phone's owner.",
        {"type": "object", "additionalProperties": False, "required": ["key"], "properties": {
            "key": _KEY,
            "action": {"type": ["string", "integer"], "default": "0", "description": "Its index or its title."},
            "text": {"type": "string", "default": "", "description": "What a reply action sends."}}},
        act),
}
