"""Doubles shared by the plugin management tests (Manage > Plugins)."""

from __future__ import annotations

from rook.hub.node import HubNode
from rook.hub.settings_store import SettingsStore


class FakeJournal:
    def __init__(self):
        self.rows = []

    def record(self, **kw):
        self.rows.append(kw)
        return "id"


class FakeClient:
    """A band client with a roster and per-worker worker.plugin.* replies."""

    def __init__(self, workers, plugins):
        self.workers = workers
        self.plugins = plugins          # wid -> {module: row}
        self.calls = []

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        self.calls.append((cap, dict(args or {}), target, identity))
        rows = self.plugins[target]
        if cap == "worker.plugin.list":
            return {"ok": True, "result": {"ok": True, "plugins": list(rows.values()), "enable": None}}
        module = (args or {}).get("module")
        if cap == "worker.plugin.enable":
            if rows.get(module, {}).get("state") == "unavailable":
                return {"ok": True, "result": {"ok": False, "error": f"{module} did not load: unavailable"}}
            rows[module] = {**rows.get(module, {"module": module}), "loaded": True, "disabled": False,
                            "state": "loaded"}
            return {"ok": True, "result": {"ok": True, "module": module}}
        if cap == "worker.plugin.disable":
            rows[module] = {**rows[module], "loaded": False, "disabled": True, "state": "disabled",
                            "reason": "disabled by an operator"}
            return {"ok": True, "result": {"ok": True, "module": module}}
        if cap == "worker.plugin.reload":
            return {"ok": True, "result": {"ok": True, "module": module}}
        raise AssertionError(cap)


WORKER_CAPS = ["worker.plugin.list", "worker.plugin.enable", "worker.plugin.disable",
               "worker.plugin.reload", "shell.exec"]


def band_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.delenv("ROOK_SETTINGS_DB", raising=False)
    monkeypatch.delenv("ROOK_KNOWLEDGE", raising=False)
    monkeypatch.delenv("ROOK_TASKS", raising=False)
    from rook.remote.enrollment import EnrollmentStore
    enroll = EnrollmentStore()
    band = enroll.register("home", "piano-haste-nugget-drift-mangle", "hub.example.com:443",
                           primary=True)
    return enroll, band


def make_node(tmp_path, client=None, enrollment=None):
    node = HubNode(str(tmp_path / "state"), client=client, enrollment=enrollment,
                   settings_store=SettingsStore(tmp_path / "settings.db"))
    node.journal = FakeJournal()
    return node


def workers_and_plugins():
    workers = {
        "w1": {"name": "box", "caps": WORKER_CAPS, "band": "*", "facts": {"os": "linux"},
               "version": "500.a.b", "tiers": {"shell.exec": "x"}},
        "w2": {"name": "old", "caps": ["shell.exec"], "band": "*", "facts": {"os": "windows"}},
    }
    plugins = {"w1": {
        "shell": {"module": "shell", "loaded": True, "disabled": False, "namespace": "shell",
                  "caps": ["shell.exec"], "state": "loaded", "source": "package:rook.worker.plugins"},
        "hid": {"module": "hid", "loaded": False, "disabled": True, "state": "disabled",
                "reason": "disabled by an operator"},
        "pikvm": {"module": "pikvm", "loaded": False, "disabled": False, "state": "unavailable",
                  "reason": "available() returned False"},
        "selfupdate": {"module": "selfupdate", "loaded": True, "state": "loaded"},
        "screen": {"module": "screen", "loaded": False, "state": "not_placed",
                   "reason": "placement \"app == 'android'\" does not match this node",
                   "source": "package:rook_android.plugins"},
        "thirdparty": {"module": "thirdparty", "loaded": True, "state": "loaded", "namespace": "tp",
                       "caps": ["tp.go"], "source": "entry_point:thirdparty"},
    }, "w2": {}}
    return workers, plugins
