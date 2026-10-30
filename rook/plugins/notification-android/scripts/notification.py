"""notification.*: posting (NotificationManager; cards through the app's
AttentionStore, which keeps them and what the person did), and other apps'
notifications through RookNotificationListener (notification access)."""

import json
import time

from rook.kit import android
from rook.kit.ctx import KitError

CHANNELS = {"urgent": "rook_urgent", "family": "rook_family", "approvals": "rook_approvals",
            "digest": "rook_digest", "jobs": "rook_jobs"}   # AttentionChannels.kt
LINE_MAX, BODY_MAX, BODY_LINES = 140, 600, 6
ORDER = ("yes", "no", "choose", "explain", "ack")
CARD_EVENTS = ("decision", "acked", "dismissed", "seen", "opened")
OWN = "systems.bake.rook"
NO_ACCESS = "notification access not allowed: allow it in the Rook app"


def _listener():
    lst = android.app("RookNotificationListener")
    if not lst.isEnabled(android.context()):
        raise KitError(NO_ACCESS)
    return lst


def _clip(text, limit):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _body(text):
    """Up to BODY_LINES short lines, blank ones dropped, BODY_MAX characters, an ellipsis where cut."""
    lines = [" ".join(raw.split()) for raw in text.splitlines()]
    lines = [ln for ln in lines if ln]
    out = "\n".join(lines[:BODY_LINES])
    if len(lines) > BODY_LINES or len(out) > BODY_MAX:
        out = out[:BODY_MAX - 1].rstrip() + "…"
    return out


def card(text, id, kind, body, explain, urgency, channel, actions, options, source, expires_at):
    """The AttentionStore card for notification.post's card args. Raises KitError."""
    acts = [a for a in ORDER if a in (actions if actions is not None
                                      else ["ack"] if kind == "alert" else ["yes", "no", "explain"])]
    opts = [o.strip() for o in options or ()]
    if ("choose" in acts) != bool(opts):
        raise KitError("the choose action and options go together")
    if not all(opts) or len(set(opts)) != len(opts):
        raise KitError("options must be different, non-blank answers")
    return {"id": id, "kind": kind, "line": _clip(text, LINE_MAX), "body": _body(body), "explain": explain.strip(),
            "urgency": urgency, "channel": channel or "approvals", "actions": acts or ["ack"],
            "source": _clip(source, 80), "expires_at": expires_at, "options": opts}


def post(ctx, text, title, channel, kind, body, explain, urgency, source, expires_at,
         id=None, actions=None, options=None):
    c = android.context()
    if id is not None:
        cid = card(text, id, kind, body, explain, urgency, channel, actions, options, source, expires_at)
        return android.result(android.app("AttentionStore").post(c, json.dumps(cid, ensure_ascii=False)))
    J = android.jclass
    nm = android.service("NOTIFICATION_SERVICE", "android.app.NotificationManager")
    Builder = J("android.app.Notification$Builder")
    if channel:
        android.app("AttentionChannels").ensure(c)
        chan = CHANNELS[channel]
    else:
        chan = "rook_msgs"
        nm.createNotificationChannel(J("android.app.NotificationChannel")(
            chan, "Rook messages", J("android.app.NotificationManager").IMPORTANCE_DEFAULT))
    b = Builder(c, chan)
    b.setContentTitle(title).setContentText(text).setSmallIcon(J("android.R$drawable").stat_notify_chat)
    b.setAutoCancel(True).setContentIntent(android.app("NotificationNavigation").mainActivity(c))
    nid = int(time.time() * 1000) & 0x7fffffff
    nm.notify(nid, b.build())
    return {"id": nid, "channel": chan}


def update(ctx, id, state, message):
    return android.result(android.app("AttentionStore").update(android.context(), id, state, _clip(message, 300)))


def _snapshot(n):
    items = json.loads(str(_listener().snapshotJson(n)))
    for i in items:
        i.setdefault("posted_ms", int(i.get("ts", 0) * 1000))
    return items


def list_notifications(ctx, limit, packages):
    wanted = set(packages)
    items = _snapshot(200 if wanted else limit)
    if wanted:
        items = [i for i in items if i.get("package") in wanted][:limit]
    return {"notifications": items, "connected": bool(android.app("RookNotificationListener").isConnected())}


def get(ctx, key):
    got = android.result(_listener().actionsJson(key))
    found = next((i for i in _snapshot(200) if i.get("key") == key), None)
    return {"notification": found, "actions": got.get("actions", [])}


def dismiss(ctx, key):
    if not _listener().dismiss(key):
        raise KitError("not dismissed (gone already, or the listener isn't connected)")
    return {}


def act(ctx, key, action, text):
    android.assistant_may_act()
    lst = _listener()
    index = action.strip()
    if not index.lstrip("-").isdigit():
        acts = android.result(lst.actionsJson(key)).get("actions") or []
        match = [a for a in acts if str(a.get("title") or "").strip().lower() == index.lower()]
        if not match:
            raise KitError(f"no action {action!r}; it has: {', '.join(str(a.get('title')) for a in acts) or 'none'}")
        index = match[0]["index"]
    return android.result(lst.act(android.context(), key, int(index), text))


def watch(ctx):
    """Emit notification-android.received for notifications posted since the
    last look (kept across restarts; the very first look only notes where it is)."""
    lst = android.app("RookNotificationListener")
    if not lst.isEnabled(android.context()):
        return
    items = _snapshot(200)
    seen = android.cursor(ctx, "posted")
    newest = max([seen or 0, *(i["posted_ms"] for i in items)])
    if seen is not None:
        for i in sorted(items, key=lambda i: i["posted_ms"]):
            if i["posted_ms"] > seen and i.get("package") != OWN:
                ctx.emit("notification-android.received", {
                    "key": i.get("key") or "", "package": i.get("package") or "", "title": i.get("title") or "",
                    "text": i.get("big_text") or i.get("text") or "", "ts": i.get("ts") or i["posted_ms"] / 1000})
    android.cursor(ctx, "posted", newest)


def watch_cards(ctx):
    """Emit notification-android.card for what the person did with cards
    since the last look. AttentionStore keeps the events while the worker is
    down; the very first look only notes where its log is."""
    store, c = android.app("AttentionStore"), android.context()
    since = android.cursor(ctx, "cards")
    for _ in range(20):   # "more": a long backlog comes in pages
        got = json.loads(str(store.poll(c, since or 0)))
        if not got.get("ok", True):
            return
        if since is not None:
            for e in got.get("events") or ():
                if e.get("type") in CARD_EVENTS:
                    ctx.emit("notification-android.card", {
                        k: e[k] for k in ("id", "type", "decision", "choice", "reason", "ts") if e.get(k) is not None})
        since = android.cursor(ctx, "cards", int(got.get("last") or 0))
        if not got.get("more"):
            return
