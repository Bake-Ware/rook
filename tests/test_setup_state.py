"""First-run setup state and the look and feel (brief HA3): the agents plugin's agent.setup and
agent.look, as 0.5 has them (cbe86a47: rook/plugins/agents/scripts/agents.py), and the first load
that tells a new hub from one made before setup existed.

These run hub_setup.py against a stand-in for the plugin library's settings, skill and on-load
hooks (brief HA2's names: plugin.setting, plugin.skill, plugin.carry, plugin.on_load, call.settings).
Once HA2's library is in, tests/test_wizard.py drives the same caps through a real hub."""

import importlib.util
import pathlib

import pytest

AGENTS = pathlib.Path(__file__).parent.parent / "hub" / "rook_hub" / "defaults" / "agents"
SYSTEM = "system-guid"


def load_module():
    spec = importlib.util.spec_from_file_location("hub_setup_under_test", AGENTS / "hub_setup.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Store:
    """The settings HA2 keeps at the hub: where=hub once, where=person per user."""

    def __init__(self):
        self.where, self.values, self.install = {"house_name": "hub"}, {}, None

    def key(self, name, user):
        return (name, None if self.where[name] == "hub" else user)


class Settings:
    def __init__(self, store, user):
        self.store, self.user = store, user

    def get(self, name):
        return self.store.values.get(self.store.key(name, self.user))

    __getitem__ = get

    def set(self, name, value):
        self.store.values[self.store.key(name, self.user)] = value


class Call:
    def __init__(self, store, user, people=True):
        self.user, self.token, self.settings, self._people = user, None, Settings(store, user), people

    def has_users(self):
        return self._people

    def install_facts(self):
        return self.settings.store.install


class Plugin:
    def __init__(self, store):
        self.store, self.caps, self.skills, self.carried, self.loads = store, {}, {}, {}, []

    def setting(self, name, doc, where="hub", default=None):
        self.store.where[name] = where

    def skill(self, name, file):
        self.skills[name] = AGENTS / file

    def carry(self, name):
        def register(fn):
            self.carried[name] = fn
            return fn
        return register

    def on_load(self, fn):
        self.loads.append(fn)

    def cap(self, name, description, schema):
        def register(fn):
            self.caps[name] = fn
            return fn
        return register


@pytest.fixture
def plugin():
    store = Store()
    plugin = Plugin(store)
    load_module().setup(plugin)
    return plugin


def call(plugin, user="alice", people=True):
    return Call(plugin.store, user, people)


def test_declares_what_0_5_had(plugin):
    assert set(plugin.caps) == {"agent.setup", "agent.look"}
    assert plugin.store.where == {"house_name": "hub", "setup": "hub", "sound": "person", "rookie": "person",
                                  "rookie_theme": "person"}
    assert plugin.skills["first-run"].is_file() and set(plugin.carried) == {"first-run"}


def test_a_new_hub_starts_in_setup_and_done_ends_it(plugin):
    plugin.loads[0](call(plugin, SYSTEM, people=False))          # the first load on a new data directory
    assert plugin.caps["agent.setup"](call(plugin), {}) == {"done": False}
    plugin.loads[0](call(plugin, SYSTEM, people=True))           # later starts: an owner exists now
    assert plugin.caps["agent.setup"](call(plugin), {}) == {"done": False}     # decided once, at the first load
    lead = plugin.carried["first-run"](call(plugin))
    assert lead.startswith("Setup isn't finished: you are walking this person through first-run setup.")
    assert lead.endswith("The next look-and-feel question is: sound.")
    assert plugin.caps["agent.setup"](call(plugin), {"done": True}) == {"done": True}
    state = plugin.store.values[("setup", None)]
    assert state["done"] is True and state["by"] == {"user": "alice", "token": None}
    assert plugin.carried["first-run"](call(plugin)) is None     # no skill on a turn once it's done
    assert plugin.caps["agent.setup"](call(plugin), {"done": False}) == {"done": False}    # and again


def test_a_hub_made_before_setup_is_done(plugin):
    """The beta: people, and no setup value; it comes up done and is never put into setup."""
    plugin.loads[0](call(plugin, SYSTEM, people=True))
    assert plugin.caps["agent.setup"](call(plugin), {}) == {"done": True}
    assert plugin.carried["first-run"](call(plugin)) is None


def test_no_value_counts_as_done_and_install_facts_are_returned(plugin):
    assert plugin.caps["agent.setup"](call(plugin), {}) == {"done": True}       # 0.5: no setup.json is done
    plugin.store.values[("setup", None)] = {"done": False, "install": {"folder": "/opt/rook", "http": 8740}}
    assert plugin.caps["agent.setup"](call(plugin), {}) == {"done": False,
                                                           "install": {"folder": "/opt/rook", "http": 8740}}
    plugin.caps["agent.setup"](call(plugin), {"done": True})
    assert plugin.store.values[("setup", None)]["install"] == {"folder": "/opt/rook", "http": 8740}  # kept
    # Core's installer (HA4) writes the facts to the data directory's install.json: read from there.
    del plugin.store.values[("setup", None)]["install"]
    plugin.store.install = {"by": "deploy/install/install.sh", "http": "0.0.0.0:8740 (every interface)"}
    assert plugin.caps["agent.setup"](call(plugin), {})["install"] == plugin.store.install


def test_look_steps_in_order(plugin):
    look = plugin.caps["agent.look"]
    first = look(call(plugin), {})
    assert first["next"] == "sound" and first["name"] == "Rookie" and first["sound"] is False
    assert first["chosen"] == {"sound": False, "rookie": False, "theme": False, "name": False}
    assert look(call(plugin), {"sound": False})["next"] == "rookie"           # an answer of "no" is an answer
    style, theme = first["styles"][0], first["themes"][-1]
    assert look(call(plugin), {"rookie": style.upper()})["next"] == "theme"     # kept as 0.5 kept it: lower case
    assert look(call(plugin), {"theme": theme})["next"] == "name"
    done = look(call(plugin), {"name": "  Jeeves   the  Rook "})
    assert done["next"] is None and done["name"] == "Jeeves the Rook" and done["rookie"] == style
    assert done["theme"] == theme and all(done["chosen"].values())
    assert plugin.store.values[("house_name", None)] == "Jeeves the Rook"
    lead = plugin.carried["first-run"](call(plugin))
    assert lead is None or "look-and-feel question" not in lead


def test_look_is_each_persons_and_the_name_is_the_hubs(plugin):
    look = plugin.caps["agent.look"]
    look(call(plugin, "alice"), {"sound": True, "rookie": "none", "name": "Jeeves"})
    bob = look(call(plugin, "bob"), {})
    assert bob["sound"] is False and bob["chosen"]["rookie"] is False and bob["next"] == "sound"
    assert bob["name"] == "Jeeves" and bob["chosen"]["name"] is True
    alice = look(call(plugin, "alice"), {})
    assert alice["sound"] is True and alice["rookie"] == "none"


def test_nothing_is_checked_or_cut(plugin):
    """0.5's "For the hardening pass", 3: unknown styles and themes show as the default; no name cap."""
    look = plugin.caps["agent.look"]
    out = look(call(plugin), {"rookie": "no-such-style", "theme": "no-such-theme", "name": "x" * 500})
    assert out["rookie"] == "pixel" and out["theme"] == out["themes"][0] and out["name"] == "x" * 500
    assert out["chosen"]["rookie"] and out["chosen"]["theme"]
    assert look(call(plugin), {"name": "   "})["name"] == "Rookie"            # blank: Rookie


def test_the_skill_keeps_0_5s_words():
    skill = (AGENTS / "skills" / "first-run.md").read_text()
    assert skill.startswith("---\ndescription: Walk a new owner through setting up their hub")
    for words in ("[[open: account]]", "[[choices: A personal assistant | Running my computers | A project | My home]]",
                  "[[choices: Just this computer | Other devices at home | From anywhere]]",
                  "Directive: if they choose anything other than \"Just this computer\"",
                  "Never ask for a password in the chat.", "Then call `agent.setup {done: true}`"):
        assert words in skill, words
    # Core's caps in place of 0.5's that core does not have.
    for gone in ("hub.joins", "hub.join_code", "hub.workers", "/health", "doors.mcp.bind", "castling-worker"):
        assert gone not in skill, gone
    for core in ("worker.join-token", "worker.approve", "backup.list", "secret:host_password"):
        assert core in skill, core
