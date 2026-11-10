// Rookie's 0.5 wiring: which Rookie state (rookie.js STATES) the site shows
// for what the house agent is doing. Inputs are what the site already sees:
// the dock chat (agent.chat.read events), the dock agent's calls for this
// person (/web/api/activity: start and end of each call, holds) and the
// person typing. The table is in docs/castling-first-run.md; keep the two
// in step. No DOM here: tests run it under node.
"use strict";

(function (root) {
  // A cap -> a DOING state (or a mood). First match wins: exact caps, then
  // families ("shell." = every shell cap), then the cap's last word, then
  // "working".
  const EXACT = {
    "agent.backend": "thinking",          // the agent's own turn on its AI backend
    "agent.chat.send": "messaging",
    "agent.skill": "reading",
    "agent.note": "writing",
    "agent.wake": "spawning",
    "stream.read": "loading",             // following a stream (a long call, a terminal)
    "stream.write": "writing",
    "stream.open": "spawning",
    "shell.open": "exec",
    "voice.speak": "talking",
    "voice.tts": "talking",
    "voice.ask": "listening",
    "voice.stt": "listening",
    "speech.hear": "listening",
    "approval.ask": "messaging",
    "core.elevate": "locking",
    "core.plugin.install": "deploying",
    "core.plugin.rollback": "deploying",
    "backup-store.put": "uploading",
    "backup-store.pull": "downloading",
    "backup-store.get": "downloading",
    "torrent.add": "downloading",
  };
  const FAMILY = [
    ["shell.", "exec"], ["proc.", "exec"], ["job.run", "exec"],
    ["browser.", "websearch"], ["web.", "websearch"], ["http.", "websearch"],
    ["secret.", "locking"], ["vault.", "locking"], ["token.", "locking"], ["trust.", "locking"],
    ["notify.", "messaging"], ["notification.", "messaging"], ["telegram.", "messaging"],
    ["discord.", "messaging"], ["sms.", "messaging"], ["call.", "messaging"], ["huddle.", "messaging"],
    ["voice.", "messaging"], ["speech.", "messaging"],
    ["media.", "music"], ["sound.", "music"],
  ];
  const VERB = {
    turn: "thinking",                     // an AI backend's turn (agents-opencode.turn, ...)
    search: "searching", find: "searching",
    read: "reading", get: "reading", list: "reading", stat: "reading", status: "reading", view: "reading",
    diff: "reading", skill: "reading", manifests: "reading", workers: "reading",
    write: "writing", create: "writing", update: "writing", set: "writing", post: "writing",
    comment: "writing", note: "writing", link: "writing", setup: "writing",
    delete: "delete", remove: "delete", clear: "delete", archive: "delete", revoke: "delete",
    install: "deploying", rollback: "deploying", distribute: "deploying", merge: "deploying",
    test: "testing", check: "testing", checks: "testing",
    upload: "uploading", put: "uploading", download: "downloading", pull: "downloading", fetch: "downloading",
  };

  function capState(cap) {
    cap = String(cap || "");
    if (EXACT[cap]) return EXACT[cap];
    for (const [prefix, state] of FAMILY) if (cap === prefix || cap.startsWith(prefix)) return state;
    const verb = cap.slice(cap.lastIndexOf(".") + 1);
    return VERB[verb] || "working";
  }

  const FLASH_MS = 2500;            // an error or a denial shows this long
  const SLEEP_MS = 10 * 60 * 1000;  // nothing for this long: asleep
  const TYPING_MS = 4000;           // the person typing: listening, this long after the last key

  // Drives one Rookie from events. ``set(state)`` and ``say(text)`` are the
  // Rookie instance's; ``now()`` is a clock (tests pass their own).
  class Driver {
    constructor(rookie, now) {
      this.rookie = rookie;
      this.now = now || (() => Date.now());
      this.flight = [];             // calls in flight, oldest first: {call, state}
      this.holds = new Map();       // hold name -> call id, while it waits on the person
      this.flash = null;            // {state, until}
      this.typing = -Infinity;      // last key, ms
      this.waiting = false;         // the person sent a message the agent hasn't answered
      this.last = this.now();       // anything at all happened
      this.shown = null;
      this.talkUntil = 0;
    }

    // What Rookie should show now.
    state() {
      const t = this.now();
      if (this.flash && t < this.flash.until) return this.flash.state;
      if (this.flight.length) return this.flight[this.flight.length - 1].state;
      if (this.waiting) return "thinking";
      if (this.holds.size) return "waiting";
      if (t - this.typing < TYPING_MS) return "listening";
      if (t - this.last >= SLEEP_MS) return "sleeping";
      return "idle";
    }

    show() {
      const s = this.state();
      if (this.now() < this.talkUntil && s === "idle") return s;    // let say() finish
      if (s !== this.shown) {
        this.shown = s;
        this.rookie.set(s);
      }
      return s;
    }

    touch() {
      this.last = this.now();
    }

    // /web/api/activity: {phase: start|end, call, cap, decision?, ok?, hold?}
    activity(ev) {
      this.touch();
      if (ev.phase === "start") {
        this.flight.push({ call: ev.call, state: capState(ev.cap) });
      } else {
        this.flight = this.flight.filter((f) => f.call !== ev.call);
        if (ev.decision === "held" && ev.hold) this.holds.set(ev.hold, ev.call);
        else if (ev.decision === "released" || ev.decision === "refused") this.holds.delete(ev.hold);
        if (ev.decision === "deny" && ev.ok === false) this.flash = { state: "confused", until: this.now() + FLASH_MS };
        else if (ev.ok === false && ev.decision !== "held") this.flash = { state: "error", until: this.now() + FLASH_MS };
      }
      return this.show();
    }

    // agent.chat.read events: input (the person, or the site), turn reply, status error
    chat(ev) {
      this.touch();
      const d = ev.data || {};
      if (ev.type === "input") {
        this.waiting = true;
        this.typing = -Infinity;
      } else if (ev.type === "turn" && d.role === "reply") {
        this.waiting = false;
        this.flight = [];
        if (typeof d.text === "string" && d.text) {
          this.shown = "talking";
          this.talkUntil = this.now() + Math.min(9000, 1400 + d.text.length * 55);
          this.rookie.say(d.text, { then: this.state() });
          return "talking";
        }
      } else if (ev.type === "status" && d.state === "error") {
        this.waiting = false;
        this.flight = [];
        this.flash = { state: "error", until: this.now() + FLASH_MS };
      }
      return this.show();
    }

    // The person typing in the chat box.
    key() {
      this.touch();
      this.typing = this.now();
      return this.show();
    }

    // Every second or so: flashes end, idle turns to sleep.
    tick() {
      return this.show();
    }
  }

  const api = { capState, Driver, FLASH_MS, SLEEP_MS, TYPING_MS };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.RookieMap = api;
})(typeof window !== "undefined" ? window : this);
