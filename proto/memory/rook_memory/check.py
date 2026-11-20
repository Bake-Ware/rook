"""The made-up set's regression expectations (madeup/expect.toml), checked against what memory holds.

Run after `play madeup/conversations.json` on an empty store. These test what the model did with
the rules, so they cost a full play to run; the plain mechanics are in tests/.

Kinds of check:
- belief: the belief closest to `query` (it must be at least `closeness` close); then any of
  pinned, direction (+1/-1), grade (tentative/settling/mature), min_flips, about, unpinned_once,
  min_against_since_pin.
- no_belief: no belief about `about` is at least `closeness` close to `query`.
- person_only: every belief about the person stands only on his own words (others' links count 0).
- voice_tentative: a belief standing only on voice lines is tentative.
- pins: exactly `count` beliefs pinned now.
- miss: recall for `query` turns up nothing ("I don't remember").
"""

import tomllib

from . import embed, recall, standing


def _grade(st, knobs):
    w = knobs["weigh"]
    if st["certainty"] < w["tentative_below"]:
        return "tentative"
    return "mature" if st["certainty"] >= w["mature_at"] else "settling"


def run(store, knobs, path, out=print):
    with open(path, "rb") as f:
        checks = tomllib.load(f)["check"]
    emb = embed.embedder(store.folder, knobs)
    person = knobs["people"]["person"]
    beliefs = store.all("SELECT * FROM beliefs ORDER BY id")
    vecs = recall._vectors(store, knobs, "belief", beliefs, emb)
    passed = 0
    for c in checks:
        ok, why = True, []
        t = c["type"]
        if t in ("belief", "no_belief"):
            q = emb([c["query"]])[0]
            pool = [b for b in beliefs if t == "belief" or b["about"] == c.get("about", person)]
            scored = sorted(((float(vecs[b["id"]] @ q), b) for b in pool), key=lambda x: -x[0])
            if t == "no_belief":
                hits = [(s, b) for s, b in scored if s >= c["closeness"]]
                ok = not hits
                why = [f"b{b['id']} ({s:.2f}) {b['text']}" for s, b in hits] or ["none close"]
            elif not scored or scored[0][0] < c.get("closeness", 0):
                ok, why = False, ["no belief close enough" + (f" (best {scored[0][0]:.2f})" if scored else "")]
            else:
                s, b = scored[0]
                st = standing.of(store, knobs, b["id"])
                why.append(f"b{b['id']} ({s:.2f}) {b['text']} -> {standing.summary(st)}")
                tests = {
                    "pinned": lambda v: st["pinned"] == v,
                    "direction": lambda v: st["direction"] == v,
                    "grade": lambda v: _grade(st, knobs) == v,
                    "min_flips": lambda v: st["flips"] >= v,
                    "about": lambda v: b["about"] == v,
                    "unpinned_once": lambda v: any(p["action"] == "unpin" for p in store.pins(b["id"])) == v,
                    "min_against_since_pin": lambda v: st.get("against_since_pin", 0) >= v,
                }
                for k, f in tests.items():
                    if k in c and not f(c[k]):
                        ok = False
                        why.append(f"expected {k} = {c[k]}")
        elif t == "person_only":
            for b in beliefs:
                if b["about"] != person:
                    continue
                links = store.links(b["id"])
                if any(l["said_by"] != person and l["strength"] > 0 for l in links) or \
                        not any(l["said_by"] == person and l["strength"] > 0 for l in links):
                    ok = False
                    why.append(f"b{b['id']} {b['text']}")
            why = why or ["all of them"]
        elif t == "voice_tentative":
            for b in beliefs:
                counted = [l for l in store.links(b["id"]) if l["strength"] > 0]
                if counted and all(l["channel"] == "voice" for l in counted):
                    st = standing.of(store, knobs, b["id"])
                    if not st["pinned"] and _grade(st, knobs) != "tentative":
                        ok = False
                        why.append(f"b{b['id']} {standing.summary(st)}: {b['text']}")
                    else:
                        why.append(f"b{b['id']} tentative: {b['text']}")
            why = why or ["no belief stands on voice lines alone"]
        elif t == "pins":
            pinned = [b for b in beliefs if store.pinned(b["id"])]
            ok = len(pinned) == c["count"]
            why = [f"b{b['id']} {b['text']}" for b in pinned] or ["none"]
        elif t == "miss":
            last = store.one("SELECT MAX(at) AS at FROM entries")["at"]
            r = recall.recall(store, knobs, c["query"], last, emb=emb)
            ok = r.missed
            why = [f"best closeness {r.best:.3f}"] + [f"{i.tag} {i.closeness:.2f} {i.text[:80]}" for i in r.items()]
        else:
            ok, why = False, [f"unknown check type {t}"]
        passed += ok
        out(f"{'PASS' if ok else 'FAIL'}  {c['name']}")
        for w in why:
            out(f"      {w}")
    out(f"\n{passed} of {len(checks)} passed")
    return passed == len(checks)
