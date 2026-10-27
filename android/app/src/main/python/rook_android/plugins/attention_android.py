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
try:
    _Remind = _jclass("systems.bake.rook.SpokenReminder")
except Exception:  # pragma: no cover - only importable on a Chaquopy host
    _Remind = None

KINDS = ("hold", "job", "card", "ask", "alert")
CHANNELS = ("urgent", "family", "approvals", "digest", "jobs")
ACTIONS = ("yes", "no", "choose", "explain", "ack")
STATES = ("done", "failed", "gone")
LINE_MAX, EXPLAIN_MAX, ID_MAX = 140, 2000, 64
BODY_MAX, BODY_LINES = 600, 6
#: A question's choices: as many and as long as a huddle ask takes, kept verbatim.
OPTIONS_MAX, OPTION_MAX = 6, 200
RULES_MAX_BYTES = 64 * 1024
REMIND_TEXT_MAX = 300
SAMPLE_MIN_MS, SAMPLE_MAX_MS, SAMPLE_DEFAULT_MS = 1000, 8000, 3000
_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


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


def _options(value) -> list[str]:
    """A question's choices, each kept as written (only trimmed). Raises ValueError."""
    if value is None or value == "" or value == []:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("options must be a list of short answers")
    out = [str(o).strip() for o in value]
    if len(out) > OPTIONS_MAX or not all(out) or any(len(o) > OPTION_MAX for o in out):
        raise ValueError(f"options must be 1-{OPTIONS_MAX} answers of 1-{OPTION_MAX} characters")
    if len(set(out)) != len(out):
        raise ValueError("options must differ from each other")
    return out


def clean_post(id, kind="card", line="", explain="", urgency="normal", channel="approvals",
               actions=None, source="", expires_at=0, body="", options=None) -> dict:
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
    opts = _options(options)
    if ("choose" in acts) != bool(opts):
        raise ValueError("the choose action and options go together")
    try:
        exp = float(expires_at or 0)
    except (TypeError, ValueError):
        raise ValueError("expires_at must be epoch seconds") from None
    if exp != exp or exp < 0:
        raise ValueError("expires_at must be epoch seconds")
    return {"id": cid, "kind": kind, "line": text, "body": _body(body), "explain": _clip(explain, EXPLAIN_MAX),
            "urgency": urgency, "channel": channel, "actions": acts or ["ack"], "source": _clip(source, 80),
            "expires_at": exp, "options": opts}


def _flag(value, name: str) -> bool:
    """A boolean-ish arg; ``"false"`` must not become True. Raises ValueError."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)) and value == value:
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    raise ValueError(f"{name} must be true or false, got {value!r}")


def clean_remind(id, text, sample_ms=SAMPLE_DEFAULT_MS, speak_unchecked=False) -> dict:
    """Validate and normalize attention.remind args. Raises ValueError."""
    cid = str(id if id is not None else "").strip()
    if not cid or len(cid) > ID_MAX:
        raise ValueError(f"id must be 1-{ID_MAX} characters")
    line = " ".join(str(text if text is not None else "").split())
    if not line:
        raise ValueError("text is required")
    if len(line) > REMIND_TEXT_MAX:
        raise ValueError(f"text must be at most {REMIND_TEXT_MAX} characters")
    if isinstance(sample_ms, bool):
        raise ValueError("sample_ms must be a number of milliseconds")
    try:
        ms = float(SAMPLE_DEFAULT_MS if sample_ms is None or sample_ms == "" else sample_ms)
    except (TypeError, ValueError):
        raise ValueError("sample_ms must be a number of milliseconds") from None
    if ms != ms:
        raise ValueError("sample_ms must be a number of milliseconds")
    ms = int(max(SAMPLE_MIN_MS, min(SAMPLE_MAX_MS, ms)))
    return {"id": cid, "text": line, "sample_ms": ms, "speak_unchecked": _flag(speak_unchecked, "speak_unchecked")}


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

    @capability("post", risk="admin", tags=("physical",), scope="local")
    def _post(self, id: str, kind: str = "card", line: str = "", explain: str = "", urgency: str = "normal",
              channel: str = "approvals", actions: list | str | None = None, source: str = "",
              expires_at: float = 0, body: str = "", options: list | None = None) -> dict:
        """Put a decision card on the phone; re-posting the same id updates it without ringing again.

        id: up to 64 chars, your handle for attention.update and the events.
        kind: hold|job|card|ask|alert. line: the plain title (140 chars).
        body: optional short lines always shown under the title, e.g. "For: ...",
        "Who: ...", "I want to: ...", "Risk: ..." one per line (newlines kept,
        blank lines dropped, at most 6 lines / 600 chars).
        explain: shown under More details (2000 chars, newlines kept). urgency: urgent rings
        like an alarm and takes over the screen (also channel=urgent); normal is a
        heads-up that opens the card when the phone is next unlocked.
        channel: urgent|family|approvals|digest|jobs. actions: subset of
        yes/no/choose/explain/ack (default yes,no,explain; ack for alerts).
        options: a question's choices (1-6, up to 200 chars each), one button
        each, with the choose action; the tapped one comes back verbatim.
        source: who asks, shown small. expires_at: epoch seconds, 0 = never.
        The answer comes back through attention.poll as a "decision" event.
        """
        try:
            card = clean_post(id, kind, line, explain, urgency, channel, actions, source, expires_at, body, options)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return _call("post", json.dumps(card, ensure_ascii=False))

    @capability("remind", risk="admin", tags=("physical",), scope="local")
    def _remind(self, id: str, text: str, sample_ms: int = SAMPLE_DEFAULT_MS, speak_unchecked: bool = False) -> dict:
        """Speak a short reminder on the phone, but only if the room is quiet.

        Returns at once: {ok, id, accepted}. accepted=false with busy (another
        reminder is running) or reason "off" (the app's Spoken reminders switch).
        The phone then skips it in Do Not Disturb or bedtime (outcome dnd),
        during a call, its own voice session, another
        app recording, media playing or speech already playing; otherwise it
        listens sample_ms (1000-8000) and speaks text (at most 300 chars) in the
        app's voice only if nobody is talking. When Android silences the mic
        (background, no permission) it speaks only if speak_unchecked is true.
        The result arrives as a "reminded" event in attention.poll. The sound
        never leaves the phone; the event carries two rounded level numbers.
        """
        try:
            r = clean_remind(id, text, sample_ms, speak_unchecked)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if _Remind is None or app_context() is None:
            return {"ok": False, "error": "not an Android host"}
        try:
            return json.loads(str(_Remind.start(app_context(), r["id"], r["text"], r["sample_ms"], r["speak_unchecked"])))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    @capability("update", risk="admin", scope="local")
    def _update(self, id: str, state: str, message: str = "") -> dict:
        """Show what happened after a decision: state done (brief confirmation,
        then the card closes), failed (shows message) or gone (removed silently)."""
        state = str(state or "").strip().lower()
        if state not in STATES:
            return {"ok": False, "error": f"state must be one of {list(STATES)}"}
        return _call("update", str(id), state, _clip(message, 300))

    @capability("poll", risk="admin", tags=("sensitive",), scope="local")
    def _poll(self, since: int = 0) -> dict:
        """Events after seq ``since``: {events, last}; pass ``last`` next time.

        Each event has seq (monotonic), ts (epoch s) and type: decision {id,
        decision approve|deny|choose, reason?, choice? (the option tapped, verbatim)}, seen {id, via card|heads_up}, opened {id},
        dismissed {id}, acked {id}, unlocked {}, notice {id, app, category,
        priority, summary} (an urgent/family notification, summary only),
        reminded {id, outcome spoken|dnd|busy|call|voice_session|mic_in_use|
        media|speaking|unchecked|off|error, via? server|device, level? {active_frac,
        peak_dbfs}, checked? false (spoken without a quiet check)} (attention.remind).
        Kept on the phone until polled (last 1000). reset=true: the phone's log
        restarted, all of it is returned; more=true: call again; gap=true: older
        events were dropped before they were read.
        """
        try:
            n = max(0, int(since or 0))
        except (TypeError, ValueError):
            return {"ok": False, "error": "since must be an integer"}
        return _call("poll", n)

    @capability("feed", risk="exec", tags=("sensitive",), scope="local")
    def _feed(self, limit: int = 50) -> dict:
        """The phone's quiet feed, newest first: summaries of notifications it
        sorted as urgent, family or normal (never full content), plus ignored_7d."""
        try:
            n = max(1, min(int(limit), 300))
        except (TypeError, ValueError):
            return {"ok": False, "error": "limit must be an integer"}
        return _call("feed", n)

    @capability("rules_get", risk="exec", tags=("sensitive",), scope="local")
    def _rules_get(self) -> dict:
        """The notification rules in use: source default|override and the rules document."""
        return _call("rulesGet")

    @capability("rules_set", risk="admin", scope="local")
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
