"""A belief's standing, worked out from its links. A belief has no score of its own.

Each link says whether an episode supports the belief (+1) or counts against it (-1), and how
strongly (kind x who said it x channel; see [weigh] in knobs.toml).

- Direction: the most recent link is what we go on. Older links are not averaged in.
- Certainty (maturity): the occasions behind the current direction, each counted once at its
  strongest, summed and put through 1 - exp(-sum / maturity_scale). One occasion is tentative however
  sure it sounded; it matures as it comes up again on separate occasions. Occasions from before the
  latest contrary link count at before_flip_weight.
- The contrary history is kept as its own certainty, and flips are counted, so a fresh one-off against
  a mature belief shows as a conflict ("ask before acting") instead of quietly winning or losing.
- A pinned belief stands at +1 whatever its links say; links against it are still recorded and shown.
"""

import math


def strength(knobs, kind, said_by, channel):
    w = knobs["weigh"]
    who = "person" if said_by == knobs["people"]["person"] else said_by
    return (w.get(f"kind_{kind}", w["kind_mention"]) * w.get(f"by_{who}", w["by_other"])
            * w.get(f"channel_{channel}", 1.0))


def occasion(knobs, at, session):
    return str(at)[:10] if knobs["weigh"]["occasion"] == "day" else f"session {session}"


def _cert(total, knobs):
    return 1.0 - math.exp(-total / knobs["weigh"]["maturity_scale"])


def standing(links, knobs, pinned=False, upto=None):
    """links: mappings with stance, strength, at, occasion and id. upto: only links at or before it."""
    w = knobs["weigh"]
    links = sorted((l for l in links if upto is None or str(l["at"]) <= str(upto)), key=lambda l: (str(l["at"]), l["id"]))
    counted = [l for l in links if l["strength"] > 0]
    flips = sum(1 for a, b in zip(counted, counted[1:]) if a["stance"] != b["stance"])
    out = {"pinned": pinned, "links": len(links), "counted": len(counted), "flips": flips,
           "direction": 0, "certainty": 0.0, "contrary": 0.0, "occasions": 0, "contrary_occasions": 0,
           "conflict": False}
    if counted:
        d = counted[-1]["stance"]
        last_contrary = max((str(l["at"]) for l in counted if l["stance"] != d), default="")
        mine, theirs = {}, {}
        for l in counted:
            pool = mine if l["stance"] == d else theirs
            key = l["occasion"]
            weight = l["strength"]
            if pool is mine and str(l["at"]) <= last_contrary:
                weight *= w["before_flip_weight"]
            pool[key] = max(pool.get(key, 0.0), weight)
        out.update(direction=d, certainty=_cert(sum(mine.values()), knobs), contrary=_cert(sum(theirs.values()), knobs),
                   occasions=len(mine), contrary_occasions=len(theirs))
        out["conflict"] = out["certainty"] < w["tentative_below"] and out["contrary"] >= w["mature_at"]
    if pinned:
        out["value"] = 1.0
        out["label"] = "pinned"
        out["conflict"] = False
    else:
        out["value"] = out["direction"] * out["certainty"]
        out["label"] = label(out, knobs)
    return out


def label(s, knobs):
    w = knobs["weigh"]
    if not s["direction"]:
        return "no evidence"
    word = "holds" if s["direction"] > 0 else "does not hold"
    if s["certainty"] < w["tentative_below"]:
        grade = "tentative"
    elif s["certainty"] >= w["mature_at"]:
        grade = "mature"
    else:
        grade = "settling"
    text = f"{word}, {grade}"
    if s["conflict"]:
        text += f"; ask before acting: a fresh one-off against a mature '{'holds' if s['direction'] < 0 else 'does not hold'}'"
    return text


def summary(s):
    """One line: the standing, how many occasions stand behind it, and its history."""
    if s["pinned"]:
        against = s.get("against_since_pin", 0)
        return "PINNED" + (f" ({against} link{'s' if against != 1 else ''} against since, not counted)" if against else "")
    parts = [f"{s['label']} ({s['value']:+.2f}; {s['occasions']} occasion{'s' if s['occasions'] != 1 else ''})"]
    if s["contrary_occasions"]:
        parts.append(f"{s['contrary_occasions']} contrary occasion{'s' if s['contrary_occasions'] != 1 else ''}"
                     f" ({s['contrary']:.2f})")
    if s["flips"]:
        parts.append(f"flipped {s['flips']} time{'s' if s['flips'] != 1 else ''}")
    return "; ".join(parts)


def of(store, knobs, belief, upto=None):
    """The standing of one stored belief now (or as it was at `upto`)."""
    links = store.links(belief)
    pins = [p for p in store.pins(belief) if upto is None or p["at"] <= upto]
    pinned = bool(pins) and pins[-1]["action"] == "pin"
    s = standing(links, knobs, pinned=pinned, upto=upto)
    if pinned:
        since = pins[-1]["at"]
        s["pinned_at"] = since
        s["pinned_words"] = pins[-1]["words"]
        s["against_since_pin"] = sum(1 for l in links if l["stance"] < 0 and l["at"] >= since
                                     and (upto is None or l["at"] <= upto))
    return s


def cases(knobs):
    """The three cases from the brief, with the knobs as they are: lines to print."""
    stmt = strength(knobs, "statement", knobs["people"]["person"], "chat")
    corr = strength(knobs, "correction", knobs["people"]["person"], "chat")

    def L(i, stance, w, day):
        return {"id": i, "stance": stance, "strength": w, "at": day, "occasion": day}

    held = [L(1, 1, stmt, "2026-05-04"), L(2, 1, stmt, "2026-05-19"), L(3, 1, stmt, "2026-06-20")]
    steps = [
        ("A mature belief: said plainly on three separate days", held),
        ("1. one statement against it", held + [L(4, -1, stmt, "2026-07-01")]),
        ("   ... or one correction from him against it", held + [L(4, -1, corr, "2026-07-01")]),
        ("2. the same statement on three separate days", held + [L(4, -1, stmt, "2026-07-01"), L(5, -1, stmt, "2026-07-08"),
                                                            L(6, -1, stmt, "2026-07-20")]),
        ("   ... or three times in one sitting (one occasion)", held + [L(4, -1, stmt, "2026-07-01"),
                                                                  L(5, -1, stmt, "2026-07-01"), L(6, -1, stmt, "2026-07-01")]),
        ("3. a flip: said once, then once against", [L(1, 1, stmt, "2026-05-19"), L(2, -1, stmt, "2026-07-08")]),
        ("   and the flip back", [L(1, 1, stmt, "2026-05-19"), L(2, -1, stmt, "2026-07-08"), L(3, 1, stmt, "2026-07-20")]),
        ("One garbled voice line", [L(1, 1, strength(knobs, "unclear", knobs["people"]["person"], "voice"), "2026-06-10")]),
    ]
    return [f"{title}\n    {summary(standing(links, knobs))}" for title, links in steps]
