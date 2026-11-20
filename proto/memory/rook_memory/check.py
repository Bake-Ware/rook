"""The made-up set's regression expectations (madeup/expect.toml), checked against what memory holds.

Run after `play madeup/conversations.json` on an empty store. These test what the model did with
the rules, so they cost a full play to run; the plain mechanics are in tests/. `tally` runs the same
checks over several stores (repeated plays) and counts how often each fails.

Kinds of check:
- belief: the belief closest to `query` (at least `closeness` close); then any of pinned, direction
  (+1/-1), grade (tentative/settling/mature), min_flips, about, unpinned_once, min_against_since_pin,
  never_flipped (its direction never changed in its history), his_words (its text is words from one
  of Bake's own messages).
- no_belief: no belief about `about` is at least `closeness` close to `query`.
- count_like: at most `max` beliefs (holding, at least `closeness` close to `query`).
- distinct: the beliefs closest to `query` and to `other` are not the same belief.
- person_only: every belief about the person stands only on his own words (others' links count 0).
- voice_tentative: a belief standing only on voice lines is tentative.
- pins: exactly `count` beliefs pinned now; and each of `pinned` (queries) is pinned, each of
  `not_pinned` is not (judged on the closest belief).
- miss: recall for `query` turns up nothing ("I don't remember").
- recalls: recall for `query` turns up a belief or episode containing all of `words`.
- replies_lack: no house reply matches `pattern` (a regular expression), except replies to turns
  that pinned or unpinned.
- reply: the house reply to the message containing `to` matches `has` (if given) and not `lacks`.
- things_once: at most `max` pairs of things that may be one thing (a shared name or alias, or names
  at least `closeness` close).
- beliefs_once: at most `max` pairs of holding beliefs at least `closeness` close (opposites aside).
- about_coverage: at least `min` of the beliefs are about some thing.
"""

import copy
import pathlib
import re
import tomllib

from . import embed, recall, search, standing
from .store import Store


def _grade(st, knobs):
    w = knobs["weigh"]
    if st["certainty"] < w["tentative_below"]:
        return "tentative"
    return "mature" if st["certainty"] >= w["mature_at"] else "settling"


def _closest(beliefs, vecs, q):
    return sorted(((float(vecs[b["id"]] @ q), b) for b in beliefs), key=lambda x: -x[0])


def _norm(t):
    return " ".join(re.findall(r"[a-z0-9']+", (t or "").lower()))


def checks(path):
    with open(path, "rb") as f:
        return tomllib.load(f)["check"]


def evaluate(store, knobs, path):
    """Answers [(name, ok, [why...])]."""
    # Which belief a check means is judged by one fixed embedding model, whatever search is under test;
    # the recall checks (miss, recalls) use the search under test.
    judge = copy.deepcopy(knobs)
    judge["search"]["model"] = knobs.get("check", {}).get("judge", "bge-small")
    emb = embed.embedder(store.folder, judge)
    person = knobs["people"]["person"]
    beliefs = store.all("SELECT * FROM beliefs ORDER BY id")
    vecs = search.vectors(store, judge, "belief", beliefs, emb) if beliefs else {}
    last = (store.one("SELECT MAX(at) AS at FROM entries")["at"]) or "9999"
    results = []
    for c in checks(path):
        ok, why, t = True, [], c["type"]
        if t in ("belief", "no_belief", "count_like"):
            q = emb([c["query"]])[0]
            pool = [b for b in beliefs if t != "no_belief" or b["about"] == c.get("about", person)]
            scored = _closest(pool, vecs, q) if pool else []
            if t == "no_belief":
                hits = [(s, b) for s, b in scored if s >= c["closeness"]]
                ok = not hits
                why = [f"b{b['id']} ({s:.2f}) {b['text']}" for s, b in hits] or ["none close"]
            elif t == "count_like":
                hits = [(s, b) for s, b in scored if s >= c["closeness"]
                        and standing.of(store, knobs, b["id"])["value"] > 0]
                ok = len(hits) <= c["max"]
                why = [f"{len(hits)} holding: " + "; ".join(f"b{b['id']} ({s:.2f}) {b['text']}" for s, b in hits)]
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
                    "never_flipped": lambda v: _never_flipped(store, knobs, b["id"]) == v,
                    "his_words": lambda v: any(_norm(b["text"]) in _norm(e["text"]) for e in store.all(
                        "SELECT text FROM entries WHERE speaker = ?", person)) == v,
                }
                for k, f in tests.items():
                    if k in c and not f(c[k]):
                        ok = False
                        why.append(f"expected {k} = {c[k]}")
        elif t == "distinct":
            a = _closest(beliefs, vecs, emb([c["query"]])[0])[0][1]
            b = _closest(beliefs, vecs, emb([c["other"]])[0])[0][1]
            ok = a["id"] != b["id"]
            why = [f"b{a['id']} {a['text']}", f"b{b['id']} {b['text']}"]
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
            why = why or ["no belief stands on voice lines alone, or all such are tentative"]
        elif t == "pins":
            pinned = [b for b in beliefs if store.pinned(b["id"])]
            if "count" in c and len(pinned) != c["count"]:
                ok = False
                why.append(f"expected {c['count']} pinned, found {len(pinned)}")
            why += [f"pinned b{b['id']} {b['text']}" for b in pinned]
            for key, want in (("pinned", True), ("not_pinned", False)):
                for query in c.get(key, []):
                    s, b = _closest(beliefs, vecs, emb([query])[0])[0]
                    got = store.pinned(b["id"])
                    if got != want:
                        ok = False
                        why.append(f"expected {'pinned' if want else 'not pinned'}: {query!r} (closest b{b['id']},"
                                   f" {s:.2f}: {b['text']})")
        elif t in ("miss", "recalls"):
            r = recall.recall(store, knobs, c["query"], last)
            if t == "miss":
                ok = r.missed
            else:
                words = [w.lower() for w in c["words"]]
                ok = any(all(w in i.text.lower() for w in words) for i in r.beliefs + r.episodes)
            why = [f"best closeness {r.best:.3f}"] + [f"{i.tag} {i.closeness:.2f} {i.text[:90]}" for i in r.items()]
        elif t == "replies_lack":
            pat = re.compile(c["pattern"], re.I)
            for tr in store.all("SELECT t.entry, e.text AS said, r.text AS answer FROM turns t JOIN entries e ON e.id = t.entry"
                                " LEFT JOIN entries r ON r.id = t.reply"):
                if store.one("SELECT 1 FROM pins WHERE entry = ?", tr["entry"]):
                    continue
                if tr["answer"] and pat.search(tr["answer"]):
                    ok = False
                    why.append(f"to {tr['said'][:60]!r}: {tr['answer'][:140]!r}")
            why = why or ["none"]
        elif t == "reply":
            rows = store.all("SELECT e.text AS said, r.text AS reply FROM turns t JOIN entries e ON e.id = t.entry"
                             " LEFT JOIN entries r ON r.id = t.reply WHERE e.text LIKE ?", f"%{c['to']}%")
            if not rows:
                ok, why = False, ["no such turn"]
            for row in rows:
                reply = row["reply"] or ""
                if c.get("has") and not re.search(c["has"], reply, re.I):
                    ok = False
                if c.get("lacks") and re.search(c["lacks"], reply, re.I):
                    ok = False
                why.append(f"reply: {reply[:200]!r}")
        elif t == "things_once":
            things = store.all("SELECT * FROM things ORDER BY id")
            names = {}
            for th in things:
                for n in [th["name"]] + [a["alias"] for a in store.all("SELECT alias FROM aliases WHERE thing = ?", th["id"])]:
                    names.setdefault(" ".join(n.lower().split()), set()).add(th["id"])
            pairs = {tuple(sorted(ids)) for ids in names.values() if len(ids) > 1}
            if things:
                tv = emb([th["name"] for th in things])
                for i in range(len(things)):
                    for j in range(i):
                        if float(tv[i] @ tv[j]) >= c.get("closeness", 0.9):
                            pairs.add((things[j]["id"], things[i]["id"]))
            ok = len(pairs) <= c.get("max", 0)
            byid = {th["id"]: th["name"] for th in things}
            why = [f"{len(things)} things, {len(pairs)} pair(s) that may be one thing"] + \
                  [f"t{a} {byid[a]!r} / t{b} {byid[b]!r}" for a, b in sorted(pairs)]
        elif t == "beliefs_once":
            holding = [b for b in beliefs if standing.of(store, knobs, b["id"])["value"] > 0]
            pairs = []
            for i in range(len(holding)):
                for j in range(i):
                    a, b = holding[j], holding[i]
                    if float(vecs[a["id"]] @ vecs[b["id"]]) >= c.get("closeness", 0.92) and b["id"] not in store.opposites(a["id"]):
                        pairs.append((a, b))
            ok = len(pairs) <= c.get("max", 0)
            why = [f"{len(holding)} beliefs holding, {len(pairs)} pair(s) saying nearly the same"] + \
                  [f"b{a['id']} {a['text'][:70]!r} / b{b['id']} {b['text'][:70]!r}" for a, b in pairs]
        elif t == "about_coverage":
            n = len(beliefs)
            linked = sum(1 for b in beliefs if store.one(
                "SELECT 1 FROM edges WHERE src_kind = 'belief' AND src = ? AND rel = 'about'", b["id"]))
            ok = n and linked / n >= c["min"]
            why = [f"{linked} of {n} beliefs are about at least one thing"]
        elif t == "asks":
            # every belief close to the query (pins in his words can leave several saying it)
            q = emb([c["query"]])[0]
            near = [b for sc, b in _closest(beliefs, vecs, q) if sc >= c.get("closeness", 0.7)]
            asks = [a for b in near for a in store.all("SELECT * FROM asks WHERE belief = ? ORDER BY id", b["id"])]
            flipped = [b for b in near if standing.of(store, knobs, b["id"])["direction"] < 0]
            ok = bool(asks) and all(a["shown_at"] for a in asks) and any(a["answer"] == c["answer"] for a in asks) \
                and not flipped
            why = [f"{len(near)} beliefs close to it; flipped: {len(flipped)}"] + \
                  [f"ask {a['id']} on b{a['belief']}: shown {str(a['shown_at'])[:16]}, answer {a['answer']}: {a['claim'][:80]}"
                   for a in asks] + ([] if asks else ["no ask was made"])
        elif t == "beside":
            qa, qb = emb([c["query"]])[0], emb([c["other"]])[0]
            narrow = [b for sc, b in _closest(beliefs, vecs, qa) if sc >= c.get("closeness", 0.75)]
            broad = [b for sc, b in _closest(beliefs, vecs, qb) if sc >= c.get("closeness", 0.75)
                     and b["id"] not in {n["id"] for n in narrow}]
            edges = [e for n in narrow for b in broad for e in store.all(
                "SELECT * FROM edges WHERE rel IN ('exception_to', 'for_now') AND src_kind = 'belief' AND src = ? AND dst = ?",
                n["id"], b["id"])]
            against = [l for b in broad for l in store.links(b["id"]) if l["stance"] < 0 and any(
                store.one("SELECT 1 FROM links WHERE belief = ? AND episode = ?", n["id"], l["episode"]) for n in narrow)]
            ok = bool(edges) and not against
            why = [f"narrow: {', '.join('b%d' % n['id'] for n in narrow) or 'none'}; standing: "
                   f"{', '.join('b%d' % b['id'] for b in broad) or 'none'}"] + \
                  [f"{e['rel']} b{e['src']} -> b{e['dst']}" + (f" (settled {e['ended']})" if e["ended"] else "") for e in edges] + \
                  [f"links against the standing one from the same evidence: {len(against)}"]
        elif t == "one_of":
            a = _closest(beliefs, vecs, emb([c["query"]])[0])[0][1]
            b = _closest(beliefs, vecs, emb([c["other"]])[0])[0][1]
            both = a["id"] != b["id"] and standing.of(store, knobs, a["id"])["value"] > 0 and \
                standing.of(store, knobs, b["id"])["value"] > 0
            ok = not both
            hist = store.all("SELECT text FROM belief_texts WHERE belief = ?", a["id"])
            why = [f"b{a['id']} {a['text'][:80]}" + (f" (was: {hist[0]['text'][:60]})" if hist else ""),
                   f"b{b['id']} {b['text'][:80]}", "both hold" if both else "not both holding"]
        else:
            ok, why = False, [f"unknown check type {t}"]
        results.append((c["name"], ok, why))
    return results


def _never_flipped(store, knobs, belief):
    links = store.links(belief)
    directions = [standing.of(store, knobs, belief, upto=l["at"])["direction"] for l in links]
    directions = [d for d in directions if d]
    return all(d == directions[0] for d in directions)


def run(store, knobs, path, out=print):
    results = evaluate(store, knobs, path)
    for name, ok, why in results:
        out(f"{'PASS' if ok else 'FAIL'}  {name}")
        for w in why:
            out(f"      {w}")
    passed = sum(ok for _, ok, _ in results)
    out(f"\n{passed} of {len(results)} passed")
    return passed == len(results)


def tally(folders, knobs, path, out=print):
    """The checks over several stores: how often each failed, and which runs."""
    runs = []
    for f in folders:
        if not (pathlib.Path(f) / "memory.sqlite3").exists():
            continue
        s = Store(f)
        runs.append((pathlib.Path(f).name, evaluate(s, knobs, path)))
        s.close()
    if not runs:
        out("no stores")
        return False
    out(f"{len(runs)} runs: {', '.join(n for n, _ in runs)}\n")
    names = [n for n, _, _ in runs[0][1]]
    for i, name in enumerate(names):
        failed = [r for r, res in runs if not res[i][1]]
        out(f"  {len(failed)}/{len(runs)} failed  {name}" + (f"  ({', '.join(failed)})" if failed else ""))
    total = [sum(ok for _, ok, _ in res) for _, res in runs]
    out(f"\npassed per run: {', '.join(f'{n} {t}/{len(names)}' for (n, _), t in zip(runs, total))}")
    return True
