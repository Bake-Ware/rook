"""First-run setup and the look and feel: the agents plugin's `agent.setup` and `agent.look` (brief HA3).

Ported from 0.5 (cbe86a47: rook/plugins/agents/scripts/agents.py, "the hub site: the chat dock and the
look and feel"; plugin.yaml's `setup` and `look`; docs/castling-first-run.md). The same cap names,
arguments and replies, the same words. What core's shape changed is said where it happens:

- 0.5 kept setup in the plugin's data file `setup.json` {done, install, ts, by}. Here it is the agents
  plugin's hub setting `setup`, holding the same object (HA2's settings store).
- 0.5 counted no setup.json as done, and its installer wrote done false. Here the plugin's first load on
  a data directory writes the setting once: not done on a new hub (no person yet), done on a hub made
  before (it has people: the beta). An owner made by `--no-password`, or a password set at step 5,
  comes after that first load and never changes it. `agent.setup {done: false}` starts it again.
- While setup isn't done, every house turn carries the first-run skill (0.5's `setup_brief`), through
  the runtime's carried-skill hook (HA2): `while_setup` gives 0.5's lead-in, the runtime adds the skill.

Not ported (RULES-HA.md; 0.5's "For the hardening pass"): nothing checks who may mark setup done or set
the look, and a style, theme or name is not checked or cut: an unknown one shows as the default.
"""

import re
import time
from functools import lru_cache
from pathlib import Path

PLUGIN_DIR = Path(__file__).parent
LOOK_STEPS = ("sound", "rookie", "theme", "name")      # first-run asks these first, in this order
HOUSE_NAME = "Rookie"
ROOKIE_JS = PLUGIN_DIR / "shell" / "rookie.js"


@lru_cache(maxsize=2)
def _rookie_js(mtime):
    text = ROOKIE_JS.read_text(encoding="utf-8")
    styles = re.findall(r"\bSKINS\.([a-z][a-z0-9_]*)\s*=\s*\{", text)
    block = re.search(r"\bTHEMES\s*=\s*\{(.*?)\n\s*\};", text, re.S)
    themes = re.findall(r"^\s*([a-z][a-z0-9_]*)\s*:\s*\{", block[1], re.M) if block else []
    return tuple(dict.fromkeys(styles + ["none"])), tuple(themes or ["stone"])


def rookie_js():
    """(styles, themes) the vendored ``shell/rookie.js`` defines, read from the file itself
    (``SKINS.<name>={``, the ``THEMES={...}`` keys) so a new rookie.js drops in without a code change."""
    try:
        styles, themes = _rookie_js(ROOKIE_JS.stat().st_mtime)
    except OSError:
        return ["none"], ["stone"]
    return list(styles), list(themes)


def look_state(call):
    """A person's look and feel (castling-first-run; Bake, Nov 5 2026: "prompt the user if they want sound
    first, then rookie style then rookie theme, then rookie name. This will be the name of the home
    agent"): sound, Rookie's style and theme (theirs), Rookie's name (the house agent's, the hub's), which
    were answered (a setting never set: not yet), and the first that wasn't (``next``)."""
    s = call.settings
    styles, themes = rookie_js()
    sound, rookie, theme, name = s.get("sound"), s.get("rookie"), s.get("rookie_theme"), s.get("house_name")
    chosen = {"sound": sound is not None, "rookie": rookie is not None, "theme": theme is not None,
              "name": name is not None}
    return {"sound": bool(sound),
            "rookie": rookie if rookie in styles else "pixel",
            "theme": theme if theme in themes else themes[0],
            "name": name or HOUSE_NAME, "chosen": chosen,
            "next": next((k for k in LOOK_STEPS if not chosen[k]), None),
            "styles": styles, "themes": themes}


def _setup_value(call):
    state = call.settings.get("setup")
    return state if isinstance(state, dict) else {}


def setup_state(call, install=False):
    """First-run setup: ``{done}``, and with ``install`` the facts the installer left (folder, units,
    ports, backend). No value at all counts as done, as 0.5's missing setup.json did."""
    state = _setup_value(call)
    out = {"done": bool(state.get("done", True))}
    if install and isinstance(state.get("install"), dict):
        out["install"] = state["install"]
    return out


def while_setup(call):
    """0.5's setup_brief lead-in, while setup isn't done; None once it is. The runtime adds
    " The first-run skill, in full:" and the skill to the house agent's turn (HA2's carried skill)."""
    if setup_state(call)["done"]:
        return None
    step = look_state(call)["next"]
    nxt = f" The next look-and-feel question is: {step}." if step else ""
    return ("Setup isn't finished: you are walking this person through first-run setup. "
            f"Carry on from the first step not done yet.{nxt}")


def first_load(call):
    """The first load on a data directory decides once whether this hub starts in setup: a new hub (no
    person yet) does; a hub made before setup existed (it has people) counts as done and is never put
    into setup by surprise."""
    if call.settings.get("setup") is None:
        call.settings.set("setup", {"done": call.has_users(), "ts": time.time(), "by": "first start"})


def _actor(call):
    return {"user": call.user, "token": call.token}


def setup(plugin):
    plugin.setting("setup", "First-run setup: {done, ts, by, install}. done false: the site opens on the house "
                   "agent's setup chat (agent.setup).", where="hub")
    plugin.setting("sound", "Your look and feel: Rookie's sounds (a chiptune blip as it changes state). Off until "
                   "you answer; set by agent.look.", where="person")
    plugin.setting("rookie", "Your look and feel: Rookie's style beside the house agent (a skin shell/rookie.js "
                   "ships; none hides it); blank: pixel. Set by agent.look.", where="person")
    plugin.setting("rookie_theme", "Your look and feel: Rookie's colour theme (one shell/rookie.js ships); blank: "
                   "its first. Set by agent.look.", where="person")
    plugin.skill("first-run", "skills/first-run.md")
    plugin.carry("first-run")(while_setup)
    plugin.on_load(first_load)

    @plugin.cap("agent.setup",
                "First-run setup: whether it is finished. While it isn't, the site opens straight into the house "
                "agent's chat, which walks the owner through setting up (the first-run skill); install has the "
                "facts the installer left. done=true when the walk-through is over; done=false starts it again. "
                "Blank done: just say where it stands.",
                {"type": "object", "properties": {
                    "done": {"type": "boolean", "description": "true: setup is finished; false: not (the site "
                             "opens on the setup chat again)."}}})
    def agent_setup(call, args):
        if args.get("done") is not None:
            call.settings.set("setup", {**_setup_value(call), "done": bool(args["done"]), "ts": time.time(),
                                        "by": _actor(call)})
        return setup_state(call, install=True)

    @plugin.cap("agent.look",
                "Look and feel on the hub site, the first four first-run questions in this order: sound (yes/no), "
                "Rookie's style, Rookie's theme, Rookie's name. The name is the house agent's name (the setting "
                "house_name). Give any of them to set it; blank: just say what you have. chosen says which were "
                "answered, next is the first one that wasn't (null when all were).",
                {"type": "object", "properties": {
                    "sound": {"type": "boolean", "description": "Rookie's sounds on or off."},
                    "rookie": {"type": "string", "description": "Rookie's style, one of styles (e.g. pixel, kawaii; "
                               "none hides it)."},
                    "theme": {"type": "string", "description": "Rookie's colour theme, one of themes (e.g. stone, "
                              "gameboy)."},
                    "name": {"type": "string", "description": "Rookie's name: the house agent's name from now on."}}})
    def agent_look(call, args):
        # Anyone sets anything here, a person or an agent alone; an unknown style or theme shows as the
        # default (Bake, Nov 6: "Remove all restrictions and gates"). Sound, style and theme are the
        # person's own (where=person: the calling user's).
        for key, arg in (("sound", "sound"), ("rookie", "rookie"), ("rookie_theme", "theme")):
            if args.get(arg) is not None:
                value = args[arg]
                call.settings.set(key, bool(value) if key == "sound" else str(value).strip().lower())
        if args.get("name") is not None:
            call.settings.set("house_name", " ".join(str(args["name"]).split()) or HOUSE_NAME)
        return look_state(call)
