"""attention.* — decision cards, read/ack signals and the quiet notification feed.

The hub puts decision cards on the phone (attention.post) and reads back what
the person did (attention.poll). Backed by systems.bake.rook.AttentionStore
(Kotlin), which persists cards and events in the app's files, so a phone that
is offline keeps every event until the next poll.

The phone also sorts other apps' notifications itself (rules in
attention.rules_get/rules_set). Their content stays on the phone; only a short
summary of urgent or family items leaves it, as a "notice" event. See
docs/design/attention-phone.md.
"""

from __future__ import annotations

import json

from rook.worker.plugin import ANDROID_APP, Plugin, capability, place
from rook_android.androidctx import app_context

try:
    from java import jclass as _jclass
    _Store = _jclass("systems.bake.rook.AttentionStore")
except Exception:  # pragma: no cover - only importable on a Chaquopy host
    _Store = None

KINDS = ("hold", "job", "card", "ask", "alert")
CHANNELS = ("urgent", "family", "approvals", "digest", "jobs")
ACTIONS = ("yes", "no", "explain", "ack")
STATES = ("done", "failed", "gone")
LINE_MAX, EXPLAIN_MAX, ID_MAX = 140, 600, 64
BODY_MAX, BODY_LINES = 600, 6
RULES_MAX_BYTES = 64 * 1024


def _clip(text, limit: int) -> str:
    text = " ".join(str(text or "").split()) if limit <= LINE_MAX else str(text or "").strip()
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _body(text) -> str:
    """Up to BODY_LINES short lines, newlines kept: each line's whitespace collapsed, blank lines
    dropped, BODY_MAX characters in all, an ellipsis where it was cut."""
    lines = [" ".join(raw.split()) for raw in str(text or "").splitlines()]
    lines = [ln for ln in lines if ln]
    cut = len(lines) > BODY_LINES
    out = "\n".join(lines[:BODY_LINES])
    if len(out) > BODY_MAX:
        out, cut = out[:BODY_MAX - 1], True
    if cut:
        out = out.rstrip() + "…"
    return out


def _actions(value) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).replace(",", " ").split()
    out = [str(a).strip().lower() for a in items if str(a).strip()]
    bad = [a for a in out if a not in ACTIONS]
    if bad:
        raise ValueError(f"actions must be a subset of {list(ACTIONS)}, got {bad}")
    return [a for a in ACTIONS if a in out]


def clean_post(id, kind="card", line="", explain="", urgency="normal", channel="approvals",
               actions=None, source="", expires_at=0, body="") -> dict:
    """Validate and normalize attention.post args. Raises ValueError."""
    cid = str(id or "").strip()
    if not cid or len(cid) > ID_MAX:
        raise ValueError(f"id must be 1-{ID_MAX} characters")
    kind = str(kind or "card").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {list(KINDS)}")
    text = _clip(line, LINE_MAX)
    if not text:
        raise ValueError("line is required")
    urgency = str(urgency or "normal").strip().lower()
    if urgency not in ("urgent", "normal"):
        raise ValueError("urgency must be urgent or normal")
    channel = str(channel or "approvals").strip().lower()
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {list(CHANNELS)}")
    acts = _actions(actions) if actions is not None else (["ack"] if kind == "alert" else ["yes", "no", "explain"])
    try:
        exp = float(expires_at or 0)
    except (TypeError, ValueError):
        raise ValueError("expires_at must be epoch seconds") from None
    if exp != exp or exp < 0:
        raise ValueError("expires_at must be epoch seconds")
    return {"id": cid, "kind": kind, "line": text, "body": _body(body), "explain": _clip(explain, EXPLAIN_MAX),
            "urgency": urgency, "channel": channel, "actions": acts or ["ack"], "source": _clip(source, 80),
            "expires_at": exp}


def clean_rules(rules) -> str:
    """attention.rules_set payload as a JSON string ("" resets to the defaults). Raises ValueError."""
    if rules is None or rules == "" or rules == []:
        return ""
    if isinstance(rules, str):
        try:
            rules = json.loads(rules)
        except ValueError:
            raise ValueError("rules must be JSON (a list of rules or a {rules: [...]} document)") from None
    if isinstance(rules, dict):
        items = rules.get("rules")
        if not isinstance(items, list):
            raise ValueError("a rules document needs a 'rules' list")
    elif isinstance(rules, list):
        items = rules
    else:
        raise ValueError("rules must be a list of rules or a {rules: [...]} document")
    for i, r in enumerate(items):
        if not isinstance(r, dict):
            raise ValueError(f"rule {i} is not an object")
        if str(r.get("priority", "")).lower() not in ("urgent", "family", "normal", "ignore"):
            raise ValueError(f"rule {i}: priority must be urgent, family, normal or ignore")
        for part in ("match", "scope", "except"):
            m = r.get(part)
            if m is None and part != "match":
                continue
            if not isinstance(m, dict) or (part == "match" and not any(m.get(k) for k in m)):
                raise ValueError(f"rule {i}: {part} must be an object" + (" with at least one list" if part == "match" else ""))
            for k, v in m.items():
                if k not in ("packages", "app_labels", "words", "senders"):
                    raise ValueError(f"rule {i}: unknown {part} key {k!r}")
                if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                    raise ValueError(f"rule {i}: {part}.{k} must be a list of strings")
    text = json.dumps(rules, ensure_ascii=False)
    if len(text.encode()) > RULES_MAX_BYTES:
        raise ValueError(f"rules are larger than {RULES_MAX_BYTES // 1024} KB")
    return text


def _ready() -> bool:
    return _Store is not None and app_context() is not None


def _call(method: str, *args) -> dict:
    if not _ready():
        return {"ok": False, "error": "not an Android host"}
    try:
        return json.loads(str(getattr(_Store, method)(app_context(), *args)))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class AndroidAttentionPlugin(Plugin):
    NAMESPACE = "attention"
    PLACEMENT = place(ANDROID_APP)

    def available(self) -> bool:
        return _ready()

    @capability("post", risk="admin", tags=("physical",))
    def _post(self, id: str, kind: str = "card", line: str = "", explain: str = "", urgency: str = "normal",
              channel: str = "approvals", actions: list | str | None = None, source: str = "",
              expires_at: float = 0, body: str = "") -> dict:
        """Put a decision card on the phone; re-posting the same id updates it without ringing again.

        id: up to 64 chars, your handle for attention.update and the events.
        kind: hold|job|card|ask|alert. line: the plain title (140 chars).
        body: optional short lines always shown under the title, e.g. "For: ...",
        "Who: ...", "I want to: ...", "Risk: ..." one per line (newlines kept,
        blank lines dropped, at most 6 lines / 600 chars).
        explain: the technical detail shown on Explain (600 chars). urgency: urgent rings
        like an alarm and takes over the screen (also channel=urgent); normal is a
        heads-up that opens the card when the phone is next unlocked.
        channel: urgent|family|approvals|digest|jobs. actions: subset of
        yes/no/explain/ack (default yes,no,explain; ack for alerts).
        source: who asks, shown small. expires_at: epoch seconds, 0 = never.
        The answer comes back through attention.poll as a "decision" event.
        """
        try:
            card = clean_post(id, kind, line, explain, urgency, channel, actions, source, expires_at, body)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return _call("post", json.dumps(card, ensure_ascii=False))

    @capability("update", risk="admin")
    def _update(self, id: str, state: str, message: str = "") -> dict:
        """Show what happened after a decision: state done (brief confirmation,
        then the card closes), failed (shows message) or gone (removed silently)."""
        state = str(state or "").strip().lower()
        if state not in STATES:
            return {"ok": False, "error": f"state must be one of {list(STATES)}"}
        return _call("update", str(id), state, _clip(message, 300))

    @capability("poll", risk="admin", tags=("sensitive",))
    def _poll(self, since: int = 0) -> dict:
        """Events after seq ``since``: {events, last}; pass ``last`` next time.

        Each event has seq (monotonic), ts (epoch s) and type: decision {id,
        decision approve|deny, reason?}, seen {id, via card|heads_up}, opened {id},
        dismissed {id}, acked {id}, unlocked {}, notice {id, app, category,
        priority, summary} (an urgent/family notification, summary only).
        Kept on the phone until polled (last 1000). reset=true: the phone's log
        restarted, all of it is returned; more=true: call again; gap=true: older
        events were dropped before they were read.
        """
        try:
            n = max(0, int(since or 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "since must be an integer"}
        return _call("poll", n)

    @capability("feed", risk="exec", tags=("sensitive",))
    def _feed(self, limit: int = 50) -> dict:
        """The phone's quiet feed, newest first: summaries of notifications it
        sorted as urgent, family or normal (never full content), plus ignored_7d."""
        try:
            n = max(1, min(int(limit), 300))
        except (TypeError, ValueError):
            return {"ok": False, "error": "limit must be an integer"}
        return _call("feed", n)

    @capability("rules_get", risk="exec", tags=("sensitive",))
    def _rules_get(self) -> dict:
        """The notification rules in use: source default|override and the rules document."""
        return _call("rulesGet")

    @capability("rules_set", risk="admin")
    def _rules_set(self, rules: list | dict | str | None = None) -> dict:
        """Replace the phone's notification rules. Empty resets to the shipped defaults.

        A list of rules goes in front of the defaults; a document {rules,
        family_names?, family_starred?, default?, extend_default?} replaces them
        (unless extend_default is true). Rule: {priority urgent|family|normal|ignore,
        category, match {packages, app_labels, words, senders}, scope?, except?,
        note?}; first match wins; "@family" in senders means family_names plus
        starred contacts.
        """
        try:
            payload = clean_rules(rules)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return _call("rulesSet", payload)


PLUGIN = AndroidAttentionPlugin
