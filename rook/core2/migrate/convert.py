"""0.4 state -> 0.5 shapes. Pure functions; what has no 0.5 equivalent comes
back as notes for a person to read, never silently dropped.

People (spec 8.5): an owner keeps preset ``owner``; a member gets a
``member-<tier>`` preset (no grants of its own, ``max_risk`` their highest
0.4 grant tier) and their active grants as patterns. 0.5 has one ceiling per
person where 0.4 had one per grant, so a lower-tier grant beside a higher one
widens: each such grant is a note.

Trust map: a sacred ``worker:`` entry becomes a name in the config's
``sacred`` list (its changes are hard stops); ``project:`` entries become the
project's trust record (sacred -> sacred, guarded -> normal, ship-it ->
ship-it). The rest (guarded machines, ``cap:``, ``repo:``, ``resource:``,
exceptions, baselines, ledger, tool grants) has no 0.5 equivalent yet.
"""
from __future__ import annotations

import copy
import datetime as _dt
import fnmatch
import re

TIERS = ("read", "write", "exec", "admin")       # 0.4 tiers == 0.5 risks
DEFAULT_TIER = "exec"
HUB = "rook"                                     # the hub node's name in 0.5 targets
KNOWLEDGE_READ = ["knowledge.get", "knowledge.list", "knowledge.search", "knowledge.history"]
PROJECT_LEVEL = {"sacred": "sacred", "guarded": "normal", "ship-it": "ship-it"}
LEVEL_RANK = ("ship-it", "normal", "sacred")
TRUST_LISTS = ("exceptions", "baseline", "ledger", "grants", "proposals")
NAME = re.compile(r"[a-z][a-z0-9_-]*")


def user_name(p: dict, taken: set) -> str:
    """A 0.5 user name (``[a-z][a-z0-9_-]*``) from the account's username,
    the member label or the id; ``-2``, ``-3``... when taken."""
    for raw in (p.get("username"), p.get("label"), p.get("name"), p["id"]):
        base = re.sub(r"[^a-z0-9_-]+", "-", str(raw or "").lower()).strip("-_")
        if base:
            break
    if not NAME.fullmatch(base):
        base = "u-" + base
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


def _tier(t) -> str:
    t = str(t or "").strip().lower()
    t = {"r": "read", "w": "write", "x": "exec", "a": "admin"}.get(t, t)
    return t if t in TIERS else DEFAULT_TIER


def grant_patterns(g: dict, worker_names: dict, several_bands: bool) -> tuple[list, list]:
    """One 0.4 grant -> (0.5 patterns, notes). ``worker_names``: 0.4 worker
    id, or casefolded name, -> the machine's name."""
    caps, kind, ref, notes = g["caps"] or ["*"], g["kind"], g["ref"], []
    odd = [c for c in caps if any(ch in c for ch in "?[]")]
    if odd:
        notes.append(f"grant {g['id']}: {', '.join(odd)} used fnmatch wildcards; in 0.5 only * is a wildcard")
    if kind == "any":
        return list(caps), notes
    if kind == "band":
        if several_bands:
            notes.append(f"grant {g['id']} (band {ref[:8]}) now covers every machine of the 0.5 hub")
        return list(caps), notes
    if kind == "worker":
        name = worker_names.get(ref) or worker_names.get(ref.casefold(), ref)     # 0.4 matched names in any case
        return [f"{c}@{name}" for c in caps], notes
    if kind == "hub":
        if caps != ["*"]:
            notes.append(f"grant {g['id']}: hub caps {', '.join(caps)} keep their 0.4 names; "
                         "0.5 renamed many (castling-caps): check them")
        return [f"{c}@{HUB}" for c in caps], notes
    if kind == "knowledge":
        if ref != "*":
            notes.append(f"grant {g['id']}: knowledge area {ref!r} is not kept; 0.5 grants cover all knowledge")
        return (["knowledge.*"] if "knowledge.write" in caps or "*" in caps else list(KNOWLEDGE_READ)), notes
    notes.append(f"grant {g['id']}: {kind} {ref} has no 0.5 equivalent yet (chat spaces come with the "
                 "telegram/discord plugins): not carried over")
    return [], notes


def people(rows: list[dict], existing: dict, worker_names: dict, several_bands: bool,
           rename: dict | None = None) -> tuple[dict, dict, list, list]:
    """0.4 members -> ``(users, presets, mapping, notes)``.

    ``users`` is the config's ``users:`` after the merge (``existing`` kept:
    a person already there keeps their preset and gains the grants);
    ``presets`` the ``member-<tier>`` presets used; ``mapping`` one row per
    0.4 member for the report."""
    rename = rename or {}
    users = copy.deepcopy(existing)
    taken, claimed, mapping, notes = set(users), set(), [], []
    for p in rows:
        base = user_name(p, set())
        name = rename.get(p["id"]) or rename.get(base)
        if not name:                             # "bake" in both configs: the same person
            name = base if base in existing and base not in claimed else user_name(p, taken)
        claimed.add(name)
        grants, tiers = [], []
        for g in (p["grants"] if p["role"] != "owner" else []):
            pats, gnotes = grant_patterns(g, worker_names, several_bands)
            notes += [f"{name}: {n}" for n in gnotes]
            if pats:
                grants += pats
                tiers.append((_tier(g["tier"]), g["id"]))
        if p["role"] == "owner":
            preset = "owner"
        else:
            top = max((t for t, _ in tiers), key=TIERS.index, default=DEFAULT_TIER)
            preset = f"member-{top}"
            for t, gid in tiers:
                if t != top:
                    notes.append(f"{name}: grant {gid} was {t}-only; under {preset} it reaches {top} caps")
        for t in p["tokens"]:
            notes.append(f"{name}: 0.4 API key {t['label'] or t['agent_id']} isn't carried over: "
                         f"mint a new one with `python -m rook.core2.hub --config <hub.yaml> token --person {name}`")
        for c in p["chat_links"]:
            notes.append(f"{name}: {c['platform']} link {c['handle'] or ''} isn't carried over: link again "
                         "once the 0.5 chat plugin runs")
        if name in users:
            u = users[name]
            if u.get("preset") != preset:
                notes.append(f"{name}: already a user as {u.get('preset')}; kept that (0.4 said {preset})")
            if grants:
                u["grants"] = list(dict.fromkeys(list(u.get("grants") or ()) + grants))
        else:
            users[name] = {"preset": preset, **({"grants": grants} if grants else {})}
        taken.add(name)
        mapping.append({"from": p["id"], "name": name, "role": p["role"],
                        "preset": users[name]["preset"], "grants": users[name].get("grants", [])})
    presets = {u["preset"]: {"grants": [], "max_risk": u["preset"][7:]} for u in users.values()
               if u.get("preset", "").startswith("member-") and u["preset"][7:] in TIERS}
    return users, presets, mapping, notes


def trust(doc: dict | None, workers: list[str]) -> tuple[list, dict, list]:
    """trust.json -> ``(sacred worker names, {project slug: level}, notes)``."""
    if not doc:
        return [], {}, []
    sacred, projects, notes = [], {}, []
    for e in doc.get("entries") or ():
        level, rest = e.get("level"), []
        for t in e.get("targets") or ():
            kind, _, val = str(t).partition(":")
            if kind == "project" and level in PROJECT_LEVEL:
                new = PROJECT_LEVEL[level]
                old = projects.get(val)
                projects[val] = new if old is None or LEVEL_RANK.index(new) > LEVEL_RANK.index(old) else old
            elif kind == "worker" and level == "sacred":
                if any(ch in val for ch in "*?["):
                    hits = [w for w in workers if fnmatch.fnmatch(w.casefold(), val.casefold())]
                    notes.append(f"trust entry {e.get('id')}: sacred {t} matched {', '.join(hits) or 'no machine'}; "
                                 "0.5's sacred list names machines, so a new one that matches isn't covered")
                    sacred += hits
                else:
                    sacred.append(val)
            elif kind == "worker" and level == "ship-it":
                pass                              # agents just go: 0.5's default
            else:
                rest.append(t)
        if rest:
            notes.append(f"trust entry {e.get('id')} ({level}): {', '.join(rest)} has no 0.5 equivalent "
                         f"yet: not carried over. {e.get('description') or ''}".rstrip())
        if e.get("for_users"):
            notes.append(f"trust entry {e.get('id')}: only for {', '.join(e['for_users'])} in 0.4; "
                         "0.5 applies it to everyone")
    if doc.get("unlisted", "balanced") != "balanced":
        notes.append(f"trust map: machines no entry covers were {doc['unlisted']} in 0.4; 0.5 has no such default")
    for key in TRUST_LISTS:
        n = len(doc.get(key) or ())
        if n:
            notes.append(f"trust map: {n} {key} entries have no 0.5 equivalent: not carried over")
    return sorted(set(sacred), key=str.casefold), projects, notes


def memory(rows: list[dict], who: dict) -> tuple[list, list, dict]:
    """0.4 home-agent memories -> the house agent's ``(memories, notes,
    skipped by state)``. Only active rows move; forgotten ones never do. A
    learned procedure nobody approved goes on the notepad, so the dream cycle
    decides. A person's memory names them (``who``: 0.4 id -> 0.5 name)."""
    keep, jot, skipped = [], [], {}
    for r in rows:
        if r["state"] != "active":
            skipped[r["state"]] = skipped.get(r["state"], 0) + 1
            continue
        scope = str(r["scope"] or "shared")
        text = r["text"]
        if scope != "shared":
            person = scope.partition(":")[2] or scope
            text = f"About {who.get(person, person)}: {text}"
        item = {"text": text, "created": r["created"], "updated": r["updated"]}
        (jot if r["kind"] == "procedure" and not (r["approved"] or r["pinned"]) else keep).append(item)
    return keep, jot, skipped


def checklist(workers: list[dict], sacred: list, hub_url: str) -> list[dict]:
    """One re-enrollment row per 0.4 machine."""
    out = []
    for w in workers:
        last = w["last_seen"]
        out.append({"name": w["name"], "calls": w["calls"], "sacred": w["name"] in sacred,
                    "last_seen": _dt.datetime.fromtimestamp(last, _dt.timezone.utc).strftime("%Y-%m-%d")
                    if last else None,
                    "enroll": f"python -m rook.core2.worker --config worker.yaml enroll {hub_url} --name {w['name']}"})
    return out
