"""Clear my cards: the person answers what waits on them by voice (Bake,
2026-10-23: "I also want to just start a voice session to clear these too.
Like it just gives me the questions and I answer for the cards. And they
clear as I answer.").

The voice (Haiku, no tools) routes ``cards`` when the person asks ("clear my
cards", "what do you need from me") and ``card {answer}`` with each answer.
This module is the hub's half, and the only judge of what counts:

* **Who.** Only the owner, on the owner's phone, recognised by speaker ID with
  a *sure* match (``Sessions._owner_sure``: the same rule as a spoken yes to a
  model change). A guest, a child, a "probably", a turn too short for a
  voiceprint, or a turn with no owner key never starts a pass or decides a
  card. The pass reads cards only to the sure owner (what waits is theirs).
* **Consent.** Spoken decisions are relayed as ``voice:<role>``: they count
  only once the person ticked "May relay my approvals" for the voice on the
  dashboard's Tokens page, like a phone. Dashboard-only items (secrets,
  tokens, policy, settings, job locks and ``approvals.relay_dashboard_only``)
  are named and left for the dashboard.
* **What was said.** The decision comes from the heard transcript of the
  person's turn, never from the voice's account: "yes" must be a yes and not
  also a no; the turn must be in the normal listening mode (not active
  listening, dictation or the scribe), after the question was spoken, and
  within two minutes of it.
* **Loosening** (accepting a trust-map proposal that isn't strictly
  stricter) and **sacred** approvals (a held call the trust map holds for the
  person alone): after the yes, the hub writes one plain sentence of the exact
  change and a challenge word, and the voice must read it back word for word
  (the hub checks the spoken reply's text). Only the *next* heard turn counts,
  and it must be the sure owner saying yes and the challenge word: a recording
  of an old yes doesn't know the word, and overheard or scribed audio never
  routes. The person's words, the voiceprint score, the read-back and the word
  go into the trust ledger (``Actor.evidence``) or the relay row.
* Tightening (a no, a proposal that only makes the map stricter) is immediate.

Every decision and skip is one line in the 30-day log (attention.log).
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import re
import secrets
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .sessions import Sessions

log = logging.getLogger("rook.hub.plugins.home.cardpass")

#: An answer counts only this long after its question was spoken.
ANSWER_WINDOW_S = 120.0
#: A pass with no answer for this long is over.
IDLE_S = 600.0
#: How many times the voice may fail to read a read-back word for word.
READBACK_TRIES = 2
YES = re.compile(r"\b(yes|yeah|yep|yup|approve(d)?|go ahead|do it|accept(ed)?|confirm(ed)?|"
                 r"correct|that's right|okay|ok)\b", re.I)
NO = re.compile(r"\b(no|nope|nah|don't|do not|deny|denied|decline(d)?|reject(ed)?|not now|"
                r"cancel|stop)\b", re.I)
#: Challenge words: common, easy to say, unlike yes, no or each other.
WORDS = ("apple", "banana", "carrot", "dolphin", "eagle", "falcon", "garden", "harbor",
         "island", "jungle", "kettle", "lemon", "mango", "napkin", "orange", "pepper",
         "rabbit", "salmon", "tiger", "violin", "walnut", "zebra", "anchor", "bucket", "candle",
         "dragon", "forest", "guitar", "hammer", "jacket", "ladder", "marble", "pencil",
         "rocket", "tomato", "window", "pillow", "copper", "meadow", "pumpkin")
ANSWERS = ("yes", "no", "skip", "explain", "repeat", "stop", "choose")
#: Most important first: a held call blocks someone now; then cards, questions,
#: house asks, trust proposals; oldest first within each.
ORDER = {"hold": 0, "job": 0, "card": 1, "ask": 2, "house": 3, "trust": 4}
FLOOR_WORDS = "it's about secrets, tokens, settings or who may do what"
ASK_HOW = ("Read this card to them plainly in a sentence or two, then ask for an answer in a "
           "few words, like \"yes, approve it\", \"no, not now\", \"skip\" or \"explain\". "
           "Then stop and wait.")


def norm(text: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9']+", " ", str(text or "").lower()).split())


def said(text: Any) -> str | None:
    """yes | no | None: what a heard transcript says, both or neither is None."""
    t = str(text or "")
    y, n = bool(YES.search(t)), bool(NO.search(t))
    if y and not n:
        return "yes"
    if n and not y:
        return "no"
    return None


def has_word(text: Any, word: str) -> bool:
    return bool(re.search(rf"\b{re.escape(word)}s?\b", str(text or ""), re.I))


def serial(fn):
    """One step of a role's pass at a time: two answers racing (two routed lines,
    a read-back arriving mid-answer) never decide twice or skip a card."""
    @functools.wraps(fn)
    async def run(self, role, *args, **kw):
        lock = self._locks.setdefault(role, asyncio.Lock())
        async with lock:
            return await fn(self, role, *args, **kw)
    return run


class CardPass:
    """One pass per role, in memory (a hub restart ends it; nothing is lost:
    the cards are still waiting)."""

    def __init__(self, sessions: "Sessions") -> None:
        self.s = sessions
        self.passes: dict[str, dict] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # -- plumbing ------------------------------------------------------------------
    @property
    def home(self):
        return self.s.home

    def relayer(self, role: str) -> str:
        return f"voice:{role}"

    def label(self) -> str:
        return "voice"

    def relay(self):
        ap = self.home._node.plugin("approvals") if self.home._node is not None else None
        return getattr(ap, "relay", None)

    def trust(self):
        from ..trust import node_store
        with contextlib.suppress(Exception):
            return node_store(self.home._node)
        return None

    def owner(self) -> str:
        return self.home.owner_name or "the owner"

    def log(self, outcome: str, what: str, why: str, how: str, ref: str = "") -> None:
        att = self.home._node.plugin("attention") if self.home._node is not None else None
        if att is not None and hasattr(att, "log"):
            att.log("voice", outcome, what, why, how, ref)

    async def say(self, role: str, text: str, marker: str | None = None) -> str:
        """Hand the voice something to say. ``marker`` (a fresh one by default)
        rides as the push's ``for_turn``, so the reply that says it is known."""
        from .agent_host import Unavailable
        marker = marker or f"cards-{secrets.token_hex(5)}"
        with contextlib.suppress(Unavailable):
            await self.s.voice_say(role, "(cards) " + text, marker)
        return marker

    def heard_turn(self, role: str, turn: Any) -> dict | None:
        if turn is None:
            return None
        return next((t for t in reversed(self.s.recent(role, IDLE_S))
                     if t.get("turn") == turn), None)

    def sure_owner(self, t: dict | None) -> bool:
        return bool(t) and bool((t.get("identity") or {}).get("owner")) and self.s._owner_sure(t)

    @staticmethod
    def score(t: dict | None) -> float | None:
        sp = ((t or {}).get("identity") or {}).get("speaker") or {}
        v = sp.get("score")
        return float(v) if isinstance(v, (int, float)) else None

    # -- what waits ----------------------------------------------------------------
    def waiting(self) -> list[dict]:
        """The relay's items plus pending trust proposals, most important first."""
        out = []
        relay = self.relay()
        if relay is not None:
            with contextlib.suppress(Exception):
                out += [dict(it) for it in relay.items()]
        st = self.trust()
        if st is not None:
            with contextlib.suppress(Exception):
                for prop in st.current().get("proposals") or []:
                    if prop.get("state") == "pending":
                        out.append({"kind": "trust", "id": str(prop["id"]),
                                    "created": float(prop.get("at") or 0), "proposal": prop,
                                    "relayable": True})
        out.sort(key=lambda i: (ORDER.get(i.get("kind"), 5), float(i.get("created") or 0)))
        return out

    def find(self, ident: str) -> dict | None:
        return next((i for i in self.waiting() if str(i["id"]) == ident), None)

    def describe(self, it: dict) -> dict:
        """What to say about one item: ``{title, ask, readback?, loosens, sacred}``."""
        from ..attention.service import speakable
        if it["kind"] == "trust":
            from ...trust import describe_change
            st = self.trust()
            d = describe_change(st.current() if st is not None else {}, it["proposal"]["change"])
            who = str(it["proposal"].get("by") or "an agent")
            why = " ".join(str(it["proposal"].get("why") or "").split())[:160]
            ask = (f"A change to the trust map, proposed by {who}: {d['sentence']} "
                   + (f"Their reason: {why}. " if why else "")
                   + ("This loosens the rules" + (", on something sacred" if d["sacred"] else "")
                      + ". " if d["loosens"] else "This only makes the rules stricter. "))
            # Only loosening is read back: tightening stays immediate, sacred or not
            # (Bake: "Tightening stays immediate"); "sacred" only changes the wording.
            return {"title": "a change to the trust map", "ask": ask, "readback": d["sentence"],
                    "loosens": d["loosens"], "sacred": d["sacred"], "strict": d["loosens"]}
        p = it.get("plain") or {}
        title = speakable(it)
        bits = [f"{title}."] if not title.endswith(("?", ".")) else [title]
        for key, word in (("for", "For"), ("who", "Who"), ("want", "It wants to"),
                          ("risk", "Risk")):
            v = " ".join(str(p.get(key) or "").split())
            if v and not (key == "for" and v == "no task"):
                bits.append(f"{word}: {v.rstrip('.')}.")
        opts = [str(o) for o in it.get("options") or []]
        if it["kind"] == "ask" and opts and not all(said(o) for o in opts):
            bits.append("The choices are " + ", ".join(opts[:5]) + ".")
        sacred = it.get("hold_kind") == "trust"
        readback = None
        if sacred:
            want = " ".join(str(p.get("want") or title).split()).rstrip(".")
            readback = f"Approve, just this once: {want}."
        return {"title": title, "ask": " ".join(bits), "readback": readback, "loosens": False,
                "sacred": sacred, "strict": sacred}

    # -- the pass ------------------------------------------------------------------
    def current(self, role: str) -> dict | None:
        p = self.passes.get(role)
        if p is not None and time.time() - p["last"] > IDLE_S:
            self.passes.pop(role, None)
            return None
        return p

    @serial
    async def start(self, role: str, turn: Any = None) -> dict:
        heard = self.heard_turn(role, turn)
        if not self.sure_owner(heard):
            await self.say(role, f"I can go through cards only with {self.owner()}, recognised "
                                 "by voice, and I couldn't confirm it's them. Say so in a few "
                                 "words: they can answer on the phone, or try again; if I don't "
                                 "know their voice yet, they can say \"enroll my voice\".")
            return {"ok": False, "error": "not the owner, recognised by voice"}
        relay = self.relay()
        if relay is None or not relay.may_relay(self.relayer(role)):
            await self.say(role, f"Answering cards by voice is off. {self.owner()} turns it on "
                                 "on the dashboard's Tokens page (May relay my approvals, on "
                                 "the voice's row). Say that briefly.")
            return {"ok": False, "error": "voice decisions are off (Tokens page)"}
        items = self.waiting()
        if not items:
            self.passes.pop(role, None)
            await self.say(role, "Nothing is waiting on them: tell them their cards are clear.")
            return {"ok": True, "cards": 0}
        now = time.time()
        self.passes[role] = {"ids": [str(i["id"]) for i in items], "n": len(items), "pos": 0,
                             "cur": None, "started": now, "last": now, "decided": 0,
                             "left": 0}
        await self.next(role, f"There are {len(items)} waiting. ")
        return {"ok": True, "cards": len(items)}

    async def next(self, role: str, lead: str = "", turn: Any = None) -> None:
        """Ask the next card still waiting; cards decided elsewhere meanwhile are
        passed over, dashboard-only ones named and left."""
        p = self.passes.get(role)
        if p is None:
            return
        while p["pos"] < len(p["ids"]):
            ident = p["ids"][p["pos"]]
            p["pos"] += 1
            it = self.find(ident)
            if it is None:
                continue                      # decided or withdrawn elsewhere
            d = self.describe(it)
            num = f"Card {p['pos']} of {p['n']}: "
            if not it.get("relayable", True):
                why = str(it.get("why_not") or "")
                words = FLOOR_WORDS if why.startswith("never relayed") else "it needs the dashboard"
                lead += (f"{num}{d['title']}. That one stays on the dashboard, because "
                         f"{words}. ")
                p["left"] += 1
                continue
            p["cur"] = cur = {"id": ident, "kind": it["kind"], "title": d["title"],
                              "stage": "ask", "readback": d["readback"],
                              "strict": bool(d["strict"]), "tries": 0,
                              "word": None, "options": [str(o) for o in it.get("options") or []]}
            p["last"] = time.time()
            await self._ask(role, cur, f"{lead}{num}{d['ask']} {ASK_HOW}")
            return
        decided, left = p["decided"], p["left"] + 0
        self.passes.pop(role, None)
        await self.say(role, f"{lead}That's all of them: {decided} answered"
                             + (f", {left} left for the dashboard or the phone" if left else "")
                             + ". Tell them in a sentence.")

    async def _ask(self, role: str, cur: dict, text: str) -> None:
        """Say ``text`` for the current card under a fresh marker: only the reply
        that carries it marks the card as asked."""
        cur.update(marker=f"cards-{secrets.token_hex(5)}", spoken=None, sent=time.time())
        await self.say(role, text, cur["marker"])

    @serial
    async def spoken(self, role: str, reply: dict) -> None:
        """A pushed reply finished: what the voice said to the person. The reply to
        the current card's own message marks it as asked; a read-back counts only
        when it was read word for word (else it is asked once more, then left)."""
        p = self.current(role)
        if p is None or p.get("cur") is None or not reply.get("push"):
            return
        cur = p["cur"]
        if reply.get("for_turn") != cur.get("marker") or cur.get("spoken") is not None:
            return
        if reply.get("ok") is False or reply.get("interrupted"):
            return                            # talked over: the next answer is refused
        text = str(reply.get("text") or "")
        if cur["stage"] != "confirm":
            cur["spoken"] = time.time()
            return
        if norm(cur["readback"]) in norm(text) and has_word(text, cur["word"]):
            cur["spoken"] = time.time()
            return
        cur["tries"] += 1
        if cur["tries"] >= READBACK_TRIES:
            p["left"] += 1
            self.log("skipped", f"Not decided by voice: {cur['title']}",
                     "the change couldn't be read back word for word", "the voice", cur["id"])
            await self.next(role, "I couldn't read that change back word for word, so it "
                                  "stays on its card for the phone or the dashboard. ")
            return
        await self._ask(role, cur, self._readback_text(cur))

    @serial
    async def answer(self, role: str, args: dict, turn: Any = None) -> dict:
        p = self.current(role)
        if p is None or p.get("cur") is None:
            raise ValueError("no card is being read: they can say 'clear my cards'")
        cur = p["cur"]
        ans = str(args.get("answer") or "").strip().lower()
        if ans not in ANSWERS:
            raise ValueError("answer: yes, no, skip, explain, repeat, stop or choose")
        p["last"] = time.time()
        if ans == "stop":
            done = p["decided"]
            self.passes.pop(role, None)
            await self.say(role, f"Stopped. {done} answered; the rest wait on their cards. Say "
                                 "so in a few words.")
            return {"ok": True, "stopped": True}
        if ans == "skip":
            p["left"] += 1
            self.log("skipped", f"Skipped by voice: {cur['title']}", "the person said skip",
                     f"{self.owner()} to the voice", cur["id"])
            await self.next(role, "Skipped. ")
            return {"ok": True, "skipped": cur["id"]}
        it = self.find(cur["id"])
        if it is None:
            await self.next(role, "That one was already decided somewhere else. ")
            return {"ok": True, "gone": cur["id"]}
        if ans in ("repeat", "explain"):
            d = self.describe(it)
            cur.update(stage="ask", word=None, tries=0)
            extra = ""
            if ans == "explain" and it["kind"] != "trust":
                p_ = it.get("plain") or {}
                why = " ".join(str(p_.get("why") or "").split())
                extra = (f" Why: {why.rstrip('.')}." if why else "") + \
                    " The full detail is on the phone's card under Explain."
            await self._ask(role, cur, f"{d['ask']}{extra} {ASK_HOW}")
            return {"ok": True, ans: cur["id"]}
        # yes, no or choose: the person's own decision, checked here.
        heard = self.heard_turn(role, turn)
        why = self._refuse(role, p, cur, heard, ans, args)
        if why and why[0] == "unasked":
            # Answered before the card was heard out (talked over, or still being read):
            # read it again and wait for the answer to that.
            d = self.describe(it)
            text = self._readback_text(cur) if cur["stage"] == "confirm" else \
                f"Before answering, they need to hear the card. {d['ask']} {ASK_HOW}"
            await self._ask(role, cur, text)
            return {"ok": False, "error": "answered before the card was read"}
        if why:
            await self.say(role, why[1])
            return {"ok": False, "error": why[0]}
        relay = self.relay()
        if relay is None or not relay.may_relay(self.relayer(role)):
            await self.say(role, "Answering cards by voice was turned off. Say so briefly.")
            self.passes.pop(role, None)
            return {"ok": False, "error": "voice decisions are off (Tokens page)"}
        if ans in ("yes", "choose") and cur["stage"] == "ask" and not cur["strict"]:
            # The map may have changed since the card was read (an entry deleted on the
            # dashboard): judged again now, so a change that became a loosening is read back.
            d = self.describe(it)
            cur["strict"] = bool(d["strict"])
            cur["readback"] = d["readback"]       # the change as it reads against today's map
        if ans in ("yes", "choose") and cur["strict"] and cur["stage"] == "ask":
            cur.update(stage="confirm", word=secrets.choice(WORDS), tries=0,
                       first_yes=" ".join(str(heard.get("text") or "").split())[:300])
            await self._ask(role, cur, self._readback_text(cur))
            return {"ok": True, "confirm": cur["id"]}
        return await self._decide(role, p, cur, it, ans, heard, args)

    @staticmethod
    def _readback_text(cur: dict) -> str:
        return (f"(read exactly) Say these words exactly, word for word, and nothing before "
                f"them: \"To confirm: {cur['readback']} If that's right, say yes, "
                f"{cur['word']}.\" Then stop and wait.")

    def _refuse(self, role: str, p: dict, cur: dict, heard: dict | None, ans: str,
                args: dict) -> tuple[str, str] | None:
        """Why a spoken answer doesn't count (machine reason, words for the voice)."""
        who = self.owner()
        if heard is None:
            return ("no heard turn", "I didn't hear an answer to that card. Ask again.")
        mode = str(heard.get("mode") or "").strip().lower()
        if mode not in ("", "assistant"):
            return ("not in the normal mode", "Cards are answered only in the normal "
                    "conversation, not while listening or taking notes. Ask again.")
        if cur.get("spoken") is None:
            return ("unasked", "")
        if not heard.get("at", 0) > cur["spoken"] or heard["at"] - cur["spoken"] > ANSWER_WINDOW_S:
            return ("not an answer to this card", "That answer came too late for this card. "
                    "Ask again.")
        if not self.sure_owner(heard):
            self.log("skipped", f"A spoken answer didn't count: {cur['title']}",
                     "the voice wasn't recognised as the owner's (sure match)",
                     "speaker ID", cur["id"])
            return ("not the owner, recognised by voice",
                    f"I couldn't confirm that was {who}'s voice, so I left this card as it is. "
                    "Say so: they can answer on the phone, or try again in a few words.")
        text = str(heard.get("text") or "")
        if ans == "choose":
            choice = str(args.get("choice") or "")
            hit = [o for o in cur["options"] if norm(o) and norm(o) in norm(text)]
            if not choice or len(hit) != 1 or norm(hit[0]) != norm(choice):
                return ("no clear choice", "I didn't catch which choice. Ask again, naming "
                        "the choices.")
            args["choice"] = hit[0]
        elif said(text) != ans:
            return ("no clear yes or no", "I didn't catch a clear yes or no. Ask again.")
        if cur["stage"] == "confirm" and ans in ("yes", "choose"):
            after = [t for t in self.s.recent(role, IDLE_S) if t.get("at", 0) > cur["spoken"]]
            if not after or after[0] is not heard and after[0].get("turn") != heard.get("turn"):
                return ("not the first turn after the read-back", "That wasn't the answer right "
                        "after the read-back. Read it back again.")
            if not has_word(text, cur["word"]):
                return ("no challenge word", f"I need {who} to say yes and the word "
                        f"{cur['word']}. Ask again.")
            if norm(cur["readback"]) in norm(text):
                return ("an echo of the read-back", "I heard the read-back itself, not an "
                        "answer. Ask again.")
        return None

    async def _decide(self, role: str, p: dict, cur: dict, it: dict, ans: str, heard: dict,
                      args: dict) -> dict:
        import datetime as _dt
        who = self.owner()
        score = self.score(heard)
        words = " ".join(str(heard.get("text") or "").split())[:300]
        stamp = _dt.datetime.fromtimestamp(heard["at"], _dt.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        session = (f"voice:{role} turn {heard.get('turn')} {stamp} voiceprint "
                   f"{score if score is not None else '?'} (sure)")
        approve = ans in ("yes", "choose")
        verb = "Approved" if approve else "Declined"
        try:
            if it["kind"] == "trust":
                from ...trust import Actor, TrustError
                st = self.trust()
                if st is None:
                    raise ValueError("the trust map is unavailable")
                evidence = (("voiceprint", score), ("sure", True), ("turn", str(heard.get("turn"))),
                            ("heard_at", stamp), ("readback", cur.get("readback") if
                                                  cur["stage"] == "confirm" else None),
                            ("challenge", cur.get("word")), ("first_yes", cur.get("first_yes")))
                try:
                    st.change("decide_proposal", {"id": it["id"], "accept": approve},
                              Actor("person", who, principal=self.relayer(role), words=words,
                                    session=session, via="voice", evidence=evidence))
                except TrustError as e:
                    raise ValueError(str(e)) from None
                recorded = f"{'accepted' if approve else 'rejected'} by {who} via voice"
            else:
                relay = self.relay()
                quote = words + (f" (after the read-back: {cur['readback']} / {cur['word']})"
                                 if cur["stage"] == "confirm" else "")
                out = await relay.decide(
                    it["id"], "approve" if approve else "deny",
                    {"who": who, "quote": quote, "session": session},
                    relayer=self.relayer(role), relayer_label=self.label(),
                    answer=str(args.get("choice") or ""))
                recorded = str(out.get("recorded") or f"{verb.lower()} by {who} via voice")
        except (PermissionError, ValueError, LookupError) as e:
            msg = str(e)
            if "no waiting item" in msg or "no pending proposal" in msg:
                await self.next(role, "That one was already decided somewhere else. ")
                return {"ok": True, "gone": it["id"]}
            p["left"] += 1
            await self.next(role, f"That one didn't go through ({msg[:120]}), so it stays on "
                                  "its card. ")
            return {"ok": False, "error": msg[:300]}
        p["decided"] += 1
        self.log("decided", f"{verb} by voice: {cur['title']}",
                 f"{who}'s spoken answer, voiceprint sure"
                 + (f" ({score:.2f})" if score is not None else "")
                 + (", read back and confirmed" if cur["stage"] == "confirm" else ""),
                 f"{who} said {'yes' if approve else 'no'} to the voice", it["id"])
        self.home.journal("home.voice_decide", {"item": it["id"], "kind": it["kind"],
                                                "decision": "approve" if approve else "deny",
                                                "confirmed": cur["stage"] == "confirm"},
                          {"ok": True, "voiceprint": score})
        await self.next(role, f"Done: {verb.lower()}. ")
        return {"ok": True, "decided": it["id"], "recorded": recorded[:200]}
